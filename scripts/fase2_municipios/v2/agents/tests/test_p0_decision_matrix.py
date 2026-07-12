"""P0 fail-closed decision matrix and requested-bucket pin."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.fase2_municipios.v2.agents import ProsecutorAgent
from scripts.fase2_municipios.v2.agents.certifier import (
    AFFIRMATIVE_CERTIFIER_DECISIONS,
)
from scripts.fase2_municipios.v2.agents.tests import test_orchestration as fx
from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as live_fx


pytestmark = pytest.mark.offline


class RecordingRunner:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    def run(self, *, snapshot, task: str):
        self.tasks.append(task)
        return SimpleNamespace(output={"result": "review"})


def test_B_receives_only_normalized_claim_without_reason_or_confidence() -> None:
    runner = RecordingRunner()
    agent = ProsecutorAgent(runner)  # type: ignore[arg-type]
    raw = {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "candidate_id": "candidate-a",
        "resource_url": "https://example.invalid/concursos",
        "citations": [],
        "reason": "ANCHORING-NARRATIVE",
        "confidence": "high",
        "internal_history": "PRIVATE-HISTORY",
    }

    agent.audit(snapshot=fx.snapshot(), certifier_output=raw)

    task = json.loads(runner.tasks[0])
    claim = task["certifier_claim"]
    assert set(claim) == {
        "decision", "bucket", "candidate_id", "resource_url", "citations",
    }
    assert "ANCHORING-NARRATIVE" not in runner.tasks[0]
    assert "PRIVATE-HISTORY" not in runner.tasks[0]


def test_run_does_not_call_B_when_A_is_not_affirmative() -> None:
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )
    calls: list[str] = []

    class ReviewingCertifier:
        def certify(self, *, snapshot, task):
            calls.append("A")
            return SimpleNamespace(output={
                "decision": "revisar",
                "bucket": "concurso_publico",
                "candidate_id": "",
                "citations": [],
                "reason": "evidence insufficient",
            })

    class ForbiddenProsecutor:
        def audit(self, **kwargs):
            calls.append("B")
            raise AssertionError("B must not run for non-affirmative A")

    result = service.run(
        snapshot=fx.snapshot(),
        candidates=(),
        task="fixture",
        certifier=ReviewingCertifier(),
        prosecutor=ForbiddenProsecutor(),
        requested_bucket="concurso_publico",
    )

    assert calls == ["A"]
    assert judge_client.calls == []
    assert result.final_decision.status == "revisar"
    assert result.reason_code == "agreement_review"


def test_B_review_goes_to_review_without_C() -> None:
    judge = live_fx.FakeJudge(decision="aceptar_A")
    outcome = live_fx._adapter(
        prosecutor=live_fx.FakeProsecutor(result="review"),
        judge=judge,
    ).request(live_fx.MUNICIPIO, live_fx.BUCKET)

    assert judge.calls == []
    assert outcome.decision == "revisar"
    assert outcome.cause.code == "prosecutor_review"


def test_requested_bucket_pin_blocks_cross_bucket_confirmation() -> None:
    candidate = fx.record(
        "ps", fx.URL_B,
        decision="indice_oficial",
        bucket="processo_seletivo",
    )
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(candidate,),
        proposal_a=fx.proposal(candidate, bucket="processo_seletivo"),
        proposal_b=fx.proposal(candidate, bucket="processo_seletivo"),
        requested_bucket="concurso_publico",
        prosecutor_result="sustain",
    )

    assert judge_client.calls == []
    assert result.final_decision.status == "revisar"
    assert result.final_decision.bucket == "concurso_publico"
    assert result.reason_code == "bucket_mismatch"


def test_combined_surface_is_normalized_to_requested_bucket() -> None:
    candidate = fx.record(
        "combined", fx.URL_A,
        decision="indice_oficial_combinado",
        bucket="combinado",
    )
    proposal = fx.proposal(
        candidate,
        decision="indice_oficial_combinado",
        bucket="combinado",
    )
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(candidate,),
        proposal_a=proposal,
        proposal_b=proposal,
        requested_bucket="concurso_publico",
        prosecutor_result="sustain",
    )

    assert judge_client.calls == []
    assert result.final_decision.status == "confirmado"
    assert result.final_decision.bucket == "concurso_publico"
    assert result.final_decision.decision in AFFIRMATIVE_CERTIFIER_DECISIONS


def test_v2_semantic_authority_confirms_regardless_of_v1_label() -> None:
    """Independencia total (directiva 12-jul): la etiqueta semantica V1 del
    candidato NO participa en la decision. Con A+B consenso afirmativo, citas
    verificadas y seguridad estructural intacta (autoridad/identidad/
    accesibilidad), V2 confirma — aunque la clasificacion V1 dijera licitacion
    (falso negativo del shell SCPI adjudicado 3/3 a favor de V2 en Chrome)."""
    candidate = fx.record(
        "scpi", fx.URL_A,
        decision="licitacao_rechazada",  # etiqueta V1: debe ser IGNORADA
        bucket="processo_seletivo",
    )
    proposal = fx.proposal(
        candidate, decision="indice_oficial", bucket="processo_seletivo",
    )
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(candidate,),
        proposal_a=proposal,
        proposal_b=proposal,
        requested_bucket="processo_seletivo",
        prosecutor_result="sustain",
    )

    assert judge_client.calls == []
    assert result.final_decision.status == "confirmado"
    assert result.final_decision.bucket == "processo_seletivo"
    assert result.reason_code == "consensus"


def test_v1_disagreement_with_safety_blocker_stays_hard_review() -> None:
    """La cola de auditoria exige seguridad estructural INTACTA: si ademas de
    la discrepancia semantica falla autoridad/identidad/accesibilidad, es
    rechazo duro clasico (consensus_failed_final_gate), jamas cola."""
    candidate = fx.record(
        "scpi", fx.URL_A,
        decision="licitacao_rechazada",
        bucket="processo_seletivo",
        authority="desconocida",
    )
    proposal = fx.proposal(
        candidate, decision="indice_oficial", bucket="processo_seletivo",
    )
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(candidate,),
        proposal_a=proposal,
        proposal_b=proposal,
        requested_bucket="processo_seletivo",
        prosecutor_result="sustain",
    )

    assert judge_client.calls == []
    assert result.final_decision.status == "revisar"
    assert result.reason_code == "consensus_failed_final_gate"


def test_v1_agreement_lane_still_confirms_unchanged() -> None:
    """Carril V1-de-acuerdo intacto: cuando el contrato V1 coincide con V2, la
    confirmacion clasica sigue saliendo igual que antes (caso Novo Hamburgo/PS
    del golden36)."""
    candidate = fx.record(
        "ok", fx.URL_A,
        decision="indice_oficial",
        bucket="processo_seletivo",
    )
    proposal = fx.proposal(
        candidate, decision="indice_oficial", bucket="processo_seletivo",
    )
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(candidate,),
        proposal_a=proposal,
        proposal_b=proposal,
        requested_bucket="processo_seletivo",
        prosecutor_result="sustain",
    )

    assert result.final_decision.status == "confirmado"
    assert result.reason_code == "consensus"
