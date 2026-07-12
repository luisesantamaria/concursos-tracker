from __future__ import annotations

import inspect

import pytest

from scripts.fase2_municipios.v2.agents import base
from scripts.fase2_municipios.v2.agents.orchestration import ProposalValidationError
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCAdapter,
    LiveABCOutcome,
    LiveCause,
    LiveCauseKind,
    ModelResponseValidationError,
)
from scripts.fase2_municipios.v2.eval.run_golden_live import (
    _parser,
    _should_retry_unit,
    run_golden_live,
)


pytestmark = pytest.mark.offline


def _failed(error: BaseException) -> LiveABCOutcome:
    return LiveABCOutcome(
        municipio="Fixture",
        bucket="concurso_publico",
        decision="revisar",
        url="",
        cause=LiveCause(LiveCauseKind.MODEL_FAILURE, type(error).__name__, "fallo"),
        layer=None,
        original_exception=error,
    )


def test_direct_snapshot_limit_accepts_itaqui_sized_evidence() -> None:
    assert base.MAX_DIRECT_SNAPSHOT_CHARS == 400_000


def test_live_runner_default_http_read_timeout_is_sixty_seconds() -> None:
    assert inspect.signature(run_golden_live).parameters["http_read_timeout"].default == 60.0
    assert _parser().get_default("http_read_timeout") == 60.0


@pytest.mark.parametrize(
    "error",
    [ModelResponseValidationError("bad model json"), ProposalValidationError("bad proposal")],
)
def test_unit_retries_once_only_for_model_or_proposal_validation(error: BaseException) -> None:
    assert _should_retry_unit(_failed(error), attempt=1) is True
    assert _should_retry_unit(_failed(error), attempt=2) is False
    assert _should_retry_unit(_failed(TimeoutError("model timeout")), attempt=1) is False


def test_nao_encontrado_is_exposed_as_negative_final_decision() -> None:
    from scripts.fase2_municipios.v2.eval.tests.test_live_abc_adapter import (
        BUCKET,
        MUNICIPIO,
        FakeCertifier,
        _adapter,
    )

    outcome = _adapter(certifier=FakeCertifier(decision="nao_encontrado")).request(
        MUNICIPIO, BUCKET
    )

    assert outcome.decision == "nao_encontrado"
    assert outcome.url == ""
    assert outcome.cause.kind is LiveCauseKind.LEGITIMATE_ABSENCE
    assert outcome.cause.revisar_por == ""
