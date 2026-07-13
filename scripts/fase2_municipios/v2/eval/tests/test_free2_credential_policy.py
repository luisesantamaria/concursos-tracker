"""Offline contracts for FREE1 -> FREE2 -> PAID credential routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.fase2_municipios.v2.eval.live_model_policy import (
    ErrorCategory,
    ModelPolicyTelemetry,
    PolicyCallError,
    PolicyTransport,
    SchemaValidationError,
    load_model_credentials,
)
from scripts.fase2_municipios.v2.gemini import RawResponse, TokenUsage


pytestmark = pytest.mark.offline
RESPONSE = RawResponse("{}", TokenUsage(2, 3, 5))


class HttpError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"fake-http-{status}")
        self.response = SimpleNamespace(status_code=status, headers={})


class FakeTransport:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def generate(self, model, contents, config):
        self.calls.append((model, contents, config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def policy(free_1, free_2, paid, telemetry=None):
    return PolicyTransport(
        free_transport=free_1,
        free_2_transport=free_2,
        paid_transport=paid,
        model="fake-model",
        stage="A",
        telemetry=telemetry or ModelPolicyTelemetry(),
        timeout_seconds=10,
        sleep=lambda _: None,
        isolate_calls=False,
    )


def test_free1_success_does_not_touch_free2_or_paid() -> None:
    free_1 = FakeTransport([RESPONSE])
    free_2 = FakeTransport([])
    paid = FakeTransport([])
    assert policy(free_1, free_2, paid).generate("fake-model", [], {}) is RESPONSE
    assert len(free_1.calls) == 1
    assert free_2.calls == []
    assert paid.calls == []


def test_free1_429_rotates_to_free2() -> None:
    free_1 = FakeTransport([HttpError(429)])
    free_2 = FakeTransport([RESPONSE])
    paid = FakeTransport([])
    assert policy(free_1, free_2, paid).generate("fake-model", [], {}) is RESPONSE
    assert len(free_2.calls) == 1
    assert paid.calls == []


def test_two_free_429s_reach_paid_only_when_allowed() -> None:
    paid = FakeTransport([RESPONSE])
    routed = policy(
        FakeTransport([HttpError(429)]),
        FakeTransport([HttpError(429)]),
        paid,
    )
    assert routed.generate("fake-model", [], {}) is RESPONSE
    assert len(paid.calls) == 1


def test_free_only_never_reaches_paid_after_both_free_fail() -> None:
    routed = policy(
        FakeTransport([HttpError(429)]),
        FakeTransport([HttpError(429)]),
        None,
    )
    with pytest.raises(PolicyCallError) as raised:
        routed.generate("fake-model", [], {})
    assert raised.value.category is ErrorCategory.QUOTA_429
    assert routed.telemetry.summary()["paid_calls"] == 0


def test_noneligible_schema_error_is_fail_closed_without_rotation() -> None:
    free_2 = FakeTransport([])
    paid = FakeTransport([])
    routed = policy(
        FakeTransport([SchemaValidationError(reason="schema_mismatch")]),
        free_2,
        paid,
    )
    with pytest.raises(PolicyCallError) as raised:
        routed.generate("fake-model", [], {})
    assert raised.value.category is ErrorCategory.SCHEMA_INVALID
    assert free_2.calls == []
    assert paid.calls == []


def test_telemetry_distinguishes_all_three_providers() -> None:
    telemetry = ModelPolicyTelemetry()
    routed = policy(
        FakeTransport([HttpError(429)]),
        FakeTransport([HttpError(429)]),
        FakeTransport([RESPONSE]),
        telemetry,
    )
    routed.generate("fake-model", [], {})
    providers = telemetry.summary()["providers"]
    assert tuple(providers) == ("gemini_free_1", "gemini_free_2", "gemini_paid")
    assert [providers[name]["calls"] for name in providers] == [1, 1, 1]
    assert [providers[name]["quota_rate"] for name in providers] == [1, 1, 0]
    assert telemetry.summary()["paid_calls"] == 1
    assert len(telemetry.fallback_events) == 2
    assert [event["cause"] for event in telemetry.fallback_events] == [
        "quota_429", "quota_429"
    ]


def test_fake_credential_values_never_appear_in_logs(caplog) -> None:
    fake_values = ("fake-free-1", "fake-free-2", "fake-paid")
    routed = policy(
        FakeTransport([HttpError(429)]),
        FakeTransport([HttpError(429)]),
        FakeTransport([RESPONSE]),
    )
    routed.generate("fake-model", [], {})
    captured = caplog.text
    assert all(value not in captured for value in fake_values)


def test_credentials_file_accepts_optional_free2(tmp_path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text(
        "GEMINI_API_KEY_FREE=fake-free-1\n"
        "GEMINI_API_KEY_FREE_2=fake-free-2\n"
        "GEMINI_API_KEY=fake-paid\n",
        encoding="utf-8",
    )
    loaded = load_model_credentials(path)
    assert set(loaded) == {
        "GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2", "GEMINI_API_KEY"
    }


def test_legacy_free1_paid_config_keeps_two_free_attempts_then_paid() -> None:
    free_1 = FakeTransport([HttpError(429), HttpError(429)])
    paid = FakeTransport([RESPONSE])
    routed = policy(free_1, None, paid)
    assert routed.generate("fake-model", [], {}) is RESPONSE
    assert len(free_1.calls) == 2
    assert len(paid.calls) == 1
