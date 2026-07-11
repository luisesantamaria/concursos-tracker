"""Canonical resource certifier role over the generic bounded agent loop."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.fase2_municipios.v2.agents.base import (
    AgentOutputRejected,
    AgentRunner,
    skill_markdown_body,
)
from scripts.fase2_municipios.v2.agents.schemas import AGENT_STEP_SCHEMA
from scripts.fase2_municipios.v2.agents.tools import ToolLimits
from scripts.fase2_municipios.v2.gemini import (
    RoleModels,
    build_gemini_client,
    Transport,
)
from scripts.fase2_municipios.v2.loader import load_canonical_resources
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    EvidenceSnapshot,
    anchor_citation,
)


AFFIRMATIVE_CERTIFIER_DECISIONS = frozenset({
    "indice_oficial",
    "indice_oficial_combinado",
    "portal_externo_oficial",
})
REQUIRED_CONFIRMATION_CITATION_DIMENSIONS = frozenset({
    "identity", "page_role", "bucket", "stability",
})


def _certifier_citations(output: Mapping[str, Any]) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            source_id=item["source_id"],
            start=item["start"],
            end=item["end"],
            quote=item["quote"],
        )
        for item in output["citations"]
    )


def _prepare_certifier_output(
    snapshot: EvidenceSnapshot, output: Mapping[str, Any]
) -> Mapping[str, Any]:
    prepared = copy.deepcopy(dict(output))
    for item in prepared["citations"]:
        citation = anchor_citation(snapshot, item)
        item["start"] = citation.start
        item["end"] = citation.end
    return prepared


def _certifier_invariants(output: Mapping[str, Any]) -> None:
    if output.get("decision") not in AFFIRMATIVE_CERTIFIER_DECISIONS:
        return
    dimensions = {
        item.get("dimension") for item in output.get("citations", ())
        if isinstance(item, Mapping)
    }
    missing = REQUIRED_CONFIRMATION_CITATION_DIMENSIONS - dimensions
    if missing:
        raise AgentOutputRejected(
            role="certifier",
            reason="missing_confirmation_citation_dimensions:" + ",".join(sorted(missing)),
        )


class CertifierAgent:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    @property
    def system_prompt(self) -> str:
        return self.runner.system_prompt

    def certify(self, *, snapshot: EvidenceSnapshot, task: str):
        return self.runner.run(snapshot=snapshot, task=task)


def build_certifier_agent(
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
) -> CertifierAgent:
    resources = load_canonical_resources(
        repo_root=repo_root,
        skills_dir=skills_dir,
        references_dir=references_dir,
    )
    role_models = models or RoleModels()
    client = build_gemini_client(
        transport=transport,
        limiter=limiter,
        model=role_models.certifier_model,
        response_schema=AGENT_STEP_SCHEMA,
    )
    runner = AgentRunner(
        role="certifier",
        system_prompt=skill_markdown_body(
            resources.skills["fase2-resource-certifier"].content
        ),
        client=client,
        output_schema=resources.references["schema.json"],
        extract_citations=_certifier_citations,
        prepare_output=_prepare_certifier_output,
        requires_citations=lambda output: output.get("decision")
        in AFFIRMATIVE_CERTIFIER_DECISIONS,
        output_invariant=_certifier_invariants,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        estimated_tokens=estimated_tokens,
        tool_limits=tool_limits,
    )
    return CertifierAgent(runner)
