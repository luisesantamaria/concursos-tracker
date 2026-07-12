"""Offline stage-by-stage fail-closed checks for the existing A/B/C adapter."""

from __future__ import annotations

import pytest

from scripts.fase2_municipios.v2.agents import JudgeOutcome
from scripts.fase2_municipios.v2.eval.live_abc_adapter import LiveCauseKind
from scripts.fase2_municipios.v2.eval.tests.test_live_abc_adapter import (
    BUCKET,
    MUNICIPIO,
    FakeCertifier,
    FakeJudge,
    FakeProsecutor,
    _adapter,
)


pytestmark = pytest.mark.offline


def test_A_failure_stops_B_and_judge_and_returns_review() -> None:
    prosecutor = FakeProsecutor()
    judge = FakeJudge()
    adapter = _adapter(
        certifier=FakeCertifier(outcome=RuntimeError("fixture")),
        prosecutor=prosecutor,
        judge=judge,
    )
    events = []
    adapter.set_observer(events.append)

    outcome = adapter.request(MUNICIPIO, BUCKET)

    assert outcome.decision == "revisar" and outcome.layer is None
    assert outcome.cause.kind is LiveCauseKind.MODEL_FAILURE
    assert prosecutor.calls == [] and judge.calls == []
    assert any(event["stage"] == "A" and event["status"] == "error" for event in events)


def test_B_failure_stops_judge_and_returns_review() -> None:
    judge = FakeJudge()
    adapter = _adapter(
        prosecutor=FakeProsecutor(result=RuntimeError("fixture")),
        judge=judge,
    )
    events = []
    adapter.set_observer(events.append)

    outcome = adapter.request(MUNICIPIO, BUCKET)

    assert outcome.decision == "revisar" and outcome.layer is None
    assert outcome.cause.kind is LiveCauseKind.MODEL_FAILURE
    assert judge.calls == []
    assert any(event["stage"] == "B" and event["status"] == "error" for event in events)


class FailedJudge(FakeJudge):
    def choose(self, **kwargs):
        self.calls.append(kwargs)
        error = TimeoutError("fixture")
        return JudgeOutcome(
            decision=None,
            reason="timeout",
            error_code="judge_error",
            original_exception=error,
        )


def test_judge_failure_on_disagreement_returns_review_without_wrapper_decision() -> None:
    judge = FailedJudge()
    adapter = _adapter(prosecutor=FakeProsecutor(result="block"), judge=judge)
    events = []
    adapter.set_observer(events.append)

    outcome = adapter.request(MUNICIPIO, BUCKET)

    assert outcome.decision == "revisar" and outcome.layer is None
    assert outcome.cause.kind is LiveCauseKind.DISAGREEMENT_UNRESOLVED
    assert len(judge.calls) == 1
    assert any(
        event["stage"] == "juez"
        and event["status"] == "error"
        and event["error_class"] == "timeout"
        for event in events
    )

