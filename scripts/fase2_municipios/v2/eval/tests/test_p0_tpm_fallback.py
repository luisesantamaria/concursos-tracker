"""P0 offline TPM estimation and auditable fallback policy."""

from __future__ import annotations

import json

import pytest

from scripts.fase2_municipios.tests import test_live_model_policy as policy_fx
from scripts.fase2_municipios.v2.eval import live_model_policy as policy
from scripts.fase2_municipios.v2.gemini.tests import test_client as client_fx


pytestmark = pytest.mark.offline


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class GenericCodeError(RuntimeError):
    code = 503


def test_generic_exception_code_never_becomes_http_fallback() -> None:
    classified = policy.classify_error(GenericCodeError("local failure"))
    assert classified.category is policy.ErrorCategory.LOCAL_BUG
    assert classified.fallback_eligible is False


def test_global_deadline_does_not_record_unstarted_free_or_paid_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeMonotonic()
    monkeypatch.setattr(policy.time, "monotonic", clock)
    free = policy_fx.FakeTransport([TimeoutError("provider timeout")])
    paid = policy_fx.FakeTransport([policy_fx.RESPONSE])
    telemetry = policy.ModelPolicyTelemetry()
    transport = policy.PolicyTransport(
        free_transport=free,
        paid_transport=paid,
        model="fixture-model",
        stage="A",
        telemetry=telemetry,
        timeout_seconds=1,
        sleep=clock.sleep,
        jitter=lambda: 0,
        isolate_calls=False,
    )

    with pytest.raises(policy.PolicyCallError) as raised:
        transport.generate("fixture-model", [], {})

    assert raised.value.category is policy.ErrorCategory.LOCAL_BUG
    assert raised.value.original_type == "GlobalDeadline"
    assert len(free.calls) == 1
    assert paid.calls == []
    assert telemetry.fallback_events == []
    assert telemetry.summary()["free_calls"] == 1
    assert telemetry.summary()["paid_calls"] == 0


def test_exact_serialized_request_body_drives_tpm_reservation() -> None:
    contents = [
        {"role": "system", "parts": [{"text": "regra"}]},
        {"role": "user", "parts": [{"text": "ação 😀"}]},
    ]
    transport = client_fx.FakeTransport([client_fx.response({"ok": True})])
    limiter = client_fx.RecordingLimiter()
    client = client_fx.generic_client(transport, limiter)

    config = client._build_config(None)
    serialized = client.serialize_request_payload(contents, config)
    assert json.loads(serialized) == {"contents": contents, "config": config}
    expected = client.estimate_request_tokens(contents, config)
    assert expected == (len(serialized) + 3) // 4

    assert client.generate_structured(contents) == {"ok": True}
    assert limiter.events[0] == ("acquire", expected)


def test_model_calls_are_auditable_by_model_unit_attempt_and_tokens() -> None:
    events = []
    free = policy_fx.FakeTransport([policy_fx.RESPONSE])
    telemetry = policy.ModelPolicyTelemetry(observer=events.append)
    transport = policy.PolicyTransport(
        free_transport=free,
        paid_transport=policy_fx.FakeTransport([]),
        model="fixture-model",
        stage="A",
        telemetry=telemetry,
        timeout_seconds=1,
        isolate_calls=False,
    )
    transport.set_unit("Fixture", "concurso_publico")

    transport.generate("fixture-model", [], {})

    assert telemetry.summary()["models"] == {
        "fixture-model": {"free_calls": 1, "paid_calls": 0, "tokens": 5}
    }
    model_calls = [event for event in events if event.get("event") == "model_call"]
    assert model_calls == [{
        "event": "model_call",
        "municipio": "Fixture",
        "bucket": "concurso_publico",
        "stage": "A",
        "model": "fixture-model",
        "provider": "gemini_free",
        "attempt": 1,
        "status": "ok",
        "error_class": "",
        "tokens": 5,
    }]
