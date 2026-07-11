"""Offline Gemini client tests using only injected fakes and fake clocks."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.fase2_municipios.v2.gemini import (
    FREE_API_KEY_ENV,
    GroundingForbiddenError,
    MissingFreeApiKeyError,
    PaidKeyForbiddenError,
    RawResponse,
    RetryExhaustedError,
    SchemaValidationError,
    StructuredGeminiClient,
    TokenUsage,
    TransientTransportError,
    UsageInconsistencyError,
    build_certifier_client,
    resolve_free_api_key,
)
from scripts.fase2_municipios.v2.ratelimit import (
    LimiterConfig,
    ProjectRateLimiter,
    QuotaExhaustedError,
)


pytestmark = pytest.mark.offline
REPO_ROOT = Path(__file__).resolve().parents[5]
VALID_USAGE = TokenUsage(prompt_tokens=40, candidate_tokens=20, total_tokens=60)


def valid_certifier_output() -> dict[str, Any]:
    return {
        "candidate_id": "v2:fixture",
        "source_kind": "dominio_oficial_prefeitura",
        "authority": "confirmada",
        "identity": "confirmada",
        "page_role": "indice_listado",
        "evidence_state": "completa",
        "bucket": "concurso_publico",
        "decision": "indice_oficial",
        "confidence": "high",
        "citations": [],
        "reason": "fixture valid reason",
        "tool_request": None,
        "learning_proposal": None,
    }


class EnvSpy(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.membership_checks: list[str] = []
        self.value_reads: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.value_reads.append(key)
        if "PAID" in key or "PAGO" in key or key == "GOOGLE_APPLICATION_CREDENTIALS":
            raise AssertionError(f"forbidden credential value read: {key}")
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def __contains__(self, key: object) -> bool:
        self.membership_checks.append(str(key))
        return key in self.values


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self.seconds

    def utc_now(self) -> datetime:
        return self.base + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.seconds += seconds


class RecordingReservation:
    def __init__(self, inner: Any, events: list[tuple[str, int]]) -> None:
        self.inner = inner
        self.events = events

    def reconcile(self, actual_tokens: int) -> None:
        self.events.append(("reconcile", actual_tokens))
        self.inner.reconcile(actual_tokens)


class RecordingLimiter:
    def __init__(self, *, rpd: int | None = None) -> None:
        self.clock = FakeClock()
        self.events: list[tuple[str, int]] = []
        self.inner = ProjectRateLimiter(
            LimiterConfig(rpm=100, tpm=1_000_000, rpd=rpd),
            now=self.clock.now,
            sleep=self.clock.sleep,
            utc_now=self.clock.utc_now,
        )

    def acquire(self, estimated_tokens: int) -> RecordingReservation:
        self.events.append(("acquire", estimated_tokens))
        return RecordingReservation(self.inner.acquire(estimated_tokens), self.events)


class FakeTransport:
    def __init__(self, outcomes: list[RawResponse | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def generate(self, model: str, contents: Any, config: Mapping[str, Any]) -> RawResponse:
        self.calls.append((model, config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(payload: Any, usage: TokenUsage | None = VALID_USAGE) -> RawResponse:
    return RawResponse(text=json.dumps(payload), usage=usage)


def generic_client(
    transport: FakeTransport,
    limiter: Any,
    *,
    max_attempts: int = 3,
    schema: Mapping[str, Any] | None = None,
) -> StructuredGeminiClient:
    return StructuredGeminiClient(
        transport=transport,
        limiter=limiter,
        model="fixture-model",
        response_schema=schema or {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        max_attempts=max_attempts,
    )


def test_paid_variable_presence_never_reads_its_value() -> None:
    environ = EnvSpy({
        FREE_API_KEY_ENV: "free-secret",
        "GEMINI_TEAM_PAID_TOKEN": "must-never-be-read",
    })

    with pytest.raises(PaidKeyForbiddenError) as raised:
        resolve_free_api_key(environ)

    assert raised.value.variable_name == "GEMINI_TEAM_PAID_TOKEN"
    assert "GEMINI_TEAM_PAID_TOKEN" in environ.membership_checks
    assert environ.value_reads == []


def test_missing_free_key_is_fail_safe_and_exact_free_name_is_used() -> None:
    with pytest.raises(MissingFreeApiKeyError) as raised:
        resolve_free_api_key(EnvSpy({}))
    assert raised.value.variable_name == "GEMINI_API_KEY_FREE"

    environ = EnvSpy({FREE_API_KEY_ENV: "explicit-free-key"})
    assert resolve_free_api_key(environ) == "explicit-free-key"
    assert environ.value_reads == [FREE_API_KEY_ENV]


def test_nested_grounding_is_rejected_before_limiter_or_transport() -> None:
    transport = FakeTransport([response({"ok": True})])
    limiter = RecordingLimiter()
    client = generic_client(transport, limiter)

    with pytest.raises(GroundingForbiddenError) as raised:
        client.generate_structured(
            "offline",
            estimated_tokens=10,
            config_overrides={"temperature": {"nested": {"google_search": {}}}},
        )

    assert "google_search" in raised.value.path
    assert limiter.events == []
    assert transport.calls == []


def test_canonical_certifier_schema_accepts_valid_response() -> None:
    transport = FakeTransport([response(valid_certifier_output())])
    limiter = RecordingLimiter()
    client = build_certifier_client(
        transport=transport,
        limiter=limiter,
        repo_root=REPO_ROOT,
    )

    result = client.generate_structured("offline evidence", estimated_tokens=100)

    assert result == valid_certifier_output()
    assert client.model == "gemini-3.1-flash-lite"
    assert limiter.events == [("acquire", 100), ("reconcile", 60)]
    assert limiter.clock.sleep_calls == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (RawResponse(text="{invalid", usage=VALID_USAGE), "invalid_json"),
        (response({"unexpected": True}), "schema_mismatch"),
    ],
)
def test_invalid_json_and_schema_mismatch_have_distinct_reasons(
    raw: RawResponse, reason: str
) -> None:
    transport = FakeTransport([raw])
    limiter = RecordingLimiter()
    client = generic_client(transport, limiter)

    with pytest.raises(SchemaValidationError) as raised:
        client.generate_structured("offline", estimated_tokens=10)

    assert raised.value.reason == reason
    assert limiter.events == [("acquire", 10), ("reconcile", 60)]
    assert len(transport.calls) == 1


def test_transient_retry_accounts_for_every_attempt_in_order() -> None:
    first_usage = TokenUsage(10, 5, 15)
    transport = FakeTransport([
        TransientTransportError(usage=first_usage, code="timeout"),
        response({"ok": True}),
    ])
    limiter = RecordingLimiter()
    client = generic_client(transport, limiter)

    assert client.generate_structured("offline", estimated_tokens=50) == {"ok": True}
    assert limiter.events == [
        ("acquire", 50),
        ("reconcile", 15),
        ("acquire", 50),
        ("reconcile", 60),
    ]
    assert len(transport.calls) == 2
    assert limiter.clock.sleep_calls == []


def test_transient_exhaustion_raises_typed_error_after_max_attempts() -> None:
    transport = FakeTransport([
        TransientTransportError(usage=TokenUsage(1, 1, 2)),
        TransientTransportError(usage=TokenUsage(2, 1, 3)),
    ])
    limiter = RecordingLimiter()
    client = generic_client(transport, limiter, max_attempts=2)

    with pytest.raises(RetryExhaustedError) as raised:
        client.generate_structured("offline", estimated_tokens=10)

    assert raised.value.attempts == 2
    assert limiter.events == [
        ("acquire", 10),
        ("reconcile", 2),
        ("acquire", 10),
        ("reconcile", 3),
    ]


def test_quota_error_is_not_retried_and_transport_is_not_called() -> None:
    transport = FakeTransport([response({"ok": True})])
    limiter = RecordingLimiter(rpd=0)
    client = generic_client(transport, limiter, max_attempts=3)

    with pytest.raises(QuotaExhaustedError):
        client.generate_structured("offline", estimated_tokens=10)

    assert limiter.events == [("acquire", 10)]
    assert transport.calls == []


@pytest.mark.parametrize(
    ("usage", "reason"),
    [
        (None, "missing"),
        (TokenUsage(-1, 2, 1), "negative"),
        (TokenUsage(2, 3, 6), "total_mismatch"),
    ],
)
def test_anomalous_usage_fails_safe_without_assuming_zero(
    usage: TokenUsage | None, reason: str
) -> None:
    transport = FakeTransport([response({"ok": True}, usage=usage)])
    limiter = RecordingLimiter()
    client = generic_client(transport, limiter)

    with pytest.raises(UsageInconsistencyError) as raised:
        client.generate_structured("offline", estimated_tokens=25)

    assert raised.value.reason == reason
    assert limiter.events == [("acquire", 25)]
    assert len(transport.calls) == 1


def test_logging_contains_audit_fields_but_no_key_prompt_contents_or_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "NEVER-LOG-FREE-KEY"
    secret_prompt = "NEVER-LOG-PROMPT-CONTENTS"
    secret_exception = "NEVER-LOG-EXCEPTION-TEXT"
    assert resolve_free_api_key(EnvSpy({FREE_API_KEY_ENV: secret_key})) == secret_key
    transport = FakeTransport([
        TransientTransportError(usage=TokenUsage(1, 1, 2), code=secret_exception),
        response({"ok": True}),
    ])
    limiter = RecordingLimiter()
    client = generic_client(transport, limiter)

    with caplog.at_level(logging.INFO):
        client.generate_structured(secret_prompt, estimated_tokens=10)

    rendered = caplog.text + " ".join(str(record.__dict__) for record in caplog.records)
    assert secret_key not in rendered
    assert secret_prompt not in rendered
    assert secret_exception not in rendered
    retry = next(record for record in caplog.records if record.msg == "gemini_transient_retry")
    assert retry.model == "fixture-model"
    assert retry.attempt == 1
    usage_record = next(
        record for record in caplog.records if record.msg == "gemini_usage_reconciled"
    )
    assert usage_record.total_tokens in {2, 60}
