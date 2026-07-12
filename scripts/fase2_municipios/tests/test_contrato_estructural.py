from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as cascade  # noqa: E402
import verdict_extract as verdict  # noqa: E402


def _official(text: str, *, title: str = "Concursos Públicos",
              bucket: str = "concursos") -> verdict.CandidateContract:
    return verdict.evaluate_candidate_contract(
        text, bucket, title=title,
        source_kind="dominio_oficial_prefeitura",
        authority="confirmada", identity="confirmada",
    )


def test_indice_cero_resultados_con_estructura() -> None:
    result = _official(
        "Concursos Públicos\nFormulário de filtro\nBuscar\n"
        "0 resultados encontrados\n"
        "Número | Ano | Modalidade | Objeto | Data da publicação | Detalhes"
    )
    assert result.page_role == "indice_listado"
    assert result.decision == "indice_oficial"


def test_indice_un_resultado_sigue_valido() -> None:
    result = _official(
        "Concursos Públicos\nFormulário de filtro\nBuscar\n"
        "1 resultado encontrado\nConcurso Público 01/2026 para contador\n"
        "Inscrições abertas"
    )
    assert result.page_role == "indice_listado"
    assert result.decision == "indice_oficial"


def test_indice_multiples_resultados() -> None:
    result = _official(
        "Concursos Públicos\nFiltrar\n2 resultados encontrados\n"
        "Concurso Público 01/2025 para contador\n"
        "Concurso Público 02/2026 para médico"
    )
    assert result.page_role == "indice_listado"
    assert result.decision == "indice_oficial"


def test_detalle_individual_con_anexos_rechazado() -> None:
    result = _official(
        "Concurso Público 01/2026\nDocumentos\nEdital de abertura 01/2026\n"
        "Retificação\nResultado final\nHomologação",
        title="Concurso Público 01/2026",
    )
    assert result.page_role == "detalle_individual"
    assert result.decision == "detalle_individual_rechazado"


def test_noticia_numerica_no_es_indice() -> None:
    result = _official(
        "Notícias\n11 de julho de 2026 14:30\nCompartilhe\n"
        "A Prefeitura anunciou 120 vagas e inscrições de 1 a 20 de agosto.\n"
        "Notícias relacionadas",
        title="Prefeitura anuncia concurso com 120 vagas",
    )
    assert result.page_role == "noticia"
    assert result.decision == "nao_encontrado"
    assert result.note == "noticia, no indice"


def test_menu_por_ano_sin_listado_es_revision() -> None:
    result = _official(
        "Concursos Públicos\nConcurso Público 2026\nConcurso Público 2025\n"
        "Concurso Público 2024\nConcurso Público 2023\nSelecione o ano"
    )
    assert result.page_role == "menu_sin_listado"
    assert result.decision == "revisar"
    assert "menu por año" in result.note


def test_indice_combinado() -> None:
    result = _official(
        "Concursos e Processos Seletivos\nFiltrar\n2 resultados encontrados\n"
        "Concurso Público 01/2026 para contador\n"
        "Processo Seletivo 02/2026 para professor",
        title="Concursos e Processos Seletivos",
    )
    assert result.page_role == "indice_combinado"
    assert result.bucket == "combinado"
    assert result.decision == "indice_oficial_combinado"


def _external_page() -> cascade.Page:
    return cascade.Page(
        url="https://portal.example/recursos",
        status=200,
        title="Município de Exemplo - Concursos",
        text=(
            "Município de Exemplo\nConcursos Públicos\nFiltrar\n"
            "1 resultado encontrado\nConcurso Público 01/2026 para contador"
        ),
    )


def test_portal_externo_con_cadena_oficial() -> None:
    provenance = [{
        "kind": "official_navigation", "municipio": "Exemplo",
        "referrer": "https://exemplo.rs.gov.br/concursos",
        "label": "Consulte os concursos no portal",
    }]
    candidate = cascade.candidate_from_evidence(
        _external_page().url, "portal_externo_delegado", "Concursos",
        "Exemplo", _external_page(), provenance=provenance,
    )
    assert candidate.source_kind == "portal_externo_delegado"
    assert candidate.authority == "confirmada"
    assert candidate.identity == "confirmada"
    assert candidate.page_role == "indice_listado"
    assert candidate.decision == "portal_externo_oficial"


def test_portal_externo_sin_cadena_no_se_acepta_por_slug() -> None:
    candidate = cascade.candidate_from_evidence(
        _external_page().url, "portal_externo_delegado", "Concursos",
        "Exemplo", _external_page(), provenance=[],
    )
    assert candidate.source_kind == "portal_externo_delegado"
    assert candidate.authority == "desconocida"
    assert candidate.identity == "confirmada"
    assert candidate.page_role == "indice_listado"
    assert candidate.decision == "revisar"


def test_antibot_conserva_diagnostico_y_snapshot_renderizado_es_evaluable() -> None:
    shell = cascade.Page(
        url="https://exemplo.rs.gov.br/concursos", status=200,
        title="Just a moment",
        text="Checking your browser\nEnable JavaScript\nCloudflare\nAguarde",
        html="<html><body>Checking your browser</body></html>",
        is_antibot=True,
    )
    incomplete = cascade.candidate_from_evidence(
        shell.url, "menu_link", "Concursos", "Exemplo", shell,
    )
    assert incomplete.accessible is True
    assert incomplete.evidence_state == "incompleta_antibot"
    assert incomplete.page_role == "incompleto_antibot"
    assert incomplete.decision == "revisar"
    assert "antibot" in incomplete.note

    snapshot = cascade.EvidenceSnapshot(
        html="<html><body><h1>Concursos</h1></body></html>",
        text=(
            "Prefeitura Municipal de Exemplo\nConcursos Públicos\nFiltrar\n"
            "1 resultado encontrado\nConcurso Público 01/2026 para contador"
        ),
        title="Prefeitura Municipal de Exemplo - Concursos",
        final_url=shell.url,
    )
    rendered = cascade.candidate_from_evidence(
        shell.url, "playwright", "Concursos", "Exemplo", snapshot,
    )
    assert rendered.accessible is True
    assert rendered.evidence_state == "renderizada"
    assert rendered.page_role == "indice_listado"
    assert rendered.decision == "indice_oficial"
