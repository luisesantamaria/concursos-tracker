from __future__ import annotations

import logging
import sys
from dataclasses import FrozenInstanceError
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


def snapshot(
        text: str = INDEX_TEXT, *, requested_url: str = INDEX,
        final_url: str = INDEX, title: str | None = None,
        source: str = "playwright", status: int | None = 200,
        links=()) -> cascade.EvidenceSnapshot:
    page_title = title or "Prefeitura Municipal de Barros Cassal - Concursos"
    return cascade.EvidenceSnapshot(
        html=f"<html><head><title>{page_title}</title></head><body>{text}</body></html>",
        text=text,
        title=page_title,
        requested_url=requested_url,
        final_url=final_url,
        status=status,
        source=source,
        evidence_state="renderizada" if source == "playwright" else "completa",
        links=links,
    )


def record(
        *, evidence: cascade.EvidenceSnapshot | None = None,
        tier: str = "tier4", bucket_hint: str = "concursos",
        source: str = "playwright") -> cascade.CandidateRecord:
    evidence = evidence or snapshot(source=source)
    return cascade.build_candidate_record(
        requested_url=evidence.requested_url,
        source=source,
        tier=tier,
        municipio=MUNICIPIO,
        bucket_hint=bucket_hint,
        evidence=evidence,
    )


def final_for(candidate: cascade.CandidateRecord,
              bucket: str = "concurso_publico") -> cascade.FinalDecision:
    selected = cascade.SelectedResource(bucket=bucket, candidate=candidate)
    return cascade.derive_final_decision(selected)


@pytest.mark.parametrize(
    ("tier", "source"),
    [
        ("tier1", "menu_link"),
        ("tier2_grounded", "grounding"),
        ("tier2_directed", "grounding"),
        ("tier4", "playwright"),
    ],
)
def test_each_origin_keeps_exact_snapshot_and_finalizes_without_refetch(
        tier: str, source: str) -> None:
    evidence = snapshot(source="playwright" if tier == "tier4" else "requests")
    candidate = record(evidence=evidence, tier=tier, source=source)
    selected = cascade.SelectedResource("concurso_publico", candidate)

    with patch.object(
            cascade.requests.sessions.Session, "get",
            side_effect=AssertionError("FinalDecision must not refetch"),
    ) as get:
        final = cascade.derive_final_decision(selected)

    assert selected.candidate is candidate
    assert candidate.evidence_snapshot is evidence
    assert final.status == "confirmado"
    assert final.url == INDEX
    assert final.reason
    get.assert_not_called()


@pytest.mark.parametrize(
    ("text", "confirmed"),
    [
        (
            "Prefeitura Municipal de Barros Cassal\nConcursos Públicos\n"
            "Formulário de filtro\nBuscar\n0 resultados encontrados\n"
            "Número | Ano | Modalidade | Objeto | Data da publicação | Detalhes",
            True,
        ),
        ("Prefeitura Municipal de Barros Cassal\nConcursos Públicos\n0 resultados", False),
        (INDEX_TEXT, True),
        (
            INDEX_TEXT.replace("1 resultado encontrado", "2 resultados encontrados")
            + "\nConcurso Público 02/2025 para médico",
            True,
        ),
    ],
)
def test_index_zero_one_multiple_contract_reaches_final_decision(
        text: str, confirmed: bool) -> None:
    final = final_for(record(evidence=snapshot(text)))
    assert (final.status == "confirmado") is confirmed
    assert final.reason


def test_barros_vercel_checkpoint_refetch_cannot_replace_rendered_snapshot(
        caplog: pytest.LogCaptureFixture) -> None:
    rendered = snapshot()
    checkpoint = cascade.Page(
        url=INDEX, requested_url=INDEX, status=403,
        title="Vercel Security Checkpoint",
        text="Verifying you are human; Enable JavaScript",
        html="<html><body>Vercel Security Checkpoint</body></html>",
    )
    candidate = record(evidence=rendered)
    selected = cascade.SelectedResource("concurso_publico", candidate)

    caplog.set_level(logging.INFO, logger="fase2.cascade")
    with patch.object(cascade, "fetch_page", return_value=checkpoint) as refetch:
        final = cascade.derive_final_decision(selected)

    assert selected.candidate.evidence_snapshot is rendered
    assert final.status == "confirmado"
    assert final.decision == "indice_oficial"
    assert final.reason
    refetch.assert_not_called()
    assert any("final_decision" in message and candidate.candidate_id in message
               for message in caplog.messages)


def test_candidate_id_is_stable_versioned_and_distinguishes_bucket_and_snapshot() -> None:
    first = record()
    same = record()
    other_bucket = record(
        evidence=snapshot(
            INDEX_TEXT.replace("Concursos Públicos", "Processos Seletivos")
            .replace("Concurso Público", "Processo Seletivo"),
            final_url=BASE + "/processos-seletivos",
            requested_url=BASE + "/processos-seletivos",
            title="Prefeitura Municipal de Barros Cassal - Processos Seletivos",
        ),
        bucket_hint="processos",
    )
    changed_snapshot = record(evidence=snapshot(INDEX_TEXT + "\nAtualizado hoje"))

    assert first.candidate_id.startswith("v1:")
    assert first.candidate_id == same.candidate_id
    assert first.candidate_id != other_bucket.candidate_id
    assert first.candidate_id != changed_snapshot.candidate_id
    same_snapshot_c = cascade._candidate_record_id(
        final_url=INDEX, source="playwright", tier="tier4",
        municipio=MUNICIPIO, bucket="concurso_publico",
        snapshot=first.evidence_snapshot,
    )
    same_snapshot_p = cascade._candidate_record_id(
        final_url=INDEX, source="playwright", tier="tier4",
        municipio=MUNICIPIO, bucket="processo_seletivo",
        snapshot=first.evidence_snapshot,
    )
    assert same_snapshot_c != same_snapshot_p


def test_snapshot_and_candidate_are_deeply_immutable() -> None:
    mutable_links = [[BASE + "/detalhe/1", "Detalhes"]]
    evidence = snapshot(links=mutable_links)
    candidate = record(evidence=evidence)
    mutable_links[0][1] = "MUTATED"

    assert candidate.evidence_snapshot.links[0][1] == "Detalhes"
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        candidate.reason = "mutated"  # type: ignore[misc]
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        candidate.evidence_snapshot.links[0][1] = "mutated"  # type: ignore[index]


def test_tier3_selector_returns_existing_eligible_candidate_id_only() -> None:
    candidate = record()
    picked = cascade.resolve_selector_pick(
        [candidate], "concurso_publico", candidate.candidate_id,
    )
    assert isinstance(picked, cascade.SelectedResource)
    assert picked.candidate is candidate
    assert picked.candidate.candidate_id == candidate.candidate_id


def test_selector_unknown_id_and_unknown_bucket_are_review_with_reason() -> None:
    candidate = record()
    missing = cascade.resolve_selector_pick(
        [candidate], "concurso_publico", "v1:does-not-exist",
    )
    unknown_bucket = cascade.resolve_selector_pick(
        [candidate], "desconocido", candidate.candidate_id,
    )
    assert isinstance(missing, cascade.FinalDecision)
    assert missing.status == "revisar" and "inexistente" in missing.reason
    assert isinstance(unknown_bucket, cascade.FinalDecision)
    assert unknown_bucket.status == "revisar" and "bucket" in unknown_bucket.reason


def test_duplicate_eligible_record_dedupes_deterministically_with_reason() -> None:
    candidate = record()
    picked = cascade.resolve_selector_pick(
        [candidate, candidate], "concurso_publico", None,
    )
    assert isinstance(picked, cascade.SelectedResource)
    assert picked.candidate is candidate
    assert "duplic" in picked.reason


def test_distinct_eligible_tie_without_ai_pick_is_review() -> None:
    first = record()
    second_url = BASE + "/concursos-publicos"
    second = record(evidence=snapshot(
        requested_url=second_url, final_url=second_url,
        text=INDEX_TEXT + "\nTodos os anos",
    ))
    picked = cascade.resolve_selector_pick(
        [first, second], "concurso_publico", None,
    )
    assert isinstance(picked, cascade.FinalDecision)
    assert picked.status == "revisar"
    assert "empate" in picked.reason


def test_specific_pages_precede_combined_page() -> None:
    specific = record()
    combined_text = INDEX_TEXT.replace(
        "Concursos Públicos", "Concursos Públicos e Processos Seletivos",
    ) + "\nProcesso Seletivo 02/2026\nFiltrar por modalidade"
    combined = record(evidence=snapshot(
        combined_text,
        requested_url=BASE + "/concursos-e-processos",
        final_url=BASE + "/concursos-e-processos",
        title="Prefeitura de Barros Cassal - Concursos e Processos Seletivos",
    ), bucket_hint="ambos")

    picked = cascade.resolve_selector_pick(
        [specific, combined], "concurso_publico", specific.candidate_id,
    )
    assert isinstance(picked, cascade.SelectedResource)
    assert picked.candidate is specific


@pytest.mark.parametrize(
    ("case", "text", "title", "final_url", "expected_decision"),
    [
        (
            "detail",
            "Prefeitura Municipal de Barros Cassal\nConcurso Público 01/2026\n"
            "Documentos\nEdital de abertura 01/2026\nRetificação\nResultado final",
            "Concurso Público 01/2026",
            INDEX,
            "detalle_individual_rechazado",
        ),
        (
            "news",
            "Prefeitura Municipal de Barros Cassal\nClique para ouvir esta notícia\n"
            "Compartilhe\nNotícias relacionadas\nConcurso Público 01/2026 abre inscrições",
            "Concurso abre inscrições",
            INDEX,
            "nao_encontrado",
        ),
        (
            "year-menu",
            "Prefeitura Municipal de Barros Cassal\nConcursos Públicos\n"
            "Concurso Público 2026\nConcurso Público 2025\n"
            "Concurso Público 2024\nConcurso Público 2023\nSelecione o ano",
            "Concursos Públicos",
            INDEX,
            "revisar",
        ),
        (
            "other-municipality",
            INDEX_TEXT.replace("Barros Cassal", "Soledade"),
            "Prefeitura Municipal de Soledade - Concursos",
            "https://www.soledade.rs.gov.br/concursos",
            "revisar",
        ),
        (
            "checkpoint",
            "Vercel Security Checkpoint\nVerifying you are human\nEnable JavaScript",
            "Vercel Security Checkpoint",
            INDEX,
            "revisar",
        ),
        (
            "licitacao",
            "Prefeitura Municipal de Barros Cassal\nLicitações\nPregão eletrônico\nCompras públicas",
            "Licitações",
            INDEX,
            "licitacao_rechazada",
        ),
        (
            "cultural",
            "Prefeitura Municipal de Barros Cassal\nConcurso de soberanas\nRainha municipal 2026",
            "Concurso de soberanas",
            INDEX,
            "concurso_cultural_rechazado",
        ),
    ],
)
def test_rejected_or_uncertain_cases_never_confirm_and_always_explain(
        case: str, text: str, title: str, final_url: str,
        expected_decision: str) -> None:
    candidate = record(evidence=snapshot(
        text, title=title, final_url=final_url,
    ))
    final = final_for(candidate)
    assert final.status != "confirmado", case
    assert final.url == ""
    assert final.reason
    assert final.decision == expected_decision


def test_redirect_and_canonical_use_final_url_identity_and_keep_both_urls() -> None:
    requested = BASE + "/atalho"
    candidate = record(evidence=snapshot(requested_url=requested, final_url=INDEX))
    final = final_for(candidate)
    assert candidate.requested_url == requested
    assert candidate.final_url == INDEX
    assert final.status == "confirmado"
    assert final.url == INDEX


def test_redirect_to_delegated_domain_requires_official_chain() -> None:
    requested = BASE + "/concursos"
    delegated = "https://portal.example/recursos"
    evidence = snapshot(
        requested_url=requested, final_url=delegated,
        title="Município de Barros Cassal - Concursos",
    )
    provenance = [{
        "kind": "official_navigation", "municipio": MUNICIPIO,
        "referrer": requested, "label": "Concursos",
    }]
    with_chain = cascade.build_candidate_record(
        requested_url=requested, source="portal_externo_delegado", tier="tier1",
        municipio=MUNICIPIO, bucket_hint="concursos", evidence=evidence,
        provenance=provenance,
    )
    without_chain = cascade.build_candidate_record(
        requested_url=requested, source="portal_externo_delegado", tier="tier1",
        municipio=MUNICIPIO, bucket_hint="concursos", evidence=evidence,
    )
    accepted = final_for(with_chain)
    rejected = final_for(without_chain)
    assert accepted.status == "confirmado"
    assert accepted.url == delegated
    assert with_chain.requested_url == requested
    assert with_chain.final_url == delegated
    assert rejected.status == "revisar"
    assert rejected.reason


def test_bucket_isolation() -> None:
    concurso = record()
    pss_url = BASE + "/processos-seletivos"
    processo = record(
        evidence=snapshot(
            INDEX_TEXT.replace("Concursos Públicos", "Processos Seletivos")
            .replace("Concurso Público", "Processo Seletivo"),
            requested_url=pss_url, final_url=pss_url,
            title="Prefeitura Municipal de Barros Cassal - Processos Seletivos",
        ),
        bucket_hint="processos",
    )
    selected_c = cascade.resolve_selector_pick(
        [concurso, processo], "concurso_publico", concurso.candidate_id,
    )
    selected_p = cascade.resolve_selector_pick(
        [concurso, processo], "processo_seletivo", processo.candidate_id,
    )
    assert isinstance(selected_c, cascade.SelectedResource)
    assert isinstance(selected_p, cascade.SelectedResource)
    assert selected_c.candidate is concurso
    assert selected_p.candidate is processo


def test_legacy_result_uses_central_adjudicator_and_emits_reason() -> None:
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO, site_base=BASE, url_concursos=INDEX,
        confianza_concursos="probable",
    )
    page = cascade.Page(
        url=INDEX, requested_url=INDEX, status=200,
        title="Prefeitura Municipal de Barros Cassal - Concursos",
        text=INDEX_TEXT,
        html=f"<html><body>{INDEX_TEXT}</body></html>",
    )
    with (
        patch.object(cascade, "fetch_page", return_value=page) as fetch,
        patch.object(cascade, "build_candidate_record", wraps=cascade.build_candidate_record) as build,
    ):
        _, _, verdicts = cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )

    fetch.assert_called_once()
    build.assert_called_once()
    assert isinstance(
        result.selected_resources["concursos"].candidate,
        cascade.CandidateRecord,
    )
    assert result.final_decisions["concursos"].reason
    assert verdicts[f"{MUNICIPIO}|concursos"][1]


def test_legacy_snapshot_without_record_adapts_without_refetch() -> None:
    evidence = snapshot()
    legacy_candidate = cascade.Candidate(
        url=INDEX, source="legacy_snapshot", source_tier="legacy",
        bucket_hint="concursos", evidence_snapshot=evidence,
    )
    result = cascade.MunicipioResult(
        municipio=MUNICIPIO, site_base=BASE, url_concursos=INDEX,
        confianza_concursos="probable",
        selected_evidence={
            "concursos": cascade.BucketCandidateEvidence(
                "concursos", legacy_candidate, evidence,
            ),
        },
    )
    with (
        patch.object(cascade, "fetch_page", side_effect=AssertionError("no refetch")) as fetch,
        patch.object(cascade, "build_candidate_record", wraps=cascade.build_candidate_record) as build,
    ):
        cascade._batch_verify_uncertain_results(
            object(), "gemini-2.5-flash", [result], use_playwright=False,
        )
    fetch.assert_not_called()
    build.assert_called_once()
    assert result.confianza_concursos == "confirmado"
    assert result.final_decisions["concursos"].reason


def test_every_non_confirmed_final_decision_has_reason() -> None:
    decisions = [
        cascade.resolve_selector_pick([record()], "desconocido", None),
        final_for(record(evidence=snapshot("Vercel Security Checkpoint"))),
        final_for(record(evidence=snapshot("Prefeitura de Barros Cassal\nSem concursos"))),
    ]
    for decision in decisions:
        assert isinstance(decision, cascade.FinalDecision)
        if decision.status != "confirmado":
            assert decision.reason.strip()
