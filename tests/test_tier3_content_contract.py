from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as C  # noqa: E402


MUNICIPIO = "Fixture"
BASE = "https://www.fixture.rs.gov.br"


def _record(path: str, bucket: str, *, combined: bool = False,
            suffix: str = "") -> C.CandidateRecord:
    if combined:
        heading = "Concursos Públicos e Processos Seletivos"
        rows = "Concurso Público 01/2026\nProcesso Seletivo 02/2026"
        hint = "ambos"
    elif bucket == "concursos":
        heading = "Concursos Públicos"
        rows = "Concurso Público 01/2026 para contador"
        hint = "concursos"
    else:
        heading = "Processos Seletivos"
        rows = "Processo Seletivo 02/2026 para professor"
        hint = "processos"
    text = (
        f"Prefeitura Municipal de {MUNICIPIO}\n{heading}\n"
        f"Formulário de filtro\nBuscar\n1 resultado encontrado\n{rows}\n{suffix}"
    )
    url = BASE + path
    snapshot = C.EvidenceSnapshot(
        html=f"<html><body>{text}</body></html>",
        text=text,
        title=f"Prefeitura Municipal de {MUNICIPIO} - {heading}",
        requested_url=url,
        final_url=url,
        status=200,
        source="offline_fixture",
        evidence_state="renderizada",
    )
    return C.build_candidate_record(
        requested_url=url,
        source="offline_fixture",
        tier="tier4",
        municipio=MUNICIPIO,
        bucket_hint=hint,
        evidence=snapshot,
    )


def test_tier3_receives_preadjudicated_records_and_returns_existing_ids() -> None:
    concurso = _record("/concursos-a", "concursos")
    concurso_b = _record("/concursos-b", "concursos", suffix="Todos os anos")
    response = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({
            "candidate_id_concursos": concurso_b.candidate_id,
            "candidate_id_processos": None,
            "razao": "indice de todos os anos",
        })}]}}],
    }
    with patch.object(C, "gemini_post", return_value=response) as post:
        result = C.tier3_classify_and_pick(
            object(), "gemini-2.5-flash", MUNICIPIO, [concurso, concurso_b],
        )

    post.assert_called_once()
    prompt = post.call_args.args[2]["contents"][0]["parts"][0]["text"]
    assert "SOMENTE como seletor" in prompt
    assert "classificacoes" not in prompt.lower()
    assert result["url_concursos"] == concurso_b.final_url
    assert result["selected_resources"]["concursos"].candidate is concurso_b
    assert concurso.page_role == "indice_listado"
    assert concurso_b.page_role == "indice_listado"


def test_single_eligible_record_is_selected_without_ai_call() -> None:
    concurso = _record("/concursos", "concursos")
    with patch.object(C, "gemini_post") as post:
        result = C.tier3_classify_and_pick(
            object(), "gemini-2.5-flash", MUNICIPIO, [concurso],
        )
    post.assert_not_called()
    assert result["url_concursos"] == concurso.final_url
    assert result["decision_concursos"] == "indice_oficial"


def test_nonexistent_selector_id_is_review_with_reason() -> None:
    first = _record("/concursos-a", "concursos")
    second = _record("/concursos-b", "concursos", suffix="Arquivo histórico")
    response = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({
            "candidate_id_concursos": "v1:inexistente",
            "candidate_id_processos": None,
            "razao": "id inválido",
        })}]}}],
    }
    with patch.object(C, "gemini_post", return_value=response):
        result = C.tier3_classify_and_pick(
            object(), "gemini-2.5-flash", MUNICIPIO, [first, second],
        )
    assert result["url_concursos"] == ""
    assert result["decision_concursos"] == "revisar"
    assert "inexistente" in result["razao"]


def test_combined_record_serves_both_only_when_no_specific_page_exists() -> None:
    combined = _record("/editais", "concursos", combined=True)
    result = C.tier3_classify_and_pick(
        object(), "gemini-2.5-flash", MUNICIPIO, [combined],
    )
    assert result["url_concursos"] == combined.final_url
    assert result["url_processos_seletivos"] == combined.final_url
    assert result["decision_concursos"] == "indice_oficial_combinado"
    assert result["decision_processos"] == "indice_oficial_combinado"

    specific = _record("/concursos", "concursos")
    picked = C.resolve_selector_pick(
        [combined, specific], "concurso_publico", specific.candidate_id,
    )
    assert isinstance(picked, C.SelectedResource)
    assert picked.candidate is specific


def test_legacy_route_ignores_ai_classifications() -> None:
    concurso = _record("/concursos", "concursos")
    result = C._route_classified_candidates(
        [concurso],
        [{"id": 0, "page_role": "noticia", "tipo": "pss"}],
    )
    assert result["url_concursos"] == concurso.final_url
    assert result["url_processos_seletivos"] == ""
    assert concurso.page_role == "indice_listado"
    assert concurso.bucket == "concurso_publico"
    assert "ignoradas" in result["razao"]


def test_incomplete_later_tier_does_not_erase_selected_resource() -> None:
    concurso = _record("/concursos", "concursos")
    selected = C.SelectedResource("concurso_publico", concurso)
    complete = {
        "url_concursos": concurso.final_url,
        "url_processos_seletivos": "",
        "decision_concursos": concurso.decision,
        "decision_processos": "nao_encontrado",
        "classification_complete": True,
        "selected_resources": {"concursos": selected},
        "razao": "seleccion completa",
    }
    incomplete = C._empty_tier3_result()
    home = C.Page(
        url=BASE, status=200,
        title=f"Prefeitura Municipal de {MUNICIPIO}",
        text=f"Prefeitura Municipal de {MUNICIPIO}",
    )
    adapter = C.Candidate(
        url=concurso.final_url, source=concurso.source,
        source_tier=concurso.tier, record=concurso,
        page=concurso.page, evidence_snapshot=concurso.evidence_snapshot,
    )
    with (
        patch.object(C, "tier0_find_site", return_value=home),
        patch.object(C, "tier1_collect_candidates", return_value=[adapter]),
        patch.object(C, "gemini_api_key", return_value="offline-key"),
        patch.object(C, "tier3_classify_and_pick", side_effect=[complete, incomplete]),
        patch.object(C, "_probe_known_index_paths", return_value=[adapter]),
        patch.object(C, "tier2_grounded_search", return_value=[]),
        patch.object(C, "tier2_directed_bucket_search", return_value=[]),
    ):
        result = C.process_municipio(
            object(), MUNICIPIO, "gemini-2.5-flash", use_playwright=False,
        )
    assert result.url_concursos == concurso.final_url
    assert result.confianza_concursos == "confirmado"
    assert result.selected_resources["concursos"].candidate is concurso
