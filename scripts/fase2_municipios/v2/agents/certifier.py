"""Canonical resource certifier role over the generic bounded agent loop."""

from __future__ import annotations

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
    StructuredGeminiClient,
    Transport,
)
from scripts.fase2_municipios.v2.loader import load_canonical_resources
from scripts.fase2_municipios.v2.snapshot import Citation, EvidenceSnapshot


AFFIRMATIVE_CERTIFIER_DECISIONS = frozenset({
    "indice_oficial",
    "indice_oficial_combinado",
    "portal_externo_oficial",
})
REQUIRED_CONFIRMATION_CITATION_DIMENSIONS = frozenset({
    "identity", "page_role", "bucket", "stability",
})


def _certifier_citations(output: Mapping[str, Any]) -> tuple[Citation, ...]:
    # Canonical Fase2CertifierOutput has source_field+quote, not offsets. The
    # canonical skill/schema wins: source_field maps directly to snapshot source_id.
    return tuple(
        Citation(source_id=item["source_field"], quote=item["quote"])
        for item in output["citations"]
    )


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
    client = StructuredGeminiClient(
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
        requires_citations=lambda output: output.get("decision")
        in AFFIRMATIVE_CERTIFIER_DECISIONS,
        output_invariant=_certifier_invariants,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        estimated_tokens=estimated_tokens,
        tool_limits=tool_limits,
    )
    return CertifierAgent(runner)
