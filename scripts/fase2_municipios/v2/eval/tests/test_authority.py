"""Offline coverage for redirect-based general authority provenance.

Caso real (Porto Alegre, golden36): la cadena HTTP es
https://www.portoalegre.rs.gov.br/smap/servicos/selecao-e-provimento/concursos-em-andamento
-> 301/302 x4 -> https://prefeitura.poa.br/smap (200). El host de destino no
es *.rs.gov.br, pero el host de ORIGEN si es el dominio oficial confirmado
del municipio: eso es la unica evidencia estructural que este modulo emite.
"""

from __future__ import annotations

import csv
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Mision D (12-jul) -- cobertura de autoridad en municipios NUEVOS: replay
# offline de holdout50_20260712 (eval/replay_final_gate.py) confirmo 39/41
# unidades donde A certifico y B sostuvo, pero el gate rechazo por
# authority='desconocida' -- nunca por un defecto de C pisando el consenso
# (esa hipotesis quedo descartada). Reglas nuevas: (a) plataforma delegada
# (atende.net/multi24h.com.br) y (b) universo (site_base).
# ---------------------------------------------------------------------------


def test_municipio_concat_slug_keeps_connector_words() -> None:
    """Distinto de cascade.slugify (que quita da/de/do/das/dos): la
    convencion real de atende.net/multi24h.com.br concatena TODAS las
    palabras, conectores incluidos (arroiodosal, saopedrodosul,
    cristaldosul, saodomingosdosul)."""
    assert authority._municipio_concat_slug("Arroio Do Sal") == "arroiodosal"
    assert authority._municipio_concat_slug("São Pedro Do Sul") == "saopedrodosul"
    assert authority._municipio_concat_slug("Cristal Do Sul") == "cristaldosul"
    assert authority._municipio_concat_slug("São Domingos Do Sul") == "saodomingosdosul"
    assert authority._municipio_concat_slug("Dois Irmãos") == "doisirmaos"
    # Contraste explicito con cascade.slugify (que SI quita el conector):
    assert cascade.slugify("Arroio Do Sal") == "arroiosal"


@pytest.mark.parametrize(
    ("host", "expected_slug_source"),
    [
        ("cristaldosul.atende.net", "Cristal Do Sul"),
        ("www.cristaldosul.atende.net", "Cristal Do Sul"),
        ("doisirmaos.atende.net", "Dois Irmãos"),
        ("saopedrodosul.multi24h.com.br", "São Pedro Do Sul"),
        ("pmfloresdacunha.multi24h.com.br", "Flores Da Cunha"),
    ],
)
def test_delegated_platform_provenance_matches_correct_slug(
    host: str, expected_slug_source: str,
) -> None:
    provenance = authority.delegated_platform_provenance(
        expected_slug_source, f"https://{host}/cidadao/pagina/concursos",
    )
    assert provenance
    assert provenance[0]["kind"] == "official_brand"
    assert provenance[0]["label"] == "plataforma_delegada"
    assert cascade._provenance_confirms(provenance, expected_slug_source) is True


def test_delegated_platform_provenance_accepts_pm_plus_exact_slug() -> None:
    provenance = authority.delegated_platform_provenance(
        "Flores Da Cunha", "https://pmfloresdacunha.multi24h.com.br/x",
    )
    assert provenance
    assert provenance[0]["label"] == "plataforma_delegada"


def test_delegated_platform_pm_prefix_rejects_other_municipality_slug() -> None:
    assert authority.delegated_platform_provenance(
        "Flores Da Cunha", "https://pmoutracidade.multi24h.com.br/x",
    ) == ()


def test_delegated_platform_pm_prefix_adversarial_cross_municipio() -> None:
    assert authority.delegated_platform_provenance(
        "Outra Cidade", "https://pmfloresdacunha.multi24h.com.br/x",
    ) == ()


def test_delegated_platform_provenance_empty_for_unrelated_host() -> None:
    assert authority.delegated_platform_provenance(
        "Cristal Do Sul", "https://cristaldosul.rs.gov.br/x",
    ) == ()
    assert authority.delegated_platform_provenance(
        "Cristal Do Sul", "https://example.com/x",
    ) == ()


def test_structural_candidate_delegated_platform_confirms_authority_only() -> None:
    """Caso real Dois Irmaos: A certifico, B sostuvo, el gate rechazaba por
    authority='desconocida' (doisirmaos.atende.net no esta en el registro
    curado). La nueva provenance de plataforma delegada confirma autoridad
    cuando el contenido de la propia pagina TAMBIEN confirma identidad."""
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text=(
            "Prefeitura Municipal de Dois Irmãos. Concurso Público Edital "
            "01/2026 em andamento. Lista de vagas e cronograma."
        ),
        title="Concursos - Prefeitura de Dois Irmãos",
        final_url="https://doisirmaos.atende.net/cidadao/pagina/concurso-publico",
        requested_url="https://doisirmaos.atende.net/cidadao/pagina/concurso-publico",
        status=200,
        source="orion_http",
        evidence_state="completa",
    )
    provenance = authority.delegated_platform_provenance(
        "Dois Irmãos", snapshot.final_url,
    )
    assert provenance

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio="Dois Irmãos",
        bucket="concurso_publico",
        evidence=snapshot,
        provenance=provenance,
    )

    assert candidate.authority == "confirmada"
    assert candidate.source_kind == "portal_externo_delegado"
    assert candidate.identity == "confirmada"


def test_delegated_platform_provenance_never_launders_identity_for_homonym() -> None:
    """Seguridad anti-FP critica (Mision D): atende.net/multi24h.com.br
    sirven municipios de TODO Brasil y existen homonimos entre UFs. Mismo
    slug/host, pero el CONTENIDO real es de un municipio distinto (nunca
    menciona el municipio objetivo) -- la identidad NO debe confirmarse solo
    porque el slug matcheo la plataforma delegada. El contenido tambien
    declara su propia UF (Parana) sin mencionar RS, asi que ahora TAMPOCO
    la autoridad se confirma (endurecido tras revision adversarial -- ver
    ``test_delegated_platform_provenance_denies_authority_for_exact_homonym_
    other_state`` para el caso mas peligroso, donde el nombre SI coincide)."""
    municipio_objetivo = "Formosa"
    final_url = "https://formosa.atende.net/cidadao/pagina/concursos"

    # Contenido real de un municipio homonimo DISTINTO, sin ninguna relacion
    # textual con "Formosa" (deliberado: evita colisiones de substring como
    # "Progresso" dentro de "Progresso do Iracema", que ya son ambiguas para
    # el propio chequeo de contenido de cascade -- lo que este test aisla es
    # la garantia de esta provenance, no esa heuristica preexistente).
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text=(
            "Prefeitura Municipal de Realeza, Estado do Paraná. "
            "Concurso Público Edital 02/2026."
        ),
        title="Concursos - Prefeitura de Realeza/PR",
        final_url=final_url,
        requested_url=final_url,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    # Sin ``page`` la funcion pura solo mira el slug (autoridad de canal, sin
    # opinion de contenido) -- eso sigue matcheando.
    assert authority.delegated_platform_provenance(municipio_objetivo, final_url)

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio=municipio_objetivo,
        bucket="concurso_publico",
        evidence=snapshot,
    )

    # Ni autoridad ni identidad se confirman: el contenido real declara
    # explicitamente otra UF (Parana) sin mencionar RS, y ademas nunca
    # menciona "Formosa" -- el gate estructural (authority+identity) rechaza
    # la confirmacion por las dos vias.
    assert candidate.authority != "confirmada"
    assert candidate.identity != "confirmada"


def test_delegated_platform_provenance_denies_authority_for_exact_homonym_other_state() -> None:
    """El caso realmente peligroso (revision adversarial, Mision D
    endurecida): un homonimo EXACTO -- misma cadena de nombre municipal en
    OTRA UF (p.ej. "Bom Jesus" existe en RS, PI y SC) -- hace que el
    chequeo de identidad por contenido de ``cascade._candidate_identity_
    state`` sea ciego: el nombre objetivo SI aparece en la pagina, porque la
    otra ciudad se llama exactamente igual. Antes de este endurecimiento
    eso bastaba para confirmar authority (via el slug) E identity (via el
    nombre) -- un falso positivo cross-UF silencioso. Ahora, con el
    contenido real disponible, la autoridad se niega porque la pagina
    declara su propia UF (Piaui) sin mencionar RS."""
    municipio_objetivo = "Bom Jesus"
    final_url = "https://bomjesus.atende.net/cidadao/pagina/concursos"

    snapshot = cascade.EvidenceSnapshot(
        html="",
        text=(
            "Prefeitura Municipal de Bom Jesus, Estado do Piauí. "
            "Concurso Público Edital 01/2026 em andamento."
        ),
        title="Concursos - Prefeitura de Bom Jesus/PI",
        final_url=final_url,
        requested_url=final_url,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    page = cascade._page_from_html(
        final_url, snapshot.status, "text/html; charset=UTF-8", "",
        requested_url=final_url,
    )
    page.title, page.text = snapshot.title, snapshot.text

    # El nombre SI coincide -- por eso el chequeo de identidad por contenido
    # es ciego a este homonimo (queda documentado, no relajado).
    assert cascade._candidate_identity_state(page, municipio_objetivo, ()) == "confirmada"
    # El slug tambien matchea -- por eso la funcion pura (sin page) SI emite
    # provenance de autoridad.
    assert authority.delegated_platform_provenance(municipio_objetivo, final_url)
    # Pero con el contenido real disponible, la autoridad se niega: declara
    # Piaui, nunca RS.
    assert authority.delegated_platform_provenance(
        municipio_objetivo, final_url, page,
    ) == ()

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio=municipio_objetivo,
        bucket="concurso_publico",
        evidence=snapshot,
    )

    # Identity confirma (nombre identico -- esperado, documentado arriba),
    # pero authority queda denegada -- el gate estructural (exige AMBAS)
    # sigue rechazando la confirmacion del homonimo de otra UF.
    assert candidate.identity == "confirmada"
    assert candidate.authority != "confirmada"


def test_universe_site_base_host_and_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authority, "_universe_cache",
        {"acme municipio": "acme.rs.gov.br"},
    )
    assert authority.universe_site_base_host("Acme Municipio") == "acme.rs.gov.br"
    assert authority.universe_site_base_host("Outro Municipio") == ""
    assert authority.universe_site_base_match(
        "Acme Municipio", "https://www.acme.rs.gov.br/concursos",
    ) is True
    assert authority.universe_site_base_match(
        "Acme Municipio", "https://outrodominio.rs.gov.br/concursos",
    ) is False
    # Mismo host, municipio DISTINTO al que lo tiene registrado -- no matchea
    # (la clave del lookup es el propio municipio, sin fuga cruzada).
    assert authority.universe_site_base_match(
        "Municipio Homonimo", "https://www.acme.rs.gov.br/concursos",
    ) is False


def test_universe_provenance_authority_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authority, "_universe_cache",
        {"acme municipio": "pmacme.com.br"},
    )
    provenance = authority.universe_provenance(
        "Acme Municipio", "https://pmacme.com.br/site/concursos",
    )
    assert provenance
    assert provenance[0]["kind"] == "official_brand"
    assert provenance[0]["label"] == "universo_site_base"
    assert cascade._provenance_confirms(provenance, "Acme Municipio") is True
    assert authority.universe_provenance(
        "Acme Municipio", "https://outrodominio.com.br/site/concursos",
    ) == ()


def test_universe_site_base_normalizes_scheme_www_port_and_pm_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority, "_universe_cache",
        {"acme municipio": "acmemunicipio.portal.gov.br"},
    )
    assert authority.universe_site_base_match(
        "Acme Municipio", "http://www.pmacmemunicipio.portal.gov.br:8443/x",
    ) is True


def test_universe_site_base_pm_alias_rejects_different_parent_or_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority, "_universe_cache",
        {"acme municipio": "acmemunicipio.portal.gov.br"},
    )
    assert authority.universe_site_base_match(
        "Acme Municipio", "https://pmacmemunicipio.outro.gov.br/x",
    ) is False
    assert authority.universe_site_base_match(
        "Acme Municipio", "https://pmoutracidade.portal.gov.br/x",
    ) is False


def test_universe_site_base_pm_alias_adversarial_other_municipio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority, "_universe_cache",
        {"outro municipio": "outromunicipio.portal.gov.br"},
    )
    assert authority.universe_site_base_match(
        "Outro Municipio", "https://pmacmemunicipio.portal.gov.br/x",
    ) is False


def test_universe_confirmed_bucket_url_matches_html_and_percent_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_url = "http://198.51.100.7:8080/portal?entidade=1&amp;id=13099"
    monkeypatch.setattr(
        authority,
        "_universe_confirmed_url_cache",
        {
            ("acme municipio", "concurso_publico"): (
                authority._normalize_universe_url(csv_url),
            ),
        },
    )
    assert authority.universe_confirmed_url_match(
        "Acme Municipio",
        "http://198.51.100.7:8080/portal?entidade=1&amp%3Bid=13099",
        "concurso_publico",
    ) is True


def test_universe_confirmed_bucket_url_rejects_different_query_or_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirmed = authority._normalize_universe_url(
        "http://198.51.100.7:8080/portal?entidade=1&id=13099"
    )
    monkeypatch.setattr(
        authority,
        "_universe_confirmed_url_cache",
        {("acme municipio", "concurso_publico"): (confirmed,)},
    )
    assert authority.universe_confirmed_url_match(
        "Acme Municipio",
        "http://198.51.100.7:8080/portal?entidade=2&id=13099",
        "concurso_publico",
    ) is False
    assert authority.universe_confirmed_url_match(
        "Acme Municipio",
        "http://198.51.100.7:8080/portal?entidade=1&id=13099",
        "processo_seletivo",
    ) is False


def test_universe_confirmed_bucket_url_adversarial_same_ip_other_municipio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirmed = authority._normalize_universe_url(
        "http://198.51.100.7:8080/portal?entidade=1&id=13099"
    )
    monkeypatch.setattr(
        authority,
        "_universe_confirmed_url_cache",
        {("acme municipio", "concurso_publico"): (confirmed,)},
    )
    assert authority.universe_confirmed_url_match(
        "Outro Municipio",
        "http://198.51.100.7:8080/portal?entidade=1&id=13099",
        "concurso_publico",
    ) is False


def test_universe_identity_confirms_requires_both_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority, "_universe_cache",
        {"acme municipio": "acme.rs.gov.br"},
    )
    page_with_content = cascade.Page(
        url="https://acme.rs.gov.br/x", status=200,
        title="Concursos - Prefeitura de Acme Municipio",
        text="Prefeitura Municipal de Acme Municipio. Concurso Publico.",
    )
    page_without_content = cascade.Page(
        url="https://acme.rs.gov.br/x", status=200,
        title="Concursos",
        text="Lista de editais e cronograma.",
    )

    # Ambos hechos presentes -> confirma.
    assert authority.universe_identity_confirms(
        "Acme Municipio", "https://acme.rs.gov.br/x", page_with_content,
    ) is True
    # site_base matchea pero el contenido NUNCA menciona el municipio -> NO.
    assert authority.universe_identity_confirms(
        "Acme Municipio", "https://acme.rs.gov.br/x", page_without_content,
    ) is False
    # El contenido si menciona el municipio pero el host NO es el site_base
    # registrado -> NO (el anclaje estructural es obligatorio).
    assert authority.universe_identity_confirms(
        "Acme Municipio", "https://otrodominio.rs.gov.br/x", page_with_content,
    ) is False


def test_structural_identity_unescapes_html_entity_before_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_url = "https://portal-acme.example/concursos"
    monkeypatch.setattr(authority, "_universe_cache", {})
    monkeypatch.setattr(
        authority,
        "_universe_confirmed_url_cache",
        {
            ("acme municipio", "concurso_publico"): (
                authority._normalize_universe_url(final_url),
            ),
        },
    )
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text="Prefeitura de Acme Munic&#237;pio. Concurso Publico 01/2026.",
        title="Acme Munic&#237;pio",
        final_url=final_url,
        requested_url=final_url,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=final_url,
        source="orion_http",
        tier="live",
        municipio="Acme Municipio",
        bucket="concurso_publico",
        evidence=snapshot,
    )

    assert candidate.identity == "confirmada"
    assert candidate.authority == "confirmada"


def test_structural_identity_entity_decode_does_not_replace_missing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_url = "https://portal-acme.example/concursos"
    monkeypatch.setattr(authority, "_universe_cache", {})
    monkeypatch.setattr(
        authority,
        "_universe_confirmed_url_cache",
        {
            ("acme municipio", "concurso_publico"): (
                authority._normalize_universe_url(final_url),
            ),
        },
    )
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text="Lista de editais e cronogramas.",
        title="Concursos P&#250;blicos",
        final_url=final_url,
        requested_url=final_url,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )
    candidate = structural_candidate(
        requested_url=final_url,
        source="orion_http",
        tier="live",
        municipio="Acme Municipio",
        bucket="concurso_publico",
        evidence=snapshot,
    )
    assert candidate.identity != "confirmada"


def test_structural_identity_entity_adversarial_other_name_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_url = "https://portal-acme.example/concursos"
    monkeypatch.setattr(authority, "_universe_cache", {})
    monkeypatch.setattr(
        authority,
        "_universe_confirmed_url_cache",
        {
            ("acme municipio", "concurso_publico"): (
                authority._normalize_universe_url(final_url),
            ),
        },
    )
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text="Prefeitura de Outra Cid&#225;de. Concurso Publico 01/2026.",
        title="Outra Cid&#225;de",
        final_url=final_url,
        requested_url=final_url,
        status=200,
        source="orion_http",
        evidence_state="completa",
    )
    candidate = structural_candidate(
        requested_url=final_url,
        source="orion_http",
        tier="live",
        municipio="Acme Municipio",
        bucket="concurso_publico",
        evidence=snapshot,
    )
    assert candidate.identity != "confirmada"


def test_structural_candidate_universe_confirms_identity_for_connector_slug_mismatch() -> None:
    """Caso real Arroio Do Sal: host arroiodosal.rs.gov.br retiene el
    conector "do" que cascade.slugify descarta, asi que
    cascade._candidate_identity_state rechaza el candidato ANTES de leer su
    contenido. site_base (universo) + mencion real del municipio en el
    contenido confirman identidad sin relajar el chequeo de contenido."""
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text=(
            "Prefeitura Municipal de Arroio do Sal. Concurso Público Edital "
            "01/2024. Lista de vagas e cronograma."
        ),
        title="Prefeitura de Arroio do Sal - Concursos",
        final_url="https://arroiodosal.rs.gov.br/category/publicacoes-oficiais/concursos/",
        requested_url="https://arroiodosal.rs.gov.br/category/publicacoes-oficiais/concursos/",
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    # Precondicion documentada por el bug: cascade rechaza por slug ANTES de
    # nuestro fix (slugify quita el conector "do"; el host lo retiene).
    assert cascade.slugify("Arroio Do Sal") == "arroiosal"
    page = cascade._page_from_html(
        snapshot.final_url, snapshot.status, "text/html; charset=UTF-8", "",
        requested_url=snapshot.requested_url,
    )
    page.title, page.text = snapshot.title, snapshot.text
    assert cascade._candidate_identity_state(page, "Arroio Do Sal", ()) == "rechazada"

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio="Arroio Do Sal",
        bucket="concurso_publico",
        evidence=snapshot,
    )

    assert candidate.identity == "confirmada"
    assert candidate.authority == "confirmada"
    assert candidate.source_kind == "dominio_oficial_prefeitura"


def test_structural_candidate_universe_site_base_alone_does_not_confirm_identity() -> None:
    """El host del universo (site_base) NUNCA basta solo: si el contenido de
    la pagina no menciona el municipio, la identidad sigue sin confirmar --
    regla (b) exige los dos hechos, nunca uno solo."""
    snapshot = cascade.EvidenceSnapshot(
        html="",
        text="Carregando... aguarde um instante.",
        title="Portal",
        final_url="https://arroiodosal.rs.gov.br/x/y",
        requested_url="https://arroiodosal.rs.gov.br/x/y",
        status=200,
        source="orion_http",
        evidence_state="completa",
    )

    candidate = structural_candidate(
        requested_url=snapshot.requested_url,
        source="orion_http",
        tier="live",
        municipio="Arroio Do Sal",
        bucket="concurso_publico",
        evidence=snapshot,
    )

    assert candidate.identity != "confirmada"


def test_universe_csv_real_row_matches_arroio_do_sal_site_base() -> None:
    """Smoke test contra el archivo real (no un fixture): confirma que el
    loader lee correctamente data/fase2/municipios_rs_local.csv, con el
    mismo patron que los tests del registro contra dominios_oficiales_rs.csv."""
    assert authority.universe_site_base_host("Arroio Do Sal") == "arroiodosal.rs.gov.br"


def test_universe_site_base_requires_pipeline_confirmed_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``municipios_rs_local.csv`` es SALIDA del pipeline (method/confianza/
    razao/checked_at), no un registro curado a mano: solo debe alimentar
    autoridad/identidad cuando la MISMA fila muestra que el pipeline
    confirmo al menos un bucket para ese municipio. Sin este gate, una fila
    con site_base adivinado (Tier-2 grounded, sin ningun candidato
    fetcheable) se volveria fuente de autoridad+identidad para cualquier
    pagina que este modulo reciba, sin importar cuan generico sea el host."""
    csv_path = tmp_path / "universo.csv"
    csv_path.write_text(
        "uf,municipio,site_base,confianza_concursos,confianza_processos,notes\n"
        "RS,Acme Confirmado,https://acme.rs.gov.br,confirmado,,\n"
        "RS,Acme Sin Confirmar,https://directorio-generico.com.br,,,"
        "no valid index page found\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(authority, "_UNIVERSE_PATH", csv_path)
    monkeypatch.setattr(authority, "_universe_cache", None)

    assert authority.universe_site_base_host("Acme Confirmado") == "acme.rs.gov.br"
    assert authority.universe_site_base_host("Acme Sin Confirmar") == ""


def test_universe_csv_real_row_excludes_unconfirmed_muliterno_site_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke test contra el archivo real: la fila de Muliterno tiene
    site_base=https://www.cidade-brasil.com.br (un directorio nacional
    generico de ciudades, no una prefeitura) con confianza_concursos y
    confianza_processos VACIAS y notes="no valid index page found" -- el
    pipeline nunca confirmo nada para este municipio. El loader debe
    excluirla, no tratarla como hecho estructural."""
    monkeypatch.setattr(authority, "_universe_cache", None)
    assert authority.universe_site_base_host("Muliterno") == ""
