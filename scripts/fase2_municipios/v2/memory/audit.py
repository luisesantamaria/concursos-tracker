"""Separate read-only audit contract for staged learning logs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import LearningEvent, SourceCase


class AuditLogError(ValueError):
    """A complete historical line is malformed; truncated tail is tolerated."""


@dataclass(frozen=True)
class CollapsedLearning:
    event: LearningEvent
    occurrences: int


_LEARNING_FIELDS = frozenset({
    "id",
    "schema_version",
    "created_at",
    "source_case",
    "observation",
    "proposed_generalization",
    "status",
})


def _reject_constant(_value: str):
    raise AuditLogError("non_finite_json_number")


def _parse_event(raw: object) -> LearningEvent:
    if not isinstance(raw, dict) or set(raw) != _LEARNING_FIELDS:
        raise AuditLogError("invalid_learning_event_fields")
    source_case = raw["source_case"]
    if not isinstance(source_case, dict) or set(source_case) != {
        "municipio", "snapshot_ref"
    }:
        raise AuditLogError("invalid_source_case")
    string_fields = (
        raw["id"], raw["created_at"], raw["observation"],
        raw["proposed_generalization"], raw["status"],
        source_case["municipio"], source_case["snapshot_ref"],
    )
    if not all(isinstance(value, str) for value in string_fields):
        raise AuditLogError("invalid_learning_event_types")
    if not isinstance(raw["schema_version"], int) or isinstance(
        raw["schema_version"], bool
    ):
        raise AuditLogError("invalid_schema_version")
    if raw["status"] != "staged":
        raise AuditLogError("staged_status_is_immutable")
    return LearningEvent(
        id=raw["id"],
        schema_version=raw["schema_version"],
        created_at=raw["created_at"],
        source_case=SourceCase(
            municipio=source_case["municipio"],
            snapshot_ref=source_case["snapshot_ref"],
        ),
        observation=raw["observation"],
        proposed_generalization=raw["proposed_generalization"],
        status=raw["status"],
    )


def read_learning_events(path: Path) -> tuple[LearningEvent, ...]:
    data = Path(path).read_bytes()
    lines = data.splitlines(keepends=True)
    events: list[LearningEvent] = []
    for index, line in enumerate(lines):
        if index == len(lines) - 1 and not line.endswith(b"\n"):
            break
        try:
            text = line.decode("utf-8")
            raw = json.loads(text, parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditLogError(f"invalid_complete_line:{index + 1}") from exc
        events.append(_parse_event(raw))
    return tuple(events)


def collapse_learning_events(
    events: tuple[LearningEvent, ...] | list[LearningEvent],
) -> dict[str, CollapsedLearning]:
    collapsed: dict[str, CollapsedLearning] = {}
    for event in events:
        if not isinstance(event, LearningEvent):
            raise AuditLogError("invalid_event_object")
        current = collapsed.get(event.id)
        collapsed[event.id] = CollapsedLearning(
            event=current.event if current else event,
            occurrences=(current.occurrences + 1) if current else 1,
        )
    return collapsed
