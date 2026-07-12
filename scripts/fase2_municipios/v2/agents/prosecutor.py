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
    PROSECUTOR_ACCUSATION_CODES,
    PROSECUTOR_OUTPUT_SCHEMA,
)
from scripts.fase2_municipios.v2.agents.tools import ToolLimits
from scripts.fase2_municipios.v2.gemini import RoleModels, Transport, build_gemini_client
from scripts.fase2_municipios.v2.loader import load_canonical_resources
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    CitationVerificationError,
    EvidenceSnapshot,
    anchor_citation,
)


REQUIRED_ACCUSATION_CODES = frozenset(PROSECUTOR_ACCUSATION_CODES)


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


def _anchor_or_drop(
    snapshot: EvidenceSnapshot,
    items: list[dict[str, Any]],
    *,
    location: str,
    dropped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Best-effort anchor for an OPTIONAL citation batch: never fail-closed.

    Fallo real Aratiba/CP (politica 12-jul): las citas de acusaciones
    discarded/unresolved son "recomendadas", no obligatorias (SKILL.md:54,78).
    Un quote no-anclable (p.ej. chrome repetido) descarta SOLO esa cita
    puntual -- jamas el veredicto ya derivado correctamente. Cada descarte se
    registra en ``dropped`` para observabilidad.
    """
    kept: list[dict[str, Any]] = []
    for item in items:
        item.pop("start", None)
        item.pop("end", None)
        try:
            citation = anchor_citation(snapshot, item, require_offsets=False)
        except CitationVerificationError as exc:
            dropped.append({
                "location": location,
                "source_id": exc.source_id,
                "reason": exc.reason,
                "quote_preview": exc.quote_preview,
            })
            continue
        item["start"] = citation.start
        item["end"] = citation.end
        kept.append(item)
    return kept


def _prepare_prosecutor_output(
    snapshot: EvidenceSnapshot, output: Mapping[str, Any]
) -> Mapping[str, Any]:
    prepared = copy.deepcopy(dict(output))
    result = prepared.get("result")

    # Citas REQUERIDAS (gate 12-jul, endurecido pre-R3): las citas solo pueden
    # ser opcionales bajo result='sustain'. Cualquier otro resultado (block,
    # review, needs_tool o valores futuros) y toda objecion material (proved)
    # exigen evidencia literal estricta -- fallo duro. TODOS los fallos se
    # recolectan de una vez (ronda de reparacion).
    required_items: list[dict[str, Any]] = []
    if result != "sustain":
        required_items.extend(prepared["citations"])
        for accusation in prepared["accusations"]:
            required_items.extend(accusation["citations"])
    else:
        for accusation in prepared["accusations"]:
            if accusation.get("outcome") == "proved":
                required_items.extend(accusation["citations"])
    hard_failures: list[CitationVerificationError] = []
    for item in required_items:
        item.pop("start", None)
        item.pop("end", None)
        try:
            citation = anchor_citation(snapshot, item, require_offsets=False)
        except CitationVerificationError as exc:
            hard_failures.append(exc)
            continue
        item["start"] = citation.start
        item["end"] = citation.end
    if hard_failures:
        first = hard_failures[0]
        first.failures = tuple(hard_failures)
        raise first

    # Citas OPCIONALES (fallo real Aratiba/CP): SOLO bajo result='sustain',
    # las top-level y las de acusaciones discarded/unresolved. Se intenta
    # anclar cada una; un fallo puntual descarta esa cita sola (registrada en
    # dropped_optional_citations) sin afectar el resultado ya derivado.
    dropped: list[dict[str, Any]] = []
    if result == "sustain":
        prepared["citations"] = _anchor_or_drop(
            snapshot, prepared["citations"], location="top_level", dropped=dropped
        )
        for accusation in prepared["accusations"]:
            if accusation.get("outcome") == "proved":
                continue
            accusation["citations"] = _anchor_or_drop(
                snapshot, accusation["citations"],
                location=str(accusation.get("code", "")),
                dropped=dropped,
            )
    if dropped:
        prepared["dropped_optional_citations"] = dropped
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
    proved = [
        accusation for accusation in accusations
        if accusation.get("outcome") == "proved"
    ]
    unresolved = any(
        accusation.get("outcome") == "unresolved" for accusation in accusations
    )
    expected_result = "block" if proved else ("review" if unresolved else "sustain")
    if output.get("result") != expected_result:
        raise AgentOutputRejected(
            role="prosecutor",
            reason=f"{output.get('result')}_violates_global_result:{expected_result}",
        )
    if any(not accusation.get("citations") for accusation in proved):
        raise AgentOutputRejected(
            role="prosecutor", reason="proved_accusation_without_citations"
        )
    if output.get("tool_request") is not None:
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
        # Detach the exact adversarial claim. Narrative/confidence/history from A
        # are anchoring channels, not evidence, and never cross this boundary.
        claim = {
            field: copy.deepcopy(certifier_output.get(field))
            for field in (
                "decision", "bucket", "candidate_id", "resource_url", "citations"
            )
        }
        task = json.dumps(
            {
                "assignment": "Audit the proposed certifier output for false positives.",
                "certifier_claim": claim,
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
