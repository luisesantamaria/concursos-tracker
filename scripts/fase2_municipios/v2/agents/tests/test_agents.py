"""Offline scripted tests for the bounded certifier/prosecutor framework."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.fase2_municipios.v2.agents import (
    AgentLoopLimitError,
    AgentOutputRejected,
    InvalidAgentStepError,
    LocalSnapshotTools,
    ToolLimits,
    build_certifier_agent,
    build_prosecutor_agent,
)
from scripts.fase2_municipios.v2.gemini import (
    RawResponse,
    RealGeminiTransport,
    TokenUsage,
)
from scripts.fase2_municipios.v2.ratelimit import LimiterConfig, ProjectRateLimiter
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    EvidenceSource,
    build_snapshot,
    verify_citation,
)


pytestmark = pytest.mark.offline
REPO_ROOT = Path(__file__).resolve().parents[5]
USAGE = TokenUsage(prompt_tokens=50, candidate_tokens=25, total_tokens=75)
RETRIEVED_AT = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def real_transport_must_never_be_instantiated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("RealGeminiTransport must not be instantiated offline")

    monkeypatch.setattr(RealGeminiTransport, "__init__", fail)


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


def limiter_with_fake_clock() -> tuple[ProjectRateLimiter, FakeClock]:
    clock = FakeClock()
    limiter = ProjectRateLimiter(
        LimiterConfig(rpm=100, tpm=1_000_000),
        now=clock.now,
        sleep=clock.sleep,
        utc_now=clock.utc_now,
    )
    return limiter, clock


class FakeTransport:
    def __init__(self, outcomes: list[dict[str, Any] | RawResponse]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    def generate(self, model: str, contents: Any, config: dict[str, Any]) -> RawResponse:
        self.requests.append({
            "model": model,
            "contents": copy.deepcopy(contents),
            "config": copy.deepcopy(config),
        })
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, RawResponse):
            return outcome
        return RawResponse(text=json.dumps(outcome, ensure_ascii=False), usage=USAGE)


def snapshot_with_marker(marker: str = "PRIVATE-CERTIFIER-TOOL-MARKER"):
    return build_snapshot([
        EvidenceSource(
            source_id="main_content",
            url="https://example.invalid/index",
            retrieved_at=RETRIEVED_AT,
            content=f"Official index\nBuscar\n1 resultado\n{marker}",
        ),
        EvidenceSource(
            source_id="title",
            url="https://example.invalid/index#title",
            retrieved_at=RETRIEVED_AT,
            content="Concursos Públicos",
        ),
    ])


def certifier_output(*, quote: str = "Official index", citations: bool = True, decision: str = "indice_oficial"):
    return {
        "candidate_id": "v2:fixture",
        "source_kind": "dominio_oficial_prefeitura",
        "authority": "confirmada",
        "identity": "confirmada",
        "page_role": "indice_listado",
        "evidence_state": "completa",
        "bucket": "concurso_publico",
        "decision": decision,
        "confidence": "high",
        "insufficiency": "none",
        "citations": ([
            {
                "dimension": "authority",
                "quote": "Official index",
                "source_field": "main_content",
                "source_id": "main_content",
                "start": 0,
                "end": len("Official index"),
            },
            {
                "dimension": "identity",
                "quote": "Official index",
                "source_field": "main_content",
                "source_id": "main_content",
                "start": 0,
                "end": len("Official index"),
            },
            {
                "dimension": "page_role",
                "quote": quote,
                "source_field": "main_content",
                "source_id": "main_content",
                **({
                    "start": 0,
                    "end": len("Official index"),
                } if quote == "Official index" else {}),
            },
            {
                "dimension": "bucket",
                "quote": "Concursos Públicos",
                "source_field": "title",
                "source_id": "title",
                "start": 0,
                "end": len("Concursos Públicos"),
            },
            {
                "dimension": "stability",
                "quote": "Buscar",
                "source_field": "main_content",
                "source_id": "main_content",
                "start": len("Official index\n"),
                "end": len("Official index\nBuscar"),
            },
        ] if citations else []),
        "reason": "fixture decision",
        "tool_request": None,
        "learning_proposal": None,
    }


def prosecutor_output(*, result: str = "sustain"):
    accusation_codes = (
        "wrong_municipality",
        "unproven_authority",
        "news_article",
        "single_event_detail",
        "year_menu_only",
        "licitacao_or_procurement",
        "cultural_contest",
        "appointment_acts",
        "wrong_bucket",
        "generic_repository",
        "antibot_or_shell",
        "unstable_surface",
        "invented_quote",
        "chrome_contamination",
        "refetch_conflict",
    )
    return {
        "result": result,
        "reason": "independent audit complete",
        "confidence": "high",
        "insufficiency": "none",
        "accusations": [
            {"code": code, "outcome": "discarded", "citations": []}
            for code in accusation_codes
        ],
        "citations": [],
        "tool_request": None,
        "failure_mode_proposal": None,
    }


def make_certifier(outcomes, **kwargs):
    transport = FakeTransport(outcomes)
    limiter, clock = limiter_with_fake_clock()
    agent = build_certifier_agent(
        transport=transport,
        limiter=limiter,
        repo_root=REPO_ROOT,
        **kwargs,
    )
    return agent, transport, clock


def _contains_forbidden_config_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized == "tools" or "grounding" in normalized or "googlesearch" in normalized:
                return True
            if _contains_forbidden_config_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_config_key(item) for item in value)
    return False


def test_happy_tool_then_final_accepts_only_verified_citation() -> None:
    snapshot = snapshot_with_marker()
    agent, transport, clock = make_certifier([
        {
            "action": "tool",
            "tool": "get_source",
            "args": {"source_id": "main_content", "start": 0, "length": 40},
        },
        {"action": "final", "output": certifier_output()},
    ])

    result = agent.certify(snapshot=snapshot, task="Certify the candidate.")

    assert result.steps == 2
    assert result.tool_calls == 1
    assert result.output["decision"] == "indice_oficial"
    assert len(transport.requests) == 2
    second_contents = json.dumps(transport.requests[1]["contents"], ensure_ascii=False)
    assert "LOCAL_TOOL_OBSERVATION=" in second_contents
    assert "Official index" in second_contents
    assert clock.sleep_calls == []
    assert all(
        not _contains_forbidden_config_key(request["config"])
        for request in transport.requests
    )


def test_nonexistent_citation_rejects_entire_final() -> None:
    agent, transport, _clock = make_certifier([
        {"action": "final", "output": certifier_output(quote="Invented quote")},
    ])
    with pytest.raises(AgentOutputRejected) as raised:
        agent.certify(snapshot=snapshot_with_marker(), task="Certify.")
    assert raised.value.reason.startswith("citation_verification_failed")
    assert len(transport.requests) == 1


def test_affirmative_result_with_zero_citations_is_rejected() -> None:
    agent, _transport, _clock = make_certifier([
        {"action": "final", "output": certifier_output(citations=False)},
    ])
    with pytest.raises(AgentOutputRejected) as raised:
        agent.certify(snapshot=snapshot_with_marker(), task="Certify.")
    assert raised.value.reason == "affirmative_result_without_citations"


def test_max_steps_stops_without_inventing_final() -> None:
    agent, transport, _clock = make_certifier([
        {"action": "tool", "tool": "list_sources", "args": {}},
    ], max_steps=1, max_tool_calls=1)
    with pytest.raises(AgentLoopLimitError) as raised:
        agent.certify(snapshot=snapshot_with_marker(), task="Certify.")
    assert raised.value.limit_name == "max_steps"
    assert len(transport.requests) == 1


def test_max_tool_calls_is_independent_from_step_limit() -> None:
    agent, transport, _clock = make_certifier([
        {"action": "tool", "tool": "list_sources", "args": {}},
        {"action": "tool", "tool": "list_sources", "args": {}},
    ], max_steps=4, max_tool_calls=1)
    with pytest.raises(AgentLoopLimitError) as raised:
        agent.certify(snapshot=snapshot_with_marker(), task="Certify.")
    assert raised.value.limit_name == "max_tool_calls"
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        RawResponse(text="{invalid", usage=USAGE),
        {
            "action": "final",
            "tool": "find",
            "output": certifier_output(),
        },
    ],
)
def test_invalid_agent_step_is_rejected_immediately_without_retry(outcome) -> None:
    agent, transport, _clock = make_certifier([outcome])
    with pytest.raises(InvalidAgentStepError):
        agent.certify(snapshot=snapshot_with_marker(), task="Certify.")
    assert len(transport.requests) == 1


def test_unknown_tool_and_bad_args_return_observations_and_loop_continues() -> None:
    agent, transport, _clock = make_certifier([
        {"action": "tool", "tool": "not_allowed", "args": {}},
        {"action": "tool", "tool": "get_source", "args": {"start": 0}},
        {
            "action": "final",
            "output": certifier_output(citations=False, decision="nao_encontrado"),
        },
    ])

    result = agent.certify(snapshot=snapshot_with_marker(), task="Certify.")

    assert result.steps == 3
    assert result.tool_calls == 2
    final_contents = json.dumps(transport.requests[2]["contents"], ensure_ascii=False)
    assert "unknown_tool" in final_contents
    assert "invalid_args" in final_contents


def test_get_source_cap_returns_valid_json_metadata_and_raw_citable_offsets() -> None:
    snapshot = snapshot_with_marker()
    tools = LocalSnapshotTools(
        snapshot,
        ToolLimits(
            get_source_max_length=5,
            get_source_default_length=5,
            find_max_needle_length=32,
            find_max_matches=3,
        ),
    )

    observation = tools.execute(
        "get_source",
        {"source_id": "main_content", "start": 0, "length": 100},
    )

    assert json.loads(json.dumps(observation, ensure_ascii=False)) == observation
    assert observation["requested_length"] == 100
    assert observation["returned_length"] == 5
    assert observation["next_start"] == 5
    assert observation["has_more"] is True
    raw_quote = observation["content"]
    verify_citation(
        snapshot,
        Citation("main_content", 0, len(raw_quote), raw_quote),
    )


def test_prosecutor_requests_are_separate_and_contain_only_authorized_inputs() -> None:
    marker = "PRIVATE-CERTIFIER-TOOL-MARKER"
    snapshot = snapshot_with_marker(marker)
    certifier, cert_transport, _cert_clock = make_certifier([
        {
            "action": "tool",
            "tool": "find",
            "args": {"source_id": "main_content", "needle": marker},
        },
        {"action": "final", "output": certifier_output()},
    ])
    certified = certifier.certify(snapshot=snapshot, task="Private certifier task")

    prosecutor_transport = FakeTransport([
        {"action": "final", "output": prosecutor_output()},
    ])
    prosecutor_limiter, prosecutor_clock = limiter_with_fake_clock()
    prosecutor = build_prosecutor_agent(
        transport=prosecutor_transport,
        limiter=prosecutor_limiter,
        repo_root=REPO_ROOT,
    )
    prosecuted = prosecutor.audit(
        snapshot=snapshot,
        certifier_output=certified.output,
    )

    assert prosecuted.output["result"] == "sustain"
    assert prosecutor_clock.sleep_calls == []
    assert prosecutor.system_prompt != certifier.system_prompt
    assert "False-Positive Prosecutor" in prosecutor.system_prompt
    assert "Resource Certifier" in certifier.system_prompt
    assert len(prosecutor_transport.requests) == 1
    for request in prosecutor_transport.requests:
        rendered = json.dumps(request["contents"], ensure_ascii=False)
        assert marker not in rendered
        assert "Private certifier task" not in rendered
        assert "LOCAL_TOOL_OBSERVATION=" not in rendered
        assert snapshot.snapshot_sha256 in rendered
        assert certified.output["candidate_id"] in rendered
        assert not _contains_forbidden_config_key(request["config"])
    assert len(cert_transport.requests) == 2


def test_certifier_and_prosecutor_happy_citations_are_hydrated_and_anchored() -> None:
    snapshot = snapshot_with_marker()
    certifier, _transport, _clock = make_certifier([
        {"action": "final", "output": certifier_output()},
    ])
    certified = certifier.certify(snapshot=snapshot, task="Certify.")
    assert all(
        set(("source_id", "start", "end", "quote")) <= set(citation)
        for citation in certified.output["citations"]
    )

    fiscal_output = prosecutor_output()
    fiscal_output["citations"] = [
        {"source_id": "main_content", "quote": "Official index"}
    ]
    transport = FakeTransport([{"action": "final", "output": fiscal_output}])
    limiter, _clock = limiter_with_fake_clock()
    prosecutor = build_prosecutor_agent(
        transport=transport, limiter=limiter, repo_root=REPO_ROOT
    )
    prosecuted = prosecutor.audit(snapshot=snapshot, certifier_output=certified.output)
    assert prosecuted.output["citations"][0]["start"] == 0
    assert prosecuted.output["citations"][0]["end"] == len("Official index")
    assert set(prosecuted.output["citations"][0]) == {
        "source_id", "quote", "start", "end",
    }


def test_citations_are_verified_at_parse_and_preconsumption_gates(monkeypatch) -> None:
    from scripts.fase2_municipios.v2.agents import base

    calls = []
    real_verify_all = base.verify_all

    def recording_verify_all(snapshot, citations):
        calls.append(tuple(citations))
        return real_verify_all(snapshot, citations)

    monkeypatch.setattr(base, "verify_all", recording_verify_all)
    agent, _transport, _clock = make_certifier([
        {"action": "final", "output": certifier_output()},
    ])
    result = agent.certify(snapshot=snapshot_with_marker(), task="Certify.")

    assert result.output["citations"][0]["start"] == 0
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_prosecutor_top_level_citation_that_cannot_anchor_is_dropped_when_sustain() -> None:
    """Fallo real Aratiba/CP (politica 12-jul): las citas top-level son
    OPCIONALES salvo result='block'. Copiar el 'reason' del certificador
    (texto ausente del snapshot) no puede tumbar un sustain valido: la cita
    puntual se descarta y se registra en dropped_optional_citations, el
    veredicto sustain se mantiene."""
    snapshot = snapshot_with_marker()
    certifier, _transport, _clock = make_certifier([
        {"action": "final", "output": certifier_output()},
    ])
    certified = certifier.certify(snapshot=snapshot, task="Certify.")
    fiscal_output = prosecutor_output()  # result="sustain"
    fiscal_output["citations"] = [
        {"source_id": "main_content", "quote": certified.output["reason"]}
    ]
    transport = FakeTransport([{"action": "final", "output": fiscal_output}])
    limiter, _clock = limiter_with_fake_clock()
    prosecutor = build_prosecutor_agent(
        transport=transport, limiter=limiter, repo_root=REPO_ROOT
    )

    prosecuted = prosecutor.audit(snapshot=snapshot, certifier_output=certified.output)

    assert prosecuted.output["result"] == "sustain"
    assert prosecuted.output["citations"] == []
    dropped = prosecuted.output["dropped_optional_citations"]
    assert len(dropped) == 1
    assert dropped[0]["location"] == "top_level"
    assert dropped[0]["source_id"] == "main_content"


def test_prosecutor_top_level_citation_that_cannot_anchor_hard_fails_when_block() -> None:
    """El resultado 'block' NUNCA se relaja: una cita top-level no-anclable
    sigue rechazando duro el output entero bajo la nueva politica selectiva."""
    snapshot = snapshot_with_marker()
    certifier, _transport, _clock = make_certifier([
        {"action": "final", "output": certifier_output()},
    ])
    certified = certifier.certify(snapshot=snapshot, task="Certify.")
    fiscal_output = prosecutor_output(result="block")
    fiscal_output["citations"] = [
        {"source_id": "main_content", "quote": certified.output["reason"]}
    ]
    transport = FakeTransport([{"action": "final", "output": fiscal_output}])
    limiter, _clock = limiter_with_fake_clock()
    prosecutor = build_prosecutor_agent(
        transport=transport, limiter=limiter, repo_root=REPO_ROOT
    )

    with pytest.raises(AgentOutputRejected) as raised:
        prosecutor.audit(snapshot=snapshot, certifier_output=certified.output)
    assert raised.value.reason.startswith("citation_verification_failed")
