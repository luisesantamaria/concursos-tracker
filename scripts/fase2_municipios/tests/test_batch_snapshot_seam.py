from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


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
              status: int | None = 200,
              requested_url: str = INDEX,
              source: str = "playwright") -> cascade.EvidenceSnapshot:
    return cascade.EvidenceSnapshot(
        html=f"<html><head><title>{title}</title></head><body>{text}</body></html>",
        text=text,
        title=title,
        requested_url=requested_url,
        final_url=url,
        status=status,
        source=source,
    )


def _result(snapshot: cascade.EvidenceSnapshot | None = None) -> cascade.MunicipioResult:
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO,
        site_base=BASE,
        url_concursos=INDEX,
        confianza_concursos="probable",
    )
    if snapshot is not None:
        candidate = cascade.hydrate_candidate(
            cascade.Candidate(url=INDEX, source="test", source_tier="tier4"),
            MUNICIPIO, evidence=snapshot,
        )
        result.selected_evidence["concursos"] = cascade.BucketCandidateEvidence(
            bucket="concursos", candidate=candidate,
            snapshot=candidate.evidence_snapshot,
        )
    return result


SOURCE_TIERS = ["tier1", "tier2_grounded", "tier2_directed", "tier4"]


@pytest.mark.parametrize("source_tier", SOURCE_TIERS)
def test_each_source_preserves_requested_final_and_content(source_tier: str) -> None:
    requested = INDEX + "?origem=busca"
    snapshot = _snapshot(requested_url=requested)

    candidate = cascade.hydrate_candidate(
        cascade.Candidate(url=requested, source="discovery", source_tier=source_tier),
        MUNICIPIO, evidence=snapshot,
    )

    assert candidate.source_tier == source_tier
    assert candidate.evidence_snapshot is not None
    assert candidate.evidence_snapshot.requested_url == requested
    assert candidate.evidence_snapshot.final_url == INDEX
    assert candidate.evidence_snapshot.text == INDEX_TEXT


@pytest.mark.parametrize("source_tier", SOURCE_TIERS)
def test_each_source_batch_uses_snapshot_without_http_or_gemini(source_tier: str) -> None:
    snapshot = _snapshot(source="requests" if source_tier != "tier4" else "playwright")
    candidate = cascade.hydrate_candidate(
        cascade.Candidate(url=INDEX, source="discovery", source_tier=source_tier),
        MUNICIPIO, evidence=snapshot,
    )
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO, site_base=BASE, url_concursos=INDEX,
        confianza_concursos="probable",
    )
    result.selected_evidence["concursos"] = cascade.BucketCandidateEvidence(
        bucket="concursos", candidate=candidate,
        snapshot=candidate.evidence_snapshot,
    )

    with (
        patch.object(cascade, "fetch_page", side_effect=AssertionError("no refetch")) as fetch,
        patch.object(cascade, "batch_gemini_verify") as gemini,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )

    assert result.confianza_concursos == "confirmado"
    fetch.assert_not_called()
    gemini.assert_not_called()


@pytest.mark.parametrize("source_tier", SOURCE_TIERS)
def test_valid_redirect_is_evaluated_on_final_url_and_content(source_tier: str) -> None:
    requested = BASE + "/atalho-concursos"
    snapshot = _snapshot(
        requested_url=requested,
        source="requests" if source_tier != "tier4" else "playwright",
    )
    candidate = cascade.hydrate_candidate(
        cascade.Candidate(
            url=requested, source="discovery", source_tier=source_tier,
        ),
        MUNICIPIO, evidence=snapshot,
    )
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO, site_base=BASE, url_concursos=INDEX,
        confianza_concursos="probable",
    )
    result.selected_evidence["concursos"] = cascade.BucketCandidateEvidence(
        "concursos", candidate, candidate.evidence_snapshot,
    )

    with (
        patch.object(cascade, "fetch_page", side_effect=AssertionError("no refetch")) as fetch,
        patch.object(cascade, "batch_gemini_verify") as gemini,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )

    assert candidate.evidence_snapshot.requested_url == requested
    assert candidate.evidence_snapshot.final_url == INDEX
    assert result.confianza_concursos == "confirmado"
    fetch.assert_not_called()
    gemini.assert_not_called()


def test_complete_static_page_snapshot_confirms_without_refetch() -> None:
    static_text = INDEX_TEXT.replace("\n", " ")
    page = cascade.Page(
        url=INDEX, requested_url=INDEX, status=200,
        title="Prefeitura Municipal de Barros Cassal - Concursos",
        text=static_text,
        html=f"<html><body>{static_text}</body></html>",
    )
    candidate = cascade.hydrate_candidate(
        cascade.Candidate(url=INDEX, source="menu_link", source_tier="tier1"),
        MUNICIPIO, evidence=page,
    )
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO, site_base=BASE, url_concursos=INDEX,
        confianza_concursos="probable",
    )
    result.selected_evidence["concursos"] = cascade.BucketCandidateEvidence(
        "concursos", candidate, candidate.evidence_snapshot,
    )

    with (
        patch.object(cascade, "fetch_page", side_effect=AssertionError("no refetch")) as fetch,
        patch.object(cascade, "batch_gemini_verify") as gemini,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )

    assert candidate.evidence_snapshot.source == "requests"
    assert candidate.evidence_snapshot.evidence_state == "completa"
    assert result.confianza_concursos == "confirmado"
    fetch.assert_not_called()
    gemini.assert_not_called()


INVALID_CASES = [
    ("checkpoint", "Vercel Security Checkpoint\nVerifying you are human\nEnable JavaScript\nAguarde", INDEX),
    ("soft404", "Página não encontrada\nErro 404\nO conteúdo solicitado não existe\nVoltar", INDEX),
    ("other_municipality", INDEX_TEXT.replace("Barros Cassal", "Soledade"),
     "https://www.soledade.rs.gov.br/portal-da-transparencia/concursos-publicos"),
    ("login_redirect", "Acesso restrito\nLogin\nUsuário\nSenha", BASE + "/login"),
]


@pytest.mark.parametrize("source_tier", SOURCE_TIERS)
@pytest.mark.parametrize("case,text,final_url", INVALID_CASES)
def test_invalid_preserved_evidence_never_confirms(
        source_tier: str, case: str, text: str, final_url: str) -> None:
    snapshot = _snapshot(
        text=text, title=case, url=final_url,
        source="requests" if source_tier != "tier4" else "playwright",
    )
    candidate = cascade.hydrate_candidate(
        cascade.Candidate(url=INDEX, source="discovery", source_tier=source_tier),
        MUNICIPIO, evidence=snapshot,
    )
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO, site_base=BASE, url_concursos=INDEX,
        confianza_concursos="probable",
    )
    result.selected_evidence["concursos"] = cascade.BucketCandidateEvidence(
        bucket="concursos", candidate=candidate,
        snapshot=candidate.evidence_snapshot,
    )

    with (
        patch.object(cascade, "fetch_page", return_value=cascade.Page(
            url=INDEX, requested_url=INDEX, status=403, error="legacy diagnostic",
        )),
        patch.object(cascade, "batch_gemini_verify") as gemini,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )

    assert result.confianza_concursos == "revisar"
    gemini.assert_not_called()


def test_bucket_associations_do_not_alias_candidate_state() -> None:
    concurso = cascade.hydrate_candidate(
        cascade.Candidate(url=INDEX, source="grounding", source_tier="tier2_grounded"),
        MUNICIPIO, evidence=_snapshot(),
    )
    before = concurso.evidence_snapshot
    process_url = BASE + "/processos-seletivos"
    process_text = INDEX_TEXT.replace("Concursos Públicos", "Processos Seletivos").replace(
        "Concurso Público", "Processo Seletivo")
    processo = cascade.hydrate_candidate(
        cascade.Candidate(url=process_url, source="playwright", source_tier="tier4"),
        MUNICIPIO, evidence=_snapshot(
            url=process_url, requested_url=process_url, text=process_text,
            title="Prefeitura Municipal de Barros Cassal - Processos Seletivos",
        ),
    )
    result = cascade.MunicipioResult(municipio=MUNICIPIO)
    result.selected_evidence["concursos"] = cascade.BucketCandidateEvidence(
        "concursos", concurso, concurso.evidence_snapshot)
    result.selected_evidence["processos"] = cascade.BucketCandidateEvidence(
        "processos", processo, processo.evidence_snapshot)

    assert result.selected_evidence["concursos"].snapshot == before
    assert result.selected_evidence["concursos"].candidate is not processo
    assert result.selected_evidence["concursos"].snapshot.text == INDEX_TEXT


def test_barros_like_tier2_concursos_and_tier4_processos_stay_separate() -> None:
    concurso = cascade.hydrate_candidate(
        cascade.Candidate(url=INDEX, source="grounding", source_tier="tier2_grounded"),
        MUNICIPIO, evidence=_snapshot(source="requests"),
    )
    process_url = BASE + "/processos-seletivos"
    process_text = INDEX_TEXT.replace("Concursos Públicos", "Processos Seletivos").replace(
        "Concurso Público", "Processo Seletivo")
    processo = cascade.hydrate_candidate(
        cascade.Candidate(url=process_url, source="playwright", source_tier="tier4"),
        MUNICIPIO, evidence=_snapshot(
            url=process_url, requested_url=process_url, text=process_text,
            title="Prefeitura Municipal de Barros Cassal - Processos Seletivos",
        ),
    )
    home = cascade.Page(
        url=BASE, status=200, title="Prefeitura Municipal de Barros Cassal",
        text="Prefeitura Municipal de Barros Cassal",
    )
    picks = [
        {
            "url_concursos": INDEX, "url_processos_seletivos": "",
            "decision_concursos": "indice_oficial",
            "decision_processos": "nao_encontrado",
            "classification_complete": True, "razao": "grounded concursos",
        },
        {
            "url_concursos": INDEX, "url_processos_seletivos": process_url,
            "decision_concursos": "indice_oficial",
            "decision_processos": "indice_oficial",
            "classification_complete": True, "razao": "playwright processos",
        },
    ]
    with (
        patch.object(cascade, "tier0_find_site", return_value=home),
        patch.object(cascade, "tier1_collect_candidates", return_value=[]),
        patch.object(cascade, "_probe_known_index_paths", return_value=[]),
        patch.object(cascade, "tier2_grounded_search", return_value=[concurso]),
        patch.object(cascade, "tier2_directed_bucket_search", return_value=[]),
        patch.object(cascade, "tier4_playwright_collect", return_value=[processo]),
        patch.object(cascade, "tier3_classify_and_pick", side_effect=picks),
        patch.object(cascade, "gemini_api_key", return_value="offline-key"),
        patch.object(cascade, "_deterministic_verify", return_value=False),
    ):
        result = cascade.process_municipio(
            object(), MUNICIPIO, "gemini-2.5-flash", use_playwright=True,
        )

    assert result.selected_evidence["concursos"].snapshot.text == INDEX_TEXT
    assert result.selected_evidence["concursos"].candidate.source_tier == "tier2_grounded"
    assert result.selected_evidence["processos"].snapshot.text == process_text
    assert result.selected_evidence["processos"].candidate.source_tier == "tier4"

    with (
        patch.object(cascade, "fetch_page", side_effect=AssertionError("no refetch")) as fetch,
        patch.object(cascade, "batch_gemini_verify") as gemini,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )

    assert result.confianza_concursos == "confirmado"
    assert result.confianza_processos == "confirmado"
    fetch.assert_not_called()
    gemini.assert_not_called()


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
    assert result.selected_evidence["concursos"].snapshot is rendered
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
    other_snapshot = _snapshot(
        url=other_url, requested_url=other_url, text=other_text,
        title="Prefeitura Municipal de Soledade - Concursos",
    )
    other_candidate = cascade.hydrate_candidate(
        cascade.Candidate(url=other_url, source="test", source_tier="tier4"),
        MUNICIPIO, evidence=other_snapshot,
    )
    result.selected_evidence = {
        "concursos": cascade.BucketCandidateEvidence(
            "concursos", other_candidate, other_candidate.evidence_snapshot,
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
