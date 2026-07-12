"""P0 offline sanitization and terminal observability contracts."""

from __future__ import annotations

import pytest

from scripts.fase2_municipios.v2.agents.base import SnapshotInvalidOutput
from scripts.fase2_municipios.v2.eval.live_abc_adapter import LiveFetchError
from scripts.fase2_municipios.v2.eval.live_observability import redact_recursive
from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as live_fx
from scripts.fase2_municipios.v2.gemini import RawResponse, SchemaValidationError
from scripts.fase2_municipios.v2.gemini.tests import test_client as client_fx


pytestmark = pytest.mark.offline


def test_schema_validation_error_preserves_bounded_raw_response() -> None:
    transport = client_fx.FakeTransport([
        RawResponse(text='{broken "api_key":"secret"', usage=client_fx.VALID_USAGE)
    ])
    client = client_fx.generic_client(transport, client_fx.RecordingLimiter())

    with pytest.raises(SchemaValidationError) as raised:
        client.generate_structured("offline")

    assert raised.value.raw == '{broken "api_key":"secret"'


def test_redaction_preserves_public_query_and_is_idempotent() -> None:
    value = {
        "url": (
            "https://user:pass@example.invalid/pg.php?subarea=19&ano=0"
            "&token=top-secret#public"
        ),
        "Cookie": "session=private",
        "client_secret": "private-client",
        "refreshToken": "private-refresh",
        "ordinary": "tokenization da Secretaria e password policy",
    }

    once = redact_recursive(value)
    twice = redact_recursive(once)

    assert once == twice
    assert "subarea=19" in once["url"] and "ano=0" in once["url"]
    rendered = str(once)
    for secret in ("user:pass", "top-secret", "session=private", "private-client", "private-refresh"):
        assert secret not in rendered
    assert once["ordinary"] == "tokenization da Secretaria e password policy"


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        (
            lambda: live_fx._adapter(
                fetcher=live_fx.FakeFetcher(error=OSError("offline"))
            ),
            "revisar_por_adquisicion",
        ),
        (
            lambda: live_fx._adapter(
                certifier=live_fx.FakeCertifier(outcome=RuntimeError("A"))
            ),
            "revisar_por_A",
        ),
        (
            lambda: live_fx._adapter(
                prosecutor=live_fx.FakeProsecutor(result=RuntimeError("B"))
            ),
            "revisar_por_B",
        ),
        (
            lambda: live_fx._adapter(
                prosecutor=live_fx.FakeProsecutor(result="review")
            ),
            "revisar_por_B",
        ),
        (
            lambda: live_fx._adapter(
                certifier=live_fx.FakeCertifier(citation={
                    "source_id": "main", "start": 0, "end": 3, "quote": "bad",
                })
            ),
            "revisar_por_gate",
        ),
    ],
)
def test_terminal_review_has_first_class_owner(adapter, expected: str) -> None:
    outcome = adapter().request(live_fx.MUNICIPIO, live_fx.BUCKET)
    assert outcome.decision == "revisar"
    assert outcome.cause.revisar_por == expected
    assert outcome.cause.comment


def test_snapshot_invalid_output_emits_stage_error_event() -> None:
    typed = SnapshotInvalidOutput(
        role="certifier",
        code="schema_mismatch",
        raw={"decision": "bad"},
        original_exception=ValueError("invalid structured output"),
    )
    adapter = live_fx._adapter(
        certifier=live_fx.FakeCertifier(outcome=typed)
    )
    events = []
    adapter.set_observer(events.append)

    outcome = adapter.request(live_fx.MUNICIPIO, live_fx.BUCKET)

    assert outcome.decision == "revisar"
    assert any(
        event.get("stage") == "A" and event.get("status") == "error"
        for event in events
    )


def test_http_status_is_preserved_in_terminal_diagnostic() -> None:
    outcome = live_fx._adapter(
        fetcher=live_fx.FakeFetcher(
            error=LiveFetchError("http_status", status_code=403)
        )
    ).request(live_fx.MUNICIPIO, live_fx.BUCKET)

    assert outcome.cause.code == "http_status:403"
    assert "status_code=403" in outcome.audit_events[0].errors[0]
