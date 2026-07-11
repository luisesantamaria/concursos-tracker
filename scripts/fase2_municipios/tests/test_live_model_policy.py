"""Offline error-taxonomy, timeout and free/paid fallback contracts."""

from __future__ import annotations

import multiprocessing
import time

import pytest

from scripts.fase2_municipios.v2.eval.live_model_policy import (
    ErrorCategory,
    EvidenceInsufficientError,
    LocalBugError,
    ModelPolicyTelemetry,
    PolicyCallError,
    PolicyTransport,
    QuoteInvalidError,
    SemanticModelError,
    classify_error,
    load_model_credentials,
)
from scripts.fase2_municipios.v2.gemini import RawResponse, SchemaValidationError, TokenUsage
from scripts.fase2_municipios.v2.snapshot import CitationVerificationError


pytestmark = pytest.mark.offline
USAGE = TokenUsage(prompt_tokens=3, candidate_tokens=2, total_tokens=5)
RESPONSE = RawResponse(text='{"ok":true}', usage=USAGE)


class StatusError(RuntimeError):
    def __init__(self, status_code: int, retry_after: float | None = None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__("same text for every status")


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate(self, model, contents, config):
        self.calls.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.parametrize(
    "error,category,eligible",
    [
        (StatusError(429), ErrorCategory.QUOTA_429, True),
        (StatusError(408), ErrorCategory.TIMEOUT, True),
        (TimeoutError(), ErrorCategory.TIMEOUT, True),
        (StatusError(503), ErrorCategory.TRANSIENT_5XX, True),
        (ConnectionError(), ErrorCategory.TRANSPORT_ERROR, True),
        (SchemaValidationError(reason="invalid_json"), ErrorCategory.SCHEMA_INVALID, False),
        (QuoteInvalidError(), ErrorCategory.QUOTE_INVALID, False),
        (CitationVerificationError(source_id="main", reason="bad"), ErrorCategory.QUOTE_INVALID, False),
        (EvidenceInsufficientError(), ErrorCategory.EVIDENCE_INSUFFICIENT, False),
        (SemanticModelError(), ErrorCategory.SEMANTIC_ERROR, False),
        (StatusError(400), ErrorCategory.CLIENT_4XX_NO_QUOTA, False),
        (LocalBugError(), ErrorCategory.LOCAL_BUG, False),
        (AssertionError(), ErrorCategory.LOCAL_BUG, False),
    ],
)
def test_closed_taxonomy_uses_types_and_status_codes(error, category, eligible) -> None:
    classified = classify_error(error)
    assert classified.category is category
    assert classified.fallback_eligible is eligible


def test_exact_free_retry_then_paid_sequence_and_fallback_telemetry() -> None:
    free = FakeTransport([StatusError(429, retry_after=0), TimeoutError()])
    paid = FakeTransport([RESPONSE])
    telemetry = ModelPolicyTelemetry()
    transport = PolicyTransport(
        free_transport=free,
        paid_transport=paid,
        model="fixture-model",
        stage="A",
        telemetry=telemetry,
        timeout_seconds=1,
        sleep=lambda _seconds: None,
        jitter=lambda: 0,
        isolate_calls=False,
    )
    transport.set_unit("sao leopoldo", "concurso_publico")

    assert transport.generate("fixture-model", [], {}) is RESPONSE
    assert len(free.calls) == 2
    assert len(paid.calls) == 1
    summary = telemetry.summary()
    assert summary["free_calls"] == 2
    assert summary["paid_calls"] == 1
    assert summary["paid_fallback_reasons"] == {"timeout": 1}
    assert summary["tokens"] == 5
    assert telemetry.fallback_events[0]["stage"] == "A"
    assert telemetry.fallback_events[0]["municipio"] == "sao leopoldo"


@pytest.mark.parametrize(
    "error",
    [
        SchemaValidationError(reason="invalid_json"),
        QuoteInvalidError(),
        EvidenceInsufficientError(),
        SemanticModelError(),
        StatusError(400),
        LocalBugError(),
    ],
)
def test_noneligible_errors_fail_closed_without_paid_fallback(error) -> None:
    free = FakeTransport([error])
    paid = FakeTransport([RESPONSE])
    telemetry = ModelPolicyTelemetry()
    transport = PolicyTransport(
        free_transport=free,
        paid_transport=paid,
        model="fixture-model",
        stage="B",
        telemetry=telemetry,
        timeout_seconds=1,
        isolate_calls=False,
    )
    with pytest.raises(PolicyCallError) as raised:
        transport.generate("fixture-model", [], {})
    assert raised.value.category.fallback_eligible is False
    assert paid.calls == []
    assert telemetry.summary()["paid_calls"] == 0


def _block_forever(*_args, **_kwargs):
    while True:
        time.sleep(0.1)


class BlockingTransport:
    generate = staticmethod(_block_forever)


def test_gemini_hard_timeout_terminates_child_without_leak() -> None:
    before = {child.pid for child in multiprocessing.active_children()}
    transport = PolicyTransport(
        free_transport=BlockingTransport(),
        paid_transport=FakeTransport([RESPONSE]),
        model="fixture-model",
        stage="juez",
        telemetry=ModelPolicyTelemetry(),
        timeout_seconds=0.15,
        sleep=lambda _seconds: None,
        jitter=lambda: 0,
        isolate_calls=True,
    )
    started = time.monotonic()
    with pytest.raises(PolicyCallError) as raised:
        transport.generate("fixture-model", [], {})
    assert raised.value.category is ErrorCategory.TIMEOUT
    assert time.monotonic() - started < 1.5
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_env_file_loads_only_authorized_names_and_never_requires_other_values(tmp_path) -> None:
    path = tmp_path / "gemini.env"
    path.write_text(
        "GEMINI_API_KEY_FREE=free-fixture\n"
        "IGNORED_SECRET=must-not-be-loaded\n"
        "GEMINI_API_KEY=paid-fixture\n",
        encoding="utf-8",
    )
    loaded = load_model_credentials(path)
    assert set(loaded) == {"GEMINI_API_KEY_FREE", "GEMINI_API_KEY"}
