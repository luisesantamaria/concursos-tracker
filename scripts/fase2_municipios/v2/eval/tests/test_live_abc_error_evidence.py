"""Offline regression tests for live V2 failure evidence and isolation."""

from __future__ import annotations

import re
import socket
import ssl
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel

from scripts.fase2_municipios.v2.agents import ABCOrchestrator, JudgeOutcome
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    FetchedEvidence,
    LiveABCAdapter,
    LiveCauseKind,
    LiveFetchError,
)
from scripts.fase2_municipios.v2.eval.run_golden_live import _outcome_audit


pytestmark = pytest.mark.offline
BUCKET = "concurso_publico"
URL = "https://fixture.rs.gov.br/concursos-publicos"
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
TEXT = (
    "Prefeitura Municipal de Fixture - Concursos Públicos Concursos Públicos "
    "Buscar Filtrar 1 resultado encontrado Edital Situação Concurso Público "
    "Edital 01/2026 Aberto Edital de abertura Inscrições abertas para cargos efetivos."
)
HTML = """<html><head><title>Prefeitura Municipal de Fixture - Concursos Públicos</title></head>
<body><h1>Concursos Públicos</h1><form><label>Buscar</label><button>Filtrar</button></form>
<p>1 resultado encontrado</p><table><tr><th>Edital</th><th>Situação</th></tr>
<tr><td>Concurso Público Edital 01/2026</td><td>Aberto</td></tr></table>
<a href="/edital.pdf">Edital de abertura</a>
<p>Inscrições abertas para cargos efetivos.</p></body></html>"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts: list[tuple[Any, ...]] = []

    def blocked(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("offline test attempted socket access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    yield attempts
    assert attempts == []


class Fetcher:
    def __init__(self, outcomes: list[BaseException | FetchedEvidence]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float) -> FetchedEvidence:
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def fetched(url: str = URL) -> FetchedEvidence:
    return FetchedEvidence(
        requested_url=url,
        final_url=url,
        retrieved_at=NOW,
        status=200,
        content=TEXT,
        html=HTML,
        title="Prefeitura Municipal de Fixture - Concursos Públicos",
    )


def candidate_id(task: str) -> str:
    match = re.search(r"candidate_id='([^']+)'", task)
    assert match is not None
    return match.group(1)


class ValidCertifier:
    def certify(self, *, snapshot, task: str):
        quote = "Concursos Públicos"
        start = TEXT.index(quote)
        return {
            "decision": "indice_oficial",
            "bucket": BUCKET,
            "candidate_id": candidate_id(task),
            "citations": [{
                "source_id": "main",
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            }],
            "reason": "valid offline response",
        }


class ValidProsecutor:
    def audit(self, *, snapshot, certifier_output):
        return {
            "result": "sustain",
            "reason": "valid offline response",
            "citations": [],
            "accusations": [],
        }


class UnusedJudge:
    def choose(self, **kwargs):
        return JudgeOutcome("aceptar_A", "offline", None)


def adapter(fetcher: Fetcher, *, certifier=None, targets=None) -> LiveABCAdapter:
    return LiveABCAdapter(
        fetcher=fetcher,
        target_urls=targets or {("Fixture", BUCKET): URL},
        certifier=certifier or ValidCertifier(),
        prosecutor=ValidProsecutor(),
        judge=UnusedJudge(),
    )


def chained_fetch_error() -> LiveFetchError:
    try:
        raise socket.gaierror("name resolution failed")
    except socket.gaierror as cause:
        try:
            raise LiveFetchError("directed fetch failed") from cause
        except LiveFetchError as outer:
            return outer


@pytest.mark.parametrize(
    ("error", "expected_fragment"),
    [
        (socket.gaierror("dns unavailable"), "dns unavailable"),
        (OSError("connection refused"), "connection refused"),
        (OSError(), "OSError"),
        (LiveFetchError("inadmissible response"), "inadmissible response"),
        (TimeoutError("fetch timed out"), "fetch timed out"),
        (ssl.SSLError("tls negotiation failed"), "tls negotiation failed"),
    ],
    ids=["gaierror", "oserror", "oserror_empty", "live_fetch", "timeout", "ssl"],
)
def test_fetch_failures_are_audited_and_fail_closed(error, expected_fragment) -> None:
    outcome = adapter(Fetcher([error])).request("Fixture", BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.ACCESS_FAILURE
    assert outcome.layer is None
    assert outcome.audit_events[0].phase == "fetch"
    assert type(error).__name__ in outcome.audit_events[0].errors[0]
    assert expected_fragment in outcome.audit_events[0].errors[0]
    persisted = _outcome_audit(outcome)
    assert persisted["events"][0]["errors"] == list(outcome.audit_events[0].errors)


def test_live_fetch_error_preserves_explicit_cause_without_duplicates() -> None:
    outcome = adapter(Fetcher([chained_fetch_error()])).request("Fixture", BUCKET)

    errors = outcome.audit_events[0].errors
    assert len(errors) == 2
    assert errors[0].startswith("LiveFetchError:")
    assert "directed fetch failed" in errors[0]
    assert errors[1].startswith("gaierror:")
    assert "name resolution failed" in errors[1]
    assert len(errors) == len(set(errors))


class RequiredABCResponse(BaseModel):
    decision: str
    bucket: str


class MalformedModelResponse:
    def certify(self, *, snapshot, task: str):
        # This is the model-response validation boundary, not pipeline input
        # validation: the fake response deliberately omits both required fields.
        return RequiredABCResponse.model_validate({})


def test_model_response_validation_error_is_model_failure_with_evidence() -> None:
    outcome = adapter(
        Fetcher([fetched()]), certifier=MalformedModelResponse()
    ).request("Fixture", BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.MODEL_FAILURE
    assert outcome.layer is None
    assert outcome.audit_events[0].phase == "A"
    assert outcome.audit_events[0].errors[0].startswith("ValidationError:")
    assert "Field required" in outcome.audit_events[0].errors[0]


def test_valid_abc_response_preserves_previous_success_behavior() -> None:
    outcome = adapter(Fetcher([fetched()])).request("Fixture", BUCKET)

    assert outcome.decision == "indice_oficial"
    assert outcome.cause.kind is LiveCauseKind.SUCCESS
    assert outcome.layer is not None
    assert outcome.audit_events == ()


def test_unexpected_internal_error_is_not_disguised_as_model_failure(
    monkeypatch,
) -> None:
    def internal_failure(*args, **kwargs):
        raise KeyError("unexpected orchestration state")

    monkeypatch.setattr(
        ABCOrchestrator, "_proposal_from_certifier", internal_failure
    )
    outcome = adapter(Fetcher([fetched()])).request("Fixture", BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.INTERNAL_FAILURE
    assert outcome.audit_events[0].phase == "abc_internal"
    assert "unexpected orchestration state" in outcome.audit_events[0].errors[0]


def test_first_failed_unit_does_not_abort_next_unit() -> None:
    first = ("Broken", BUCKET)
    second = ("Fixture", BUCKET)
    first_url = "https://broken.invalid/concursos"
    second_url = URL
    fetcher = Fetcher([OSError("first unit offline"), fetched(second_url)])
    live = adapter(
        fetcher,
        targets={first: first_url, second: second_url},
    )

    outcomes = [live.request(*unit) for unit in (first, second)]

    assert outcomes[0].cause.kind is LiveCauseKind.ACCESS_FAILURE
    assert outcomes[0].decision == "revisar"
    assert outcomes[1].cause.kind is LiveCauseKind.SUCCESS
    assert outcomes[1].decision == "indice_oficial"
    assert fetcher.calls == [first_url, second_url]
