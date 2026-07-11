"""Offline contract tests for the opt-in real live A/B/C adapter."""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from typing import Any

import pytest

from scripts.fase2_municipios.v2.agents import JudgeOutcome
from scripts.fase2_municipios.v2.eval import golden_runner
from scripts.fase2_municipios.v2.eval.cassette_producer import (
    CassetteProducer,
    EvidenceLayer,
    ExternalAccessBlocked,
    V1Layer,
)
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    FetchedEvidence,
    LiveABCAdapter,
    LiveCauseKind,
    OrionHTTPFetcher,
)
from scripts.fase2_municipios.v2.gemini import UnauthorizedCredentialError
from scripts.fase2_municipios.v2.gemini import RoleModels


pytestmark = pytest.mark.offline
MUNICIPIO = "Fixture"
BUCKET = "concurso_publico"
URL = "https://fixture.rs.gov.br/concursos-publicos"
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
HAPPY_HTML = """<html><head><title>Prefeitura Municipal de Fixture - Concursos Públicos</title></head>
<body><h1>Concursos Públicos</h1><form><label>Buscar</label><button>Filtrar</button></form>
<p>1 resultado encontrado</p><table><tr><th>Edital</th><th>Situação</th></tr>
<tr><td>Concurso Público Edital 01/2026</td><td>Aberto</td></tr></table>
<a href="/edital.pdf">Edital de abertura</a>
<p>Inscrições abertas para cargos efetivos.</p></body></html>"""
HAPPY_TEXT = (
    "Prefeitura Municipal de Fixture - Concursos Públicos Concursos Públicos "
    "Buscar Filtrar 1 resultado encontrado Edital Situação Concurso Público "
    "Edital 01/2026 Aberto Edital de abertura Inscrições abertas para cargos efetivos."
)


class FakeFetcher:
    def __init__(self, *, content=HAPPY_TEXT, html=HAPPY_HTML, error=None) -> None:
        self.content = content
        self.html = html
        self.error = error
        self.calls = []

    def fetch(self, url: str, *, timeout_seconds: float) -> FetchedEvidence:
        self.calls.append((url, timeout_seconds))
        if self.error is not None:
            raise self.error
        return FetchedEvidence(
            requested_url=url,
            final_url=url,
            retrieved_at=NOW,
            status=200,
            content=self.content,
            html=self.html,
            title="Prefeitura Municipal de Fixture - Concursos Públicos",
        )


def _candidate_id(task: str) -> str:
    matched = re.search(r"candidate_id='([^']+)'", task)
    assert matched
    return matched.group(1)


def _citation(quote: str = "Concursos Públicos") -> dict[str, Any]:
    start = HAPPY_TEXT.index(quote) if quote in HAPPY_TEXT else 0
    return {
        "source_id": "main",
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


class FakeCertifier:
    def __init__(self, outcome=None, *, citation=None, decision="indice_oficial"):
        self.outcome = outcome
        self.citation = citation or _citation()
        self.decision = decision
        self.calls = []

    def certify(self, *, snapshot, task: str):
        self.calls.append({"snapshot": snapshot, "task": task})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is not None:
            return self.outcome
        return {
            "decision": self.decision,
            "bucket": BUCKET,
            "candidate_id": _candidate_id(task),
            "citations": [self.citation] if self.decision != "revisar" else [],
            "reason": "fixture certifier",
        }


class FakeProsecutor:
    def __init__(self, result="sustain") -> None:
        self.result = result
        self.calls = []

    def audit(self, *, snapshot, certifier_output):
        self.calls.append({
            "snapshot": snapshot,
            "certifier_output": certifier_output,
        })
        if isinstance(self.result, BaseException):
            raise self.result
        return {
            "result": self.result,
            "reason": "fixture prosecutor",
            "citations": [],
            "accusations": [],
        }


class FakeJudge:
    def __init__(self, decision="aceptar_A", error_code=None) -> None:
        self.decision = decision
        self.error_code = error_code
        self.model = RoleModels().judge_model
        self.calls = []

    def choose(self, **kwargs):
        self.calls.append(kwargs)
        return JudgeOutcome(
            decision=self.decision,
            reason="fixture judge",
            error_code=self.error_code,
        )


def _adapter(*, fetcher=None, certifier=None, prosecutor=None, judge=None):
    return LiveABCAdapter(
        fetcher=fetcher or FakeFetcher(),
        target_urls={(MUNICIPIO, BUCKET): URL},
        certifier=certifier or FakeCertifier(),
        prosecutor=prosecutor or FakeProsecutor(),
        judge=judge or FakeJudge(),
    )


def test_happy_path_one_snapshot_shared_and_citations_validated() -> None:
    fetcher = FakeFetcher()
    certifier = FakeCertifier()
    prosecutor = FakeProsecutor()
    judge = FakeJudge()
    adapter = _adapter(
        fetcher=fetcher,
        certifier=certifier,
        prosecutor=prosecutor,
        judge=judge,
    )

    outcome = adapter.request(MUNICIPIO, BUCKET)

    assert outcome.decision == "indice_oficial"
    assert outcome.url == URL
    assert outcome.cause.kind is LiveCauseKind.SUCCESS
    assert outcome.layer is adapter.get(MUNICIPIO, BUCKET)
    assert len(fetcher.calls) == 1
    assert certifier.calls[0]["snapshot"] is prosecutor.calls[0]["snapshot"]
    assert outcome.layer.sources[0].url == URL
    assert outcome.layer.citations[0].quote in outcome.layer.sources[0].content
    assert judge.calls == []

    class V1Source:
        def get(self, municipio, bucket):
            return V1Layer(
                decision="indice_oficial",
                url=URL,
                evidence=EvidenceLayer(
                    snapshot_ref="v1:fixture",
                    authority="confirmada",
                    identity="confirmada",
                    reason="offline fixture",
                ),
            )

    producer_result = CassetteProducer(
        v1_source=V1Source(),
        abc_provider=adapter,
    ).produce(((MUNICIPIO, BUCKET),))
    assert producer_result.complete
    assert producer_result.corpus is not None


def test_citation_outside_exact_snapshot_fails_closed_to_review() -> None:
    bad = {"source_id": "main", "start": 0, "end": 17, "quote": "outside snapshot"}
    outcome = _adapter(certifier=FakeCertifier(citation=bad)).request(
        MUNICIPIO, BUCKET
    )

    assert outcome.decision == "revisar"
    assert outcome.url == ""
    assert outcome.cause.kind is LiveCauseKind.EVIDENCE_FAILURE
    assert outcome.cause.code == "consensus_failed_final_gate"


@pytest.mark.parametrize("error", [ExternalAccessBlocked("blocked"), TimeoutError("slow")])
def test_network_failure_is_access_failure_and_preserves_exception(error) -> None:
    outcome = _adapter(fetcher=FakeFetcher(error=error)).request(MUNICIPIO, BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.layer is None
    assert outcome.cause.kind is LiveCauseKind.ACCESS_FAILURE
    assert outcome.cause.comment == "no se pudo acceder"
    assert outcome.original_exception is error


def test_low_level_http_boundary_propagates_external_access_blocked(
    monkeypatch,
) -> None:
    blocked = ExternalAccessBlocked("orion guard")

    def raise_blocked(*args, **kwargs):
        raise blocked

    monkeypatch.setattr("socket.create_connection", raise_blocked)
    with pytest.raises(ExternalAccessBlocked) as raised:
        OrionHTTPFetcher().fetch("https://198.51.100.1/live", timeout_seconds=1)
    assert raised.value is blocked


def test_legitimate_absence_is_distinct_from_access_failure() -> None:
    content = "Prefeitura Municipal de Fixture. Portal institucional. Atendimento."
    html = f"<html><title>Prefeitura Municipal de Fixture</title><body>{content}</body></html>"
    outcome = _adapter(
        fetcher=FakeFetcher(content=content, html=html),
        certifier=FakeCertifier(decision="revisar"),
    ).request(MUNICIPIO, BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.url == ""
    assert outcome.layer is not None
    assert outcome.cause.kind is LiveCauseKind.LEGITIMATE_ABSENCE
    assert outcome.cause.comment != "no se pudo acceder"
    assert outcome.original_exception is None


def test_disagreement_invokes_existing_judge_and_unresolved_is_review() -> None:
    judge = FakeJudge(decision="revisar")
    outcome = _adapter(
        prosecutor=FakeProsecutor(result="block"),
        judge=judge,
    ).request(MUNICIPIO, BUCKET)

    assert len(judge.calls) == 1
    assert judge.model == RoleModels().judge_model
    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.DISAGREEMENT_UNRESOLVED
    assert outcome.cause.code == "judge_ambiguous"


@pytest.mark.parametrize(
    "bad_outcome",
    [
        RuntimeError("client error"),
        TimeoutError("model timeout"),
        "{invalid json",
        {"decision": "indice_oficial"},
        {},
    ],
    ids=["client_exception", "timeout", "invalid_json", "partial", "empty"],
)
def test_gemini_failures_are_all_fail_closed_to_review(bad_outcome) -> None:
    outcome = _adapter(certifier=FakeCertifier(outcome=bad_outcome)).request(
        MUNICIPIO, BUCKET
    )

    assert outcome.decision == "revisar"
    assert outcome.url == ""
    assert outcome.layer is None
    assert outcome.cause.kind is LiveCauseKind.MODEL_FAILURE
    assert outcome.original_exception is not None


def test_paid_key_present_is_rejected_before_transport_and_value_is_never_read(
    monkeypatch,
) -> None:
    reads = []
    constructor_calls = []

    class PaidOnlyEnvironment(dict):
        def __contains__(self, key):
            return key == "GEMINI_API_KEY"

        def __getitem__(self, key):
            reads.append(key)
            raise AssertionError("paid credential value must never be read")

    def bomb_transport(*args, **kwargs):
        constructor_calls.append((args, kwargs))
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(
        "scripts.fase2_municipios.v2.eval.live_abc_adapter.RealGeminiTransport",
        bomb_transport,
    )
    with pytest.raises(UnauthorizedCredentialError) as raised:
        LiveABCAdapter.from_free_environment(
            fetcher=FakeFetcher(),
            target_urls={(MUNICIPIO, BUCKET): URL},
            environ=PaidOnlyEnvironment(),
        )

    assert raised.value.variable_name == "GEMINI_API_KEY"
    assert reads == []
    assert constructor_calls == []


def test_public_signatures_and_model_invocations_exclude_keys_grounding_and_tools(
    monkeypatch,
) -> None:
    forbidden = {"api_key", "tools", "grounding"}
    signatures = (
        inspect.signature(LiveABCAdapter),
        inspect.signature(LiveABCAdapter.from_free_environment),
        inspect.signature(LiveABCAdapter.request),
    )
    assert all(forbidden.isdisjoint(signature.parameters) for signature in signatures)

    certifier = FakeCertifier()
    prosecutor = FakeProsecutor()
    _adapter(certifier=certifier, prosecutor=prosecutor).request(MUNICIPIO, BUCKET)
    for call in (*certifier.calls, *prosecutor.calls):
        assert forbidden.isdisjoint(call)

    factory_calls = []
    fake_transport = object()
    monkeypatch.setattr(
        "scripts.fase2_municipios.v2.eval.live_abc_adapter.RealGeminiTransport",
        lambda free_key, client_factory=None: (
            factory_calls.append({
                "factory": "transport",
                "free_key": free_key,
                "client_factory": client_factory,
            })
            or fake_transport
        ),
    )
    monkeypatch.setattr(
        "scripts.fase2_municipios.v2.eval.live_abc_adapter.build_certifier_agent",
        lambda **kwargs: factory_calls.append({"factory": "A", **kwargs})
        or FakeCertifier(),
    )
    monkeypatch.setattr(
        "scripts.fase2_municipios.v2.eval.live_abc_adapter.build_prosecutor_agent",
        lambda **kwargs: factory_calls.append({"factory": "B", **kwargs})
        or FakeProsecutor(),
    )
    fake_judge_client = object()
    monkeypatch.setattr(
        "scripts.fase2_municipios.v2.eval.live_abc_adapter.build_judge_client",
        lambda **kwargs: factory_calls.append({"factory": "C-client", **kwargs})
        or fake_judge_client,
    )
    monkeypatch.setattr(
        "scripts.fase2_municipios.v2.eval.live_abc_adapter.build_conflict_judge",
        lambda **kwargs: factory_calls.append({"factory": "C", **kwargs})
        or FakeJudge(),
    )
    limiter = object()
    LiveABCAdapter.from_free_environment(
        fetcher=FakeFetcher(),
        target_urls={(MUNICIPIO, BUCKET): URL},
        environ={"GEMINI_API_KEY_FREE": "fixture-free"},
        limiter=limiter,
    )
    assert factory_calls[0]["free_key"] == "fixture-free"
    for call in factory_calls[1:]:
        assert forbidden.isdisjoint(call)
    role_calls = {call["factory"]: call for call in factory_calls}
    assert role_calls["A"]["transport"] is fake_transport
    assert role_calls["B"]["transport"] is fake_transport
    assert role_calls["C-client"]["transport"] is fake_transport
    assert role_calls["A"]["limiter"] is limiter
    assert role_calls["B"]["limiter"] is limiter
    assert role_calls["C-client"]["limiter"] is limiter
    assert role_calls["C-client"]["models"].judge_model == RoleModels().judge_model


def test_network_guard_intercepts_only_real_http_seam_and_returns_review(
    network_guard_spy,
) -> None:
    network_guard_spy.reset()
    adapter = LiveABCAdapter(
        fetcher=OrionHTTPFetcher(),
        target_urls={(MUNICIPIO, BUCKET): "https://198.51.100.1/live"},
        certifier=FakeCertifier(),
        prosecutor=FakeProsecutor(),
        judge=FakeJudge(),
        timeout_seconds=1,
    )

    outcome = adapter.request(MUNICIPIO, BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.ACCESS_FAILURE
    assert network_guard_spy.blocked_attempts >= 1


def test_run_live_opt_in_is_explicit_and_mutually_exclusive() -> None:
    contract = golden_runner.LiveContract.valid_for_tests()
    adapter = _adapter()

    outcome = golden_runner.run_live(
        contract=contract,
        enable_live_abc=True,
        abc_provider=adapter,
        municipio=MUNICIPIO,
        bucket=BUCKET,
    )
    assert outcome.cause.kind is LiveCauseKind.SUCCESS

    class LegacyAdapter:
        def request(self):
            return "legacy"

    assert golden_runner.run_live(
        contract=contract,
        request_adapter=LegacyAdapter(),
    ) == "legacy"
    with pytest.raises(golden_runner.LiveContractError, match="mutually exclusive"):
        golden_runner.run_live(
            contract=contract,
            request_adapter=LegacyAdapter(),
            enable_live_abc=True,
            abc_provider=adapter,
            municipio=MUNICIPIO,
            bucket=BUCKET,
        )
