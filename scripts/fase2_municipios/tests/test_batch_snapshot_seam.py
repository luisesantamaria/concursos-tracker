from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as cascade  # noqa: E402


MUNICIPIO = "Barros Cassal"
BASE = "https://www.barroscassal.rs.gov.br"
INDEX = BASE + "/portal-da-transparencia/concursos-publicos"
INDEX_TEXT = "\n".join([
    "Prefeitura Municipal de Barros Cassal",
    "Concursos Públicos",
    "Formulário de filtro",
    "Buscar",
    "1 resultado encontrado",
    "Concurso Público 01/2026 para contador",
    "Inscrições abertas",
])


def _snapshot(*, url: str = INDEX, text: str = INDEX_TEXT,
              title: str = "Prefeitura Municipal de Barros Cassal - Concursos",
              status: int | None = 200) -> cascade.EvidenceSnapshot:
    return cascade.EvidenceSnapshot(
        html=f"<html><head><title>{title}</title></head><body>{text}</body></html>",
        text=text,
        title=title,
        final_url=url,
        status=status,
        source="playwright",
    )


def _result(snapshot: cascade.EvidenceSnapshot | None = None) -> cascade.MunicipioResult:
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO,
        site_base=BASE,
        url_concursos=INDEX,
        confianza_concursos="probable",
    )
    if snapshot is not None:
        result.evidence_snapshots[cascade._normalized_candidate_url(INDEX)] = snapshot
    return result


def test_valid_rendered_snapshot_confirms_without_second_get() -> None:
    rendered = _snapshot()
    candidate = cascade.candidate_from_evidence(
        INDEX, "playwright", "Concursos Públicos", MUNICIPIO, rendered,
    )
    picked = {
        "url_concursos": INDEX,
        "url_processos_seletivos": "",
        "decision_concursos": "indice_oficial",
        "decision_processos": "nao_encontrado",
        "classification_complete": True,
        "razao": "indice oficial renderizado",
    }
    home = cascade.Page(
        url=BASE, status=200, title="Prefeitura Municipal de Barros Cassal",
        text="Prefeitura Municipal de Barros Cassal",
    )
    with (
        patch.object(cascade, "tier0_find_site", return_value=home),
        patch.object(cascade, "tier1_collect_candidates", return_value=[]),
        patch.object(cascade, "_probe_known_index_paths", return_value=[]),
        patch.object(cascade, "tier2_grounded_search", return_value=[]),
        patch.object(cascade, "tier2_directed_bucket_search", return_value=[]),
        patch.object(cascade, "tier4_playwright_collect", return_value=[candidate]),
        patch.object(cascade, "tier3_classify_and_pick", return_value=picked),
        patch.object(cascade, "gemini_api_key", return_value="offline-key"),
        patch.object(cascade, "_deterministic_verify", return_value=False),
    ):
        result = cascade.process_municipio(
            object(), MUNICIPIO, "gemini-2.5-flash", use_playwright=True,
        )

    assert result.confianza_concursos == "probable"
    assert result.evidence_snapshots[
        cascade._normalized_candidate_url(INDEX)
    ] is rendered
    http_fetch = Mock(side_effect=AssertionError("second GET must not happen"))

    with (
        patch.object(cascade, "fetch_page", http_fetch),
        patch.object(cascade, "batch_gemini_verify") as gemini_verify,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], timeout=15,
            use_playwright=False,
        )

    confidences = [result.confianza_concursos]
    assert sum(value == "confirmado" for value in confidences) == 1
    assert sum(value == "revisar" for value in confidences) == 0
    http_fetch.assert_not_called()
    gemini_verify.assert_not_called()


def test_checkpoint_snapshot_does_not_confirm_by_url() -> None:
    checkpoint = _snapshot(
        text="Vercel Security Checkpoint\nVerifying you are human\nEnable JavaScript\nAguarde",
        title="Vercel Security Checkpoint",
    )
    result = _result(checkpoint)
    session = object()
    http_fetch = Mock(return_value=cascade.Page(
        url=INDEX, status=403, title="Vercel Security Checkpoint",
        text="Verifying you are human",
    ))

    with (
        patch.object(cascade, "fetch_page", http_fetch),
        patch.object(cascade, "batch_gemini_verify", return_value={
            f"{MUNICIPIO}|concursos": ("revisar", "checkpoint sem evidencia"),
        }),
    ):
        cascade._batch_verify_uncertain_results(
            session, "gemini-2.5-flash", [result], timeout=15,
            use_playwright=False,
        )

    assert result.confianza_concursos == "revisar"
    http_fetch.assert_called_once_with(session, INDEX, timeout=15)


def test_missing_snapshot_keeps_legacy_fetch_path() -> None:
    result = _result()
    http_page = cascade.Page(
        url=INDEX, status=200,
        title="Prefeitura Municipal de Barros Cassal - Concursos",
        text=INDEX_TEXT,
    )
    session = object()

    with (
        patch.object(cascade, "fetch_page", return_value=http_page) as http_fetch,
        patch.object(cascade, "batch_gemini_verify", return_value={
            f"{MUNICIPIO}|concursos": ("confirmado", "conteudo legado valido"),
        }) as gemini_verify,
    ):
        cascade._batch_verify_uncertain_results(
            session, "gemini-2.5-flash", [result], timeout=15,
            use_playwright=False,
        )

    assert result.confianza_concursos == "confirmado"
    http_fetch.assert_called_once_with(session, INDEX, timeout=15)
    gemini_verify.assert_called_once()


def test_snapshot_from_other_municipality_fails_identity_gate() -> None:
    other_url = "https://www.soledade.rs.gov.br/portal-da-transparencia/concursos-publicos"
    other_text = INDEX_TEXT.replace("Barros Cassal", "Soledade")
    result = _result(_snapshot(url=other_url, text=other_text,
                               title="Prefeitura Municipal de Soledade - Concursos"))
    result.url_concursos = other_url
    result.evidence_snapshots = {
        cascade._normalized_candidate_url(other_url): _snapshot(
            url=other_url, text=other_text,
            title="Prefeitura Municipal de Soledade - Concursos",
        ),
    }
    http_fetch = Mock(side_effect=AssertionError("usable snapshot must be adjudicated"))

    with (
        patch.object(cascade, "fetch_page", http_fetch),
        patch.object(cascade, "batch_gemini_verify") as gemini_verify,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], timeout=15,
            use_playwright=False,
        )

    assert result.confianza_concursos == "revisar"
    http_fetch.assert_not_called()
    gemini_verify.assert_not_called()
