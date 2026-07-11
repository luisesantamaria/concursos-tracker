"""Independent false-positive prosecutor over a separate agent session."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.fase2_municipios.v2.agents.base import (
    AgentOutputRejected,
    AgentRunner,
    sanitized_response_schema,
    skill_markdown_body,
)
from scripts.fase2_municipios.v2.agents.schemas import (
    AGENT_STEP_SCHEMA,
    PROSECUTOR_OUTPUT_SCHEMA,
)
from scripts.fase2_municipios.v2.agents.tools import ToolLimits
from scripts.fase2_municipios.v2.gemini import RoleModels, Transport, build_gemini_client
from scripts.fase2_municipios.v2.loader import load_canonical_resources
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    EvidenceSnapshot,
    anchor_citation,
)


REQUIRED_ACCUSATION_CODES = frozenset({
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
})


def _prosecutor_citations(output: Mapping[str, Any]) -> tuple[Citation, ...]:
    raw_citations = list(output["citations"])
    for accusation in output["accusations"]:
        raw_citations.extend(accusation["citations"])
    return tuple(
        Citation(
            source_id=item["source_id"],
            start=item["start"],
            end=item["end"],
            quote=item["quote"],
        )
        for item in raw_citations
    )


def _prepare_prosecutor_output(
    snapshot: EvidenceSnapshot, output: Mapping[str, Any]
) -> Mapping[str, Any]:
    prepared = copy.deepcopy(dict(output))
    raw_citations = list(prepared["citations"])
    for accusation in prepared["accusations"]:
        raw_citations.extend(accusation["citations"])
    for item in raw_citations:
        citation = anchor_citation(snapshot, item)
        item["start"] = citation.start
        item["end"] = citation.end
    return prepared


def _prosecutor_requires_citations(output: Mapping[str, Any]) -> bool:
    return output.get("result") == "block" or any(
        accusation.get("outcome") == "proved"
        for accusation in output.get("accusations", ())
    )


def _prosecutor_invariants(output: Mapping[str, Any]) -> None:
    accusations = output.get("accusations", ())
    codes = [accusation.get("code") for accusation in accusations]
    if len(codes) != len(set(codes)) or set(codes) != REQUIRED_ACCUSATION_CODES:
        raise AgentOutputRejected(
            role="prosecutor", reason="mandatory_accusations_incomplete_or_duplicated"
        )
    if output.get("result") == "block" and not any(
        accusation.get("outcome") == "proved" for accusation in accusations
    ):
        raise AgentOutputRejected(
            role="prosecutor", reason="block_without_proved_accusation"
        )
    if output.get("result") == "sustain" and any(
        accusation.get("outcome") == "proved" for accusation in accusations
    ):
        raise AgentOutputRejected(
            role="prosecutor", reason="sustain_with_proved_accusation"
        )
    tool_request = output.get("tool_request")
    if output.get("result") == "needs_tool" and tool_request is None:
        raise AgentOutputRejected(role="prosecutor", reason="needs_tool_without_request")
    if output.get("result") != "needs_tool" and tool_request is not None:
        raise AgentOutputRejected(role="prosecutor", reason="unexpected_tool_request")


class ProsecutorAgent:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    @property
    def system_prompt(self) -> str:
        return self.runner.system_prompt

    def audit(
        self,
        *,
        snapshot: EvidenceSnapshot,
        certifier_output: Mapping[str, Any],
    ):
        # Authorized canonical inputs only: proposed decision/citations and the
        # same snapshot. Certifier messages, observations and tool history are absent.
        task = json.dumps(
            {
                "assignment": "Audit the proposed certifier output for false positives.",
                "certifier_output": certifier_output,
                "snapshot_sha256": snapshot.snapshot_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return self.runner.run(snapshot=snapshot, task=task)


def build_prosecutor_agent(
    *,
    transport: Transport,
    limiter,
    repo_root: Path | None = None,
    skills_dir: Path | None = None,
    references_dir: Path | None = None,
    models: RoleModels | None = None,
    max_steps: int = 8,
    max_tool_calls: int = 6,
    estimated_tokens: int = 4_000,
    tool_limits: ToolLimits | None = None,
    invocation_mode: str = "tool_loop",
) -> ProsecutorAgent:
    if invocation_mode not in {"tool_loop", "direct"}:
        raise ValueError("invalid invocation_mode")
    tools = None if invocation_mode == "direct" else "local_snapshot"
    resources = load_canonical_resources(
        repo_root=repo_root,
        skills_dir=skills_dir,
        references_dir=references_dir,
    )
    role_models = models or RoleModels()
    client = build_gemini_client(
        transport=transport,
        limiter=limiter,
        model=role_models.prosecutor_model,
        response_schema=sanitized_response_schema(
            PROSECUTOR_OUTPUT_SCHEMA if tools is None else AGENT_STEP_SCHEMA
        ),
    )
    runner = AgentRunner(
        role="prosecutor",
        system_prompt=skill_markdown_body(
            resources.skills["fase2-fp-prosecutor"].content
        ),
        client=client,
        output_schema=PROSECUTOR_OUTPUT_SCHEMA,
        extract_citations=_prosecutor_citations,
        prepare_output=_prepare_prosecutor_output,
        requires_citations=_prosecutor_requires_citations,
        output_invariant=_prosecutor_invariants,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        estimated_tokens=estimated_tokens,
        tool_limits=tool_limits,
        tools=tools,
    )
    return ProsecutorAgent(runner)
