"""Role-generic application-level agent loop for Fase 2 V2.

Native function calling is deliberately absent: the Gemini client forbids the
``tools`` config key. Each model response is a flat ``AgentStep`` validated by
the local JSON Schema validator, then conditional invariants are enforced in
Python. No invalid step is retried and no exhausted loop invents a final answer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.fase2_municipios.v2.agents.schemas import AGENT_STEP_SCHEMA
from scripts.fase2_municipios.v2.agents.tools import LocalSnapshotTools, ToolLimits
from scripts.fase2_municipios.v2.gemini import SchemaValidationError, StructuredGeminiClient
from scripts.fase2_municipios.v2.gemini.schema_validation import (
    JsonSchemaValidationError,
    UnsupportedJsonSchemaError,
    validate_json_schema,
)
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    CitationVerificationError,
    EvidenceSnapshot,
    verify_all,
)


LOGGER = logging.getLogger(__name__)
PROTOCOL_INSTRUCTION = """APPLICATION AGENTSTEP PROTOCOL (no native function calling):
Return exactly one JSON object per turn with action=tool or action=final.
For action=tool provide tool and args, and omit output.
For action=final provide output, and omit tool and args.
Available local tools: list_sources(), get_source(source_id,start,length),
find(source_id,needle). Tool observations are JSON and raw offsets are exact.
Never invent evidence. A final answer is accepted only after local schema and
citation verification. Every citation must declare the EvidenceSnapshot
source_id. You may provide exact Python str start/end offsets; if omitted,
Python will accept only one unique literal quote occurrence in that source."""


class AgentError(RuntimeError):
    """Base secret-free agent framework failure."""


class InvalidAgentStepError(AgentError):
    def __init__(self, *, step: int, reason: str) -> None:
        self.step = step
        self.reason = reason
        super().__init__(f"invalid agent step: step={step}, reason={reason}")


class AgentLoopLimitError(AgentError):
    def __init__(
        self, *, limit_name: str, limit: int, steps: int, tool_calls: int
    ) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.steps = steps
        self.tool_calls = tool_calls
        super().__init__(
            f"agent loop limit reached: limit={limit_name}, configured={limit}, "
            f"steps={steps}, tool_calls={tool_calls}"
        )


class AgentOutputRejected(AgentError):
    def __init__(self, *, role: str, reason: str) -> None:
        self.role = role
        self.reason = reason
        super().__init__(f"agent output rejected: role={role}, reason={reason}")


@dataclass(frozen=True)
class AgentStep:
    action: str
    tool: str | None = None
    args: Mapping[str, Any] | None = None
    output: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AgentRunResult:
    role: str
    output: Mapping[str, Any]
    steps: int
    tool_calls: int


@dataclass(frozen=True)
class SnapshotInvalidOutput:
    """Typed invocation-layer failure; deliberately carries no municipal decision."""

    role: str
    code: str
    raw: Any | None = None
    original_exception: BaseException | None = None


def fail_closed_invocation_result(result: AgentRunResult | SnapshotInvalidOutput) -> str:
    """Gate-level mapping kept separate from structured model invocation."""

    if isinstance(result, SnapshotInvalidOutput):
        return "revisar"
    output = result.output
    return str(output.get("decision", output.get("result", "revisar")))


CitationExtractor = Callable[[Mapping[str, Any]], tuple[Citation, ...]]
CitationRequirement = Callable[[Mapping[str, Any]], bool]
OutputInvariant = Callable[[Mapping[str, Any]], None]
OutputPreparer = Callable[[EvidenceSnapshot, Mapping[str, Any]], Mapping[str, Any]]


def sanitized_response_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a JSON Schema for ``response_json_schema`` without dialect markers."""

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): clean(item) for key, item in value.items()
                if key != "$schema"
            }
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return value
    return clean(schema)


def skill_markdown_body(content: str) -> str:
    """Remove YAML frontmatter and otherwise preserve the Markdown body verbatim."""
    if not content.startswith("---"):
        return content
    lines = content.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[index + 1:])
    return content


class AgentRunner:
    """Bounded role-generic loop over a StructuredGeminiClient."""

    def __init__(
        self,
        *,
        role: str,
        system_prompt: str,
        client: StructuredGeminiClient,
        output_schema: Mapping[str, Any],
        extract_citations: CitationExtractor,
        requires_citations: CitationRequirement,
        prepare_output: OutputPreparer | None = None,
        output_invariant: OutputInvariant | None = None,
        max_steps: int = 8,
        max_tool_calls: int = 6,
        estimated_tokens: int = 4_000,
        tool_limits: ToolLimits | None = None,
        tools: str | None = "local_snapshot",
    ) -> None:
        for name, value in (
            ("max_steps", max_steps),
            ("max_tool_calls", max_tool_calls),
            ("estimated_tokens", estimated_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.role = role
        self.system_prompt = system_prompt
        self.client = client
        self.output_schema = output_schema
        self.extract_citations = extract_citations
        self.requires_citations = requires_citations
        self.prepare_output = prepare_output or (lambda _snapshot, output: output)
        self.output_invariant = output_invariant
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.estimated_tokens = estimated_tokens
        self.tool_limits = tool_limits or ToolLimits()
        if tools not in {None, "local_snapshot"}:
            raise ValueError("tools must be None or local_snapshot")
        self.tools = tools

    def _direct_contents(self, snapshot: EvidenceSnapshot, task: str) -> list[dict[str, Any]]:
        evidence = {
            "snapshot_sha256": snapshot.snapshot_sha256,
            "sources": [
                {
                    "source_id": source.source_id,
                    "url": source.url,
                    "retrieved_at": source.retrieved_at.isoformat(),
                    "content": source.content,
                }
                for source in snapshot.sources
            ],
        }
        return [
            {"role": "system", "parts": [{"text": self.system_prompt}]},
            {"role": "user", "parts": [{"text": task}]},
            {"role": "user", "parts": [{
                "text": "FROZEN_EVIDENCE_SNAPSHOT="
                + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            }]},
        ]

    def _initial_contents(
        self, snapshot: EvidenceSnapshot, task: str, tools: LocalSnapshotTools
    ) -> list[dict[str, Any]]:
        inventory = json.dumps(tools.list_sources(), ensure_ascii=False, sort_keys=True)
        return [
            {"role": "system", "parts": [{"text": self.system_prompt}]},
            {"role": "user", "parts": [{"text": task}]},
            {"role": "user", "parts": [{"text": PROTOCOL_INSTRUCTION}]},
            {"role": "user", "parts": [{"text": f"INITIAL_LIST_SOURCES={inventory}"}]},
        ]

    def _parse_step(self, raw: Any, step_number: int) -> AgentStep:
        if not isinstance(raw, Mapping):
            raise InvalidAgentStepError(step=step_number, reason="step_not_object")
        action = raw.get("action")
        tool = raw.get("tool")
        args = raw.get("args")
        output = raw.get("output")
        if action == "tool":
            if not isinstance(tool, str) or not tool or not isinstance(args, Mapping):
                raise InvalidAgentStepError(step=step_number, reason="tool_requires_tool_and_args")
            if "output" in raw:
                raise InvalidAgentStepError(step=step_number, reason="tool_forbids_output")
        elif action == "final":
            if not isinstance(output, Mapping):
                raise InvalidAgentStepError(step=step_number, reason="final_requires_output")
            if "tool" in raw or "args" in raw:
                raise InvalidAgentStepError(step=step_number, reason="final_forbids_tool_and_args")
        else:
            raise InvalidAgentStepError(step=step_number, reason="unknown_action")
        return AgentStep(action=action, tool=tool, args=args, output=output)

    def _validate_final(
        self, snapshot: EvidenceSnapshot, output: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            validate_json_schema(output, self.output_schema)
        except JsonSchemaValidationError as exc:
            raise AgentOutputRejected(
                role=self.role, reason=f"role_schema:{exc.rule}@{exc.path}"
            ) from exc
        except UnsupportedJsonSchemaError as exc:
            raise AgentOutputRejected(
                role=self.role, reason=f"unsupported_role_schema:{exc.keyword}@{exc.path}"
            ) from exc
        try:
            prepared = self.prepare_output(snapshot, output)
        except CitationVerificationError as exc:
            raise AgentOutputRejected(
                role=self.role, reason="citation_verification_failed:1"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentOutputRejected(
                role=self.role, reason=f"citation_format:{type(exc).__name__}"
            ) from exc
        self._validate_for_consumption(snapshot, prepared)
        return prepared

    def _validate_for_consumption(
        self, snapshot: EvidenceSnapshot, output: Mapping[str, Any]
    ) -> None:
        """Revalidate hydrated output at the consume/persist boundary."""
        try:
            validate_json_schema(output, self.output_schema)
        except JsonSchemaValidationError as exc:
            raise AgentOutputRejected(
                role=self.role, reason=f"role_schema:{exc.rule}@{exc.path}"
            ) from exc
        except UnsupportedJsonSchemaError as exc:
            raise AgentOutputRejected(
                role=self.role, reason=f"unsupported_role_schema:{exc.keyword}@{exc.path}"
            ) from exc
        try:
            citations = self.extract_citations(output)
        except (KeyError, TypeError, ValueError, CitationVerificationError) as exc:
            raise AgentOutputRejected(
                role=self.role, reason=f"citation_format:{type(exc).__name__}"
            ) from exc
        if self.requires_citations(output) and not citations:
            raise AgentOutputRejected(role=self.role, reason="affirmative_result_without_citations")
        if self.output_invariant is not None:
            self.output_invariant(output)
        try:
            verify_all(snapshot, citations)
        except CitationVerificationError as exc:
            failure_count = len(getattr(exc, "failures", ())) or 1
            raise AgentOutputRejected(
                role=self.role, reason=f"citation_verification_failed:{failure_count}"
            ) from exc

    def _run_direct(
        self, *, snapshot: EvidenceSnapshot, task: str
    ) -> AgentRunResult | SnapshotInvalidOutput:
        raw: Any | None = None
        try:
            raw = self.client.generate_structured(
                self._direct_contents(snapshot, task),
                estimated_tokens=self.estimated_tokens,
            )
            if not isinstance(raw, Mapping):
                raise AgentOutputRejected(role=self.role, reason="direct_output_not_object")
            parsed_output = self._validate_final(snapshot, raw)
            return AgentRunResult(
                role=self.role, output=parsed_output, steps=1, tool_calls=0
            )
        except (SchemaValidationError, AgentOutputRejected) as exc:
            return SnapshotInvalidOutput(
                role=self.role,
                code=type(exc).__name__,
                raw=raw,
                original_exception=exc,
            )

    def _run_tool_loop(self, *, snapshot: EvidenceSnapshot, task: str) -> AgentRunResult:
        tools = LocalSnapshotTools(snapshot, self.tool_limits)
        contents = self._initial_contents(snapshot, task, tools)
        tool_calls = 0
        for step_number in range(1, self.max_steps + 1):
            try:
                raw = self.client.generate_structured(
                    contents,
                    estimated_tokens=self.estimated_tokens,
                )
            except SchemaValidationError as exc:
                raise InvalidAgentStepError(
                    step=step_number, reason=f"structured_step:{exc.reason}"
                ) from exc
            step = self._parse_step(raw, step_number)
            LOGGER.info(
                "agent_step",
                extra={
                    "agent_event": "step",
                    "role": self.role,
                    "step": step_number,
                    "action": step.action,
                    "tool_calls": tool_calls,
                },
            )
            if step.action == "final":
                assert step.output is not None
                parsed_output = self._validate_final(snapshot, step.output)
                self._validate_for_consumption(snapshot, parsed_output)
                decision = parsed_output.get("decision", parsed_output.get("result", "unknown"))
                LOGGER.info(
                    "agent_final",
                    extra={
                        "agent_event": "final",
                        "role": self.role,
                        "step": step_number,
                        "tool_calls": tool_calls,
                        "decision": decision,
                    },
                )
                return AgentRunResult(
                    role=self.role,
                    output=parsed_output,
                    steps=step_number,
                    tool_calls=tool_calls,
                )

            if tool_calls >= self.max_tool_calls:
                raise AgentLoopLimitError(
                    limit_name="max_tool_calls",
                    limit=self.max_tool_calls,
                    steps=step_number,
                    tool_calls=tool_calls,
                )
            assert step.tool is not None and step.args is not None
            tool_calls += 1
            observation = tools.execute(step.tool, step.args)
            LOGGER.info(
                "agent_tool",
                extra={
                    "agent_event": "tool",
                    "role": self.role,
                    "step": step_number,
                    "tool": step.tool,
                    "tool_call": tool_calls,
                    "observation_ok": observation.get("ok", False),
                },
            )
            contents.append({
                "role": "assistant",
                "parts": [{"text": json.dumps(raw, ensure_ascii=False, sort_keys=True)}],
            })
            contents.append({
                "role": "user",
                "parts": [{
                    "text": "LOCAL_TOOL_OBSERVATION="
                    + json.dumps(observation, ensure_ascii=False, sort_keys=True)
                }],
            })

        raise AgentLoopLimitError(
            limit_name="max_steps",
            limit=self.max_steps,
            steps=self.max_steps,
            tool_calls=tool_calls,
        )

    def run(
        self, *, snapshot: EvidenceSnapshot, task: str
    ) -> AgentRunResult | SnapshotInvalidOutput:
        if self.tools is None:
            return self._run_direct(snapshot=snapshot, task=task)
        return self._run_tool_loop(snapshot=snapshot, task=task)
