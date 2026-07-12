"""Offline coverage for redirect-based general authority provenance.

Caso real (Porto Alegre, golden36): la cadena HTTP es
https://www.portoalegre.rs.gov.br/smap/servicos/selecao-e-provimento/concursos-em-andamento
-> 301/302 x4 -> https://prefeitura.poa.br/smap (200). El host de destino no
es *.rs.gov.br, pero el host de ORIGEN si es el dominio oficial confirmado
del municipio: eso es la unica evidencia estructural que este modulo emite.
"""

from __future__ import annotations

import csv

import pytest

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2 import authority
from scripts.fase2_municipios.v2.eval.structural_evidence import structural_candidate


pytestmark = pytest.mark.offline

MUNICIPIO = "Porto Alegre"
BUCKET = "concurso_publico"
OFFICIAL_ORIGIN = (
    "https://www.portoalegre.rs.gov.br/smap/servicos/selecao-e-provimento/"
    "concursos-em-andamento"
)
DELEGATED_DESTINATION = "https://prefeitura.poa.br/smap"


def test_redirect_confirms_official_origin_true_for_official_origin_and_new_host() -> None:
    assert authority.redirect_confirms_official_origin(
        OFFICIAL_ORIGIN, DELEGATED_DESTINATION, MUNICIPIO
    ) is True


def test_redirect_confirms_official_origin_false_for_non_official_origin() -> None:
    assert authority.redirect_confirms_official_origin(
        "https://example.com/x", DELEGATED_DESTINATION, MUNICIPIO
    ) is False


def test_redirect_confirms_official_origin_false_without_host_change() -> None:
    same_host_final = "https://www.portoalegre.rs.gov.br/smap/outra-pagina"
    assert authority.redirect_confirms_official_origin(
        OFFICIAL_ORIGIN, same_host_final, MUNICIPIO
    ) is False


@pytest.mark.parametrize(
    ("requested_url", "final_url"),
    [
        ("", DELEGATED_DESTINATION),
        (OFFICIAL_ORIGIN, ""),
        ("", ""),
    ],
)
def test_redirect_confirms_official_origin_false_for_empty_url(
    requested_url: str, final_url: str
) -> None:
    assert authority.redirect_confirms_official_origin(
        requested_url, final_url, MUNICIPIO
    ) is False


def test_redirect_provenance_emits_official_referrer_when_confirmed() -> None:
    provenance = authority.redirect_provenance(
        OFFICIAL_ORIGIN, DELEGATED_DESTINATION, MUNICIPIO
    )

    assert provenance == (
        {
            "kind": "official_referrer",
            "municipio": MUNICIPIO,
            "referrer": OFFICIAL_ORIGIN,
            "label": "http_redirect_official_origin",
            "evidence": "www.portoalegre.rs.gov.br -> prefeitura.poa.br",
        },
    )
    # El kind que emitimos debe ser exactamente el que cascade ya acepta.
    assert cascade._provenance_confirms(provenance, MUNICIPIO) is True


def test_redirect_provenance_empty_when_origin_not_official() -> None:
    assert authority.redirect_provenance(
        "https://example.com/x", DELEGATED_DESTINATION, MUNICIPIO
    ) == ()


def test_redirect_provenance_empty_without_redirect() -> None:
    assert authority.redirect_provenance(
        OFFICIAL_ORIGIN, OFFICIAL_ORIGIN, MUNICIPIO
    ) == ()


def test_redirect_provenance_empty_for_empty_url() -> None:
    assert authority.redirect_provenance("", DELEGATED_DESTINATION, MUNICIPIO) == ()
    assert authority.redirect_provenance(OFFICIAL_ORIGIN, "", MUNICIPIO) == ()


def test_structural_candidate_confirms_authority_via_redirect_provenance() -> None:
    """Reproduce el caso Porto Alegre: A/B en consenso correcto pero el gate
    estructural rechazaba (authority='desconocida') porque nadie emitia la
    provenance del redirect real hasta ahora. La identidad la gana el DESTINO
    por su propio contenido (la pagina real dice "Prefeitura de Porto
    Alegre"), no la provenance."""
    provenance = authority.redirect_provenance(
        OFFICIAL_ORIGIN, DELEGATED_DESTINATION, MUNICIPIO
    )
    snapshot = cascade.EvidenceSnapshot(
        html=(
            "<html><title>Concursos em andamento - Prefeitura de Porto Alegre"
            "</title><body>Prefeitura de Porto Alegre - Concursos em andamento"
            " no SMAP.</body></html>"
        ),
        text="Prefeitura de Porto Alegre - Concursos em andamento no SMAP.",
        title="Concursos em andamento - Prefeitura de Porto Alegre",
        final_url=DELEGATED_DESTINATION,
        requested_url=OFFICIAL_ORIGIN,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=OFFICIAL_ORIGIN,
        source="orion_http",
        tier="live",
        municipio=MUNICIPIO,
        bucket=BUCKET,
        evidence=snapshot,
        provenance=provenance,
    )

    assert candidate.authority == "confirmada"
    assert candidate.source_kind == "portal_externo_delegado"
    assert candidate.identity == "confirmada"


def test_redirect_provenance_never_launders_identity_cross_municipio() -> None:
    """Regresion adversarial (Opus 12-jul): un open-redirect desde el dominio
    oficial hacia un indice REAL de OTRO municipio (que jamas menciona el
    municipio objetivo) no debe producir identity='confirmada'. La provenance
    solo prueba la cadena de autoridad; la identidad exige evidencia del
    destino, y sin ella _safety_blockers del gate rechaza la confirmacion."""
    origin = "https://ararica.rs.gov.br/portal/redirect?url=externo"
    destination = "https://atende.net/canoas/concursos"
    provenance = authority.redirect_provenance(origin, destination, "Ararica")
    assert provenance  # la cadena de autoridad SI se emite (origen oficial)

    snapshot = cascade.EvidenceSnapshot(
        html=(
            "<html><title>Concursos - Prefeitura de Canoas</title>"
            "<body>Prefeitura Municipal de Canoas. Lista de Concursos "
            "Publicos vigentes. Edital 01/2026.</body></html>"
        ),
        text=(
            "Prefeitura Municipal de Canoas. Lista de Concursos Publicos "
            "vigentes. Edital 01/2026."
        ),
        title="Concursos - Prefeitura de Canoas",
        final_url=destination,
        requested_url=origin,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=origin,
        source="orion_http",
        tier="live",
        municipio="Ararica",
        bucket=BUCKET,
        evidence=snapshot,
        provenance=provenance,
    )

    assert candidate.identity != "confirmada"


def test_structural_candidate_stays_unconfirmed_without_provenance() -> None:
    """Sin provenance (ni de redirect ni de registro), el mismo destino
    externo no gana autoridad por si solo -- la regresion que este fix
    corrige es precisamente la ausencia de provenance, no una relajacion
    general del gate. Usa un municipio fuera del registro a proposito: para
    ``MUNICIPIO`` (Porto Alegre) ``DELEGATED_DESTINATION`` esta en
    ``dominios_oficiales_rs.csv`` (fila con host ``prefeitura.poa.br``), asi
    que la autoridad se confirmaria legitimamente por REGISTRO -- eso se
    prueba aparte en ``test_structural_candidate_registry_confirms_identity_
    for_thin_spa_shell`` y hermanos, no aqui."""
    unregistered_municipio = "Municipio Sem Registro Nenhum"
    snapshot = cascade.EvidenceSnapshot(
        html=(
            "<html><title>Concursos em andamento - SMAP</title>"
            "<body>Concursos em andamento no SMAP.</body></html>"
        ),
        text="Concursos em andamento no SMAP.",
        title="Concursos em andamento - SMAP",
        final_url=DELEGATED_DESTINATION,
        requested_url=OFFICIAL_ORIGIN,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=OFFICIAL_ORIGIN,
        source="orion_http",
        tier="live",
        municipio=unregistered_municipio,
        bucket=BUCKET,
        evidence=snapshot,
    )

    assert authority.registry_official_host(
        unregistered_municipio, DELEGATED_DESTINATION
    ) is None
    assert candidate.authority == "desconocida"


# ---------------------------------------------------------------------------
# Registro versionado de dominios oficiales (dominios_oficiales_rs.csv)
# ---------------------------------------------------------------------------
# Directiva Luis/Orion (12-jul): registro versionado + cadena verificable +
# provenance -- resuelve las clases 1 (slug no estandar en .rs.gov.br) y 2
# (portal delegado oficial sin redirect activo) del QA de 20260712 sobre las
# 36 URLs del fixture golden. El registro asienta HECHOS curados a mano
# (host<->municipio); nunca la decision confirmar/revisar.


def test_registry_official_host_matches_with_and_without_www() -> None:
    assert authority.registry_official_host("Aratiba", "pmaratiba.rs.gov.br") is not None
    assert authority.registry_official_host("Aratiba", "www.pmaratiba.rs.gov.br") is not None


def test_registry_official_host_matches_subdomain_suffix() -> None:
    entry = authority.registry_official_host("Aratiba", "portal.pmaratiba.rs.gov.br")
    assert entry is not None
    assert entry["host"] == "pmaratiba.rs.gov.br"


def test_registry_official_host_matches_accented_and_connector_municipio_names() -> None:
    assert authority.registry_official_host(
        "Almirante Tamandaré do Sul", "almirantetamandaredosul.rs.gov.br"
    ) is not None
    # Sin acento / distinta caja tambien debe matchear (cascade.norm()).
    assert authority.registry_official_host(
        "almirante tamandare do sul", "almirantetamandaredosul.rs.gov.br"
    ) is not None
    assert authority.registry_official_host(
        "Caxias do Sul", "www.caxias.rs.gov.br"
    ) is not None


def test_registry_official_host_does_not_match_unrelated_host_for_ararica() -> None:
    # Regresion: atende.net/canoas es el host de OTRO municipio (Canoas no
    # esta delegado a atende.net en el registro); Ararica no debe matchearlo.
    assert authority.registry_official_host("Araricá", "canoas.atende.net") is None
    assert authority.registry_official_host("Araricá", "atende.net") is None


def test_registry_official_host_returns_none_for_municipio_outside_registry() -> None:
    assert authority.registry_official_host(
        "Municipio Que No Existe", "example.com.br"
    ) is None


def test_registry_provenance_emits_official_brand_when_host_matches() -> None:
    provenance = authority.registry_provenance(
        "Aratiba",
        "https://www.pmaratiba.rs.gov.br/concurso/categoria/25/concurso/",
    )
    assert provenance
    assert provenance[0]["kind"] == "official_brand"
    assert provenance[0]["municipio"] == "Aratiba"
    assert cascade._provenance_confirms(provenance, "Aratiba") is True


def test_registry_provenance_empty_when_host_does_not_match() -> None:
    assert authority.registry_provenance("Aratiba", "https://example.com/x") == ()


def test_structural_candidate_registry_confirms_identity_for_non_standard_slug() -> None:
    """Aratiba (host pmaratiba.rs.gov.br, prefijo 'pm' no estandar): la
    identidad por slug de Tier 0 rechazaria este host (slugify('Aratiba')=
    'aratiba' no esta en ['www','pmaratiba']), pero el registro lo confirma
    como HECHO curado -- clase 1 del QA 20260712."""
    snapshot = cascade.EvidenceSnapshot(
        html=(
            "<html><title>Concurso - Prefeitura Municipal de Aratiba</title>"
            "<body>Prefeitura Municipal de Aratiba. Concurso Publico "
            "Edital 01/2026 em andamento.</body></html>"
        ),
        text=(
            "Prefeitura Municipal de Aratiba. Concurso Publico Edital "
            "01/2026 em andamento."
        ),
        title="Concurso - Prefeitura Municipal de Aratiba",
        final_url="https://www.pmaratiba.rs.gov.br/concurso/categoria/25/concurso/",
        requested_url="https://www.pmaratiba.rs.gov.br/concurso/categoria/25/concurso/",
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio="Aratiba",
        bucket="concurso_publico",
        evidence=snapshot,
    )

    assert candidate.identity == "confirmada"
    assert candidate.authority == "confirmada"


def test_structural_candidate_registry_confirms_identity_for_thin_spa_shell() -> None:
    """Aceguá (atende.net, shell SPA client-rendered): el QA 20260712 mide
    45-67 chars de texto visible sin nombre del municipio -- el certificador A
    no tendria nada que citar por contenido. El registro confirma identidad y
    autoridad por el HECHO host<->municipio; el contenido vacio queda
    intacto para que A lo adjudique (no se inventa texto)."""
    thin_text = "Carregando conteudo. Aguarde um instante, por favor."  # 53 chars
    snapshot = cascade.EvidenceSnapshot(
        html=f"<html><title>Portal do Cidadao</title><body>{thin_text}</body></html>",
        text=thin_text,
        title="Portal do Cidadao",
        final_url="https://acegua.atende.net/transparencia/item/processos-seletivos",
        requested_url="https://acegua.atende.net/transparencia/item/processos-seletivos",
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio="Aceguá",
        bucket="processo_seletivo",
        evidence=snapshot,
    )

    assert candidate.identity == "confirmada"
    assert candidate.authority == "confirmada"
    # El contenido crudo no se toca -- sigue sin mencionar el municipio, para
    # que sea A (no el codigo) quien adjudique sobre el.
    assert "acegu" not in cascade.norm(candidate.evidence_snapshot.text)


def test_structural_candidate_soft_404_rejects_identity_even_with_registry_host() -> None:
    """El registro NUNCA salva un soft-404: sigue siendo un hecho objetivo
    mas fuerte que el binding host<->municipio curado."""
    snapshot = cascade.EvidenceSnapshot(
        html=(
            "<html><title>Pagina nao encontrada</title>"
            "<body>Erro 404 - nao encontramos a pagina solicitada.</body></html>"
        ),
        text="Erro 404 - nao encontramos a pagina solicitada.",
        title="Pagina nao encontrada",
        final_url="https://www.pmaratiba.rs.gov.br/concurso/categoria/999/x/",
        requested_url="https://www.pmaratiba.rs.gov.br/concurso/categoria/999/x/",
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio="Aratiba",
        bucket="concurso_publico",
        evidence=snapshot,
    )

    assert candidate.identity == "rechazada"


def test_registry_csv_only_has_provenance_columns_no_decision_labels() -> None:
    """El registro asienta HECHOS (quien/cuando/con que evidencia), nunca la
    decision confirmar/revisar: solo estas 5 columnas, ninguna semantica."""
    with authority._REGISTRY_PATH.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames) == (
            "municipio", "host", "evidencia", "verificado_por", "fecha",
        )
        rows = list(reader)

    assert len(rows) >= 24  # al menos un host por municipio del golden set
    for row in rows:
        assert set(row) == {
            "municipio", "host", "evidencia", "verificado_por", "fecha",
        }
        assert row["municipio"] and row["host"] and row["evidencia"]
        assert row["verificado_por"] and row["fecha"]
