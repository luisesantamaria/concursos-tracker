"""Canonical resource certifier role over the generic bounded agent loop."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.fase2_municipios.v2.agents.base import (
    AgentOutputRejected,
    AgentRunner,
    sanitized_response_schema,
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
    CitationVerificationError,
    EvidenceSnapshot,
    anchor_citation,
)


AFFIRMATIVE_CERTIFIER_DECISIONS = frozenset({
    "indice_oficial",
    "indice_oficial_combinado",
    "portal_externo_oficial",
})
REQUIRED_CONFIRMATION_CITATION_DIMENSIONS = frozenset({
    "authority", "identity", "page_role", "bucket", "stability",
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
    failures: list[CitationVerificationError] = []
    for item in prepared["citations"]:
        # Politica 12-jul (aprobada por Luis): el modelo entrega source_id+quote
        # y el codigo computa los offsets exigiendo ocurrencia literal UNICA
        # (quote_not_found/quote_ambiguous rechazan). Los offsets que el modelo
        # emita se DESCARTAN: son ruido demostrado (canario r1: 20/20 sin end;
        # r2: longitudes erroneas), nunca evidencia. El gate final re-verifica
        # slice-equality sobre los offsets computados (require_offsets=True en
        # orchestration._strict_citations), sin cambios.
        item.pop("start", None)
        item.pop("end", None)
        try:
            citation = anchor_citation(snapshot, item, require_offsets=False)
        except CitationVerificationError as exc:
            # Recolectar TODOS los fallos (no solo el primero): la ronda de
            # reparacion necesita la lista completa para corregir de una vez.
            failures.append(exc)
            continue
        item["start"] = citation.start
        item["end"] = citation.end
    if failures:
        first = failures[0]
        first.failures = tuple(failures)
        raise first
    return prepared


def _certifier_invariants(output: Mapping[str, Any]) -> None:
    decision = output.get("decision")
    if decision not in AFFIRMATIVE_CERTIFIER_DECISIONS:
        return
    citations = [
        item for item in output.get("citations", ())
        if isinstance(item, Mapping)
    ]
    dimensions = {item.get("dimension") for item in citations}
    missing = REQUIRED_CONFIRMATION_CITATION_DIMENSIONS - dimensions
    if missing:
        raise AgentOutputRejected(
            role="certifier",
            reason="missing_confirmation_citation_dimensions:" + ",".join(sorted(missing)),
        )
    if decision == "indice_oficial_combinado":
        # Politica 12-jul (hueco FP real): una superficie combinada exige DOS
        # evidencias de bucket textualmente distintas -- una cita bucket sola
        # solo prueba un tipo, no la combinacion. Cardinalidad pura: el codigo
        # cuenta quotes distintos, no decide cuales tipos son (eso es semantica
        # de A/B/C, fuera del alcance del codigo).
        bucket_quotes = {
            item.get("quote") for item in citations
            if item.get("dimension") == "bucket"
        }
        if len(bucket_quotes) < 2:
            raise AgentOutputRejected(
                role="certifier",
                reason="combined_requires_two_distinct_bucket_citations",
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
    invocation_mode: str = "tool_loop",
) -> CertifierAgent:
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
        model=role_models.certifier_model,
        response_schema=sanitized_response_schema(
            resources.references["schema.json"] if tools is None else AGENT_STEP_SCHEMA
        ),
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
        tools=tools,
    )
    return CertifierAgent(runner)
