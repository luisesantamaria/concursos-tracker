"""RED/GREEN contracts for role-generic ``tools=none`` invocation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from scripts.fase2_municipios.v2.agents import base
from scripts.fase2_municipios.v2.snapshot import EvidenceSource, build_snapshot


pytestmark = pytest.mark.offline


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "citations"],
    "properties": {
        "decision": {"enum": ["indice_oficial", "revisar"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_id", "start", "end", "quote"],
                "properties": {
                    "source_id": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "quote": {"type": "string"},
                },
            },
        },
    },
}


class OneShotClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = []

    def generate_structured(self, contents, *, estimated_tokens: int):
        self.calls.append((contents, estimated_tokens))
        return self.response


def _snapshot():
    return build_snapshot((EvidenceSource(
        source_id="main",
        url="https://example.invalid/concursos",
        retrieved_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        content="Official index",
    ),))


def _citations(output):
    return tuple(
        base.Citation(**item) for item in output.get("citations", ())
    )


def _runner(client: OneShotClient):
    return base.AgentRunner(
        role="fixture",
        system_prompt="Return the structured decision.",
        client=client,
        output_schema=OUTPUT_SCHEMA,
        extract_citations=_citations,
        requires_citations=lambda output: output["decision"] == "indice_oficial",
        tools=None,
    )


def test_tools_none_bypasses_loop_and_returns_direct_structured_output(monkeypatch) -> None:
    raw = {
        "decision": "indice_oficial",
        "citations": [{
            "source_id": "main", "start": 0, "end": 14,
            "quote": "Official index",
        }],
    }
    client = OneShotClient(raw)
    runner = _runner(client)
    loop_calls = []
    monkeypatch.setattr(
        runner,
        "_run_tool_loop",
        lambda **kwargs: loop_calls.append(kwargs),
        raising=False,
    )

    result = runner.run(snapshot=_snapshot(), task="certify fixture")

    assert loop_calls == []
    assert len(client.calls) == 1
    assert isinstance(result, base.AgentRunResult)
    assert result.output == raw
    assert result.steps == 1
    assert result.tool_calls == 0


def test_invalid_direct_output_is_typed_and_gate_alone_maps_to_review() -> None:
    error_type = getattr(base, "SnapshotInvalidOutput", None)
    assert error_type is not None, "typed invocation error result is missing"
    result = _runner(OneShotClient({"decision": "indice_oficial"})).run(
        snapshot=_snapshot(), task="certify fixture"
    )

    assert isinstance(result, error_type)
    assert not hasattr(result, "decision")
    gate = getattr(base, "fail_closed_invocation_result", None)
    assert gate is not None, "certifier/gate mapper is missing"
    gated = gate(result)
    assert gated == "revisar"


def test_direct_certifier_factory_uses_role_schema_without_dialect_marker() -> None:
    from scripts.fase2_municipios.v2.agents import build_certifier_agent
    from scripts.fase2_municipios.v2.agents.tests import test_agents as fixtures

    transport = fixtures.FakeTransport([fixtures.certifier_output()])
    limiter, _clock = fixtures.limiter_with_fake_clock()
    agent = build_certifier_agent(
        transport=transport,
        limiter=limiter,
        repo_root=fixtures.REPO_ROOT,
        invocation_mode="direct",
    )

    result = agent.certify(
        snapshot=fixtures.snapshot_with_marker(), task="Certify direct fixture."
    )

    assert isinstance(result, base.AgentRunResult)
    assert len(transport.requests) == 1
    config = transport.requests[0]["config"]
    assert "$schema" not in config["response_json_schema"]
    assert config["response_json_schema"]["required"]
    rendered = str(transport.requests[0]["contents"])
    assert "FROZEN_EVIDENCE_SNAPSHOT=" in rendered
    assert "APPLICATION AGENTSTEP PROTOCOL" not in rendered


def test_truncated_inline_snapshot_can_never_support_affirmative_output() -> None:
    content = "Official index" + ("x" * 400_001)
    snapshot = build_snapshot((EvidenceSource(
        source_id="main",
        url="https://example.invalid/large",
        retrieved_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        content=content,
    ),))
    raw = {
        "decision": "indice_oficial",
        "citations": [{
            "source_id": "main", "start": 0, "end": 14,
            "quote": "Official index",
        }],
    }
    client = OneShotClient(raw)
    result = _runner(client).run(snapshot=snapshot, task="certify large fixture")

    assert isinstance(result, base.SnapshotInvalidOutput)
    assert result.code == "AgentOutputRejected"
    rendered = client.calls[0][0][2]["parts"][0]["text"]
    payload = rendered.removeprefix("FROZEN_EVIDENCE_SNAPSHOT=")
    evidence = __import__("json").loads(payload)
    source = evidence["sources"][0]
    assert source["content_truncated"] is True
    assert source["original_length"] == len(content)
    assert len(source["content"]) <= 400_000


# --------------------------------------------------------------------------- #
# Ronda de auto-reparacion de citas (politica 12-jul, aprobada por Luis):
# quote-only + anclaje unico; si el anclaje rechaza (ambigua/no encontrada),
# UNA sola re-invocacion con el detalle exacto del fallo. El gate re-verifica
# igual de estricto; si la reparacion tambien falla -> SnapshotInvalidOutput.
# --------------------------------------------------------------------------- #
QUOTE_ONLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "citations"],
    "properties": {
        "decision": {"enum": ["indice_oficial", "revisar"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_id", "quote"],
                "properties": {
                    "source_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
            },
        },
    },
}


class SequencedClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def generate_structured(self, contents, *, estimated_tokens: int):
        self.calls.append((contents, estimated_tokens))
        return self.responses.pop(0)


def _hydrating_prepare(snapshot, output):
    import copy as _copy

    from scripts.fase2_municipios.v2.snapshot import anchor_citation

    prepared = _copy.deepcopy(dict(output))
    for item in prepared["citations"]:
        item.pop("start", None)
        item.pop("end", None)
        citation = anchor_citation(snapshot, item, require_offsets=False)
        item["start"] = citation.start
        item["end"] = citation.end
    return prepared


def _repair_runner(client):
    return base.AgentRunner(
        role="fixture",
        system_prompt="Return the structured decision.",
        client=client,
        output_schema=QUOTE_ONLY_SCHEMA,
        extract_citations=_citations,
        prepare_output=_hydrating_prepare,
        requires_citations=lambda output: output["decision"] == "indice_oficial",
        tools=None,
    )


def _ambiguous_snapshot():
    return build_snapshot((EvidenceSource(
        source_id="main",
        url="https://example.invalid/concursos",
        retrieved_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        content="Concurso 01 aberto. Rodape: Concurso 01 encerrado fim",
    ),))


def test_ambiguous_citation_triggers_single_repair_and_succeeds() -> None:
    first = {
        "decision": "indice_oficial",
        "citations": [{"source_id": "main", "quote": "Concurso 01"}],
    }
    fixed = {
        "decision": "indice_oficial",
        "citations": [{"source_id": "main", "quote": "Concurso 01 aberto"}],
    }
    client = SequencedClient([first, fixed])
    result = _repair_runner(client).run(
        snapshot=_ambiguous_snapshot(), task="certify fixture"
    )

    assert isinstance(result, base.AgentRunResult)
    assert len(client.calls) == 2
    repair_text = client.calls[1][0][-1]["parts"][0]["text"]
    assert "CITATION_REPAIR" in repair_text
    assert "quote_ambiguous" in repair_text
    hydrated = result.output["citations"][0]
    snapshot = _ambiguous_snapshot()
    text = snapshot.sources[0].content
    assert text[hydrated["start"]:hydrated["end"]] == "Concurso 01 aberto"
    assert result.steps == 2


def test_repair_is_bounded_to_one_attempt_then_fail_closed() -> None:
    still_ambiguous = {
        "decision": "indice_oficial",
        "citations": [{"source_id": "main", "quote": "Concurso 01"}],
    }
    client = SequencedClient([still_ambiguous, dict(still_ambiguous)])
    result = _repair_runner(client).run(
        snapshot=_ambiguous_snapshot(), task="certify fixture"
    )

    assert isinstance(result, base.SnapshotInvalidOutput)
    assert len(client.calls) == 2


def test_non_citation_rejection_never_triggers_repair() -> None:
    affirmative_without_citations = {
        "decision": "indice_oficial",
        "citations": [],
    }
    client = SequencedClient([affirmative_without_citations])
    result = _repair_runner(client).run(
        snapshot=_ambiguous_snapshot(), task="certify fixture"
    )

    assert isinstance(result, base.SnapshotInvalidOutput)
    assert len(client.calls) == 1
