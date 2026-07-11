"""Data-only contracts for external learning staging."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1
MAX_MUNICIPIO_CHARS = 200
MAX_SNAPSHOT_REF_CHARS = 256
MAX_TEXT_CHARS = 4_000


class LearningValidationError(ValueError):
    """Untrusted learning data violates the bounded staging contract."""


def _sanitize_text(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise LearningValidationError(f"{field}_not_string")
    sanitized = "".join(
        character
        if not unicodedata.category(character).startswith("C")
        else f"\\u{ord(character):04x}"
        for character in value
    )[:limit]
    if not sanitized.strip():
        raise LearningValidationError(f"{field}_empty")
    return sanitized


def _injected_timestamp(value: datetime, *, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LearningValidationError(f"{field}_must_be_timezone_aware_datetime")
    return value.isoformat()


@dataclass(frozen=True)
class SourceCase:
    municipio: str
    snapshot_ref: str


@dataclass(frozen=True)
class LearningCandidate:
    source_case: SourceCase
    observation: str
    proposed_generalization: str


@dataclass(frozen=True)
class LearningEvent:
    id: str
    schema_version: int
    created_at: str
    source_case: SourceCase
    observation: str
    proposed_generalization: str
    status: str = "staged"


@dataclass(frozen=True)
class CaptureReport:
    captured: bool
    learning_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PromotionEvent:
    schema_version: int
    learning_id: str
    promoted_at: str
    actor: str
    event: str = "promoted"


def sanitize_candidate(candidate: LearningCandidate) -> LearningCandidate:
    if not isinstance(candidate, LearningCandidate):
        raise LearningValidationError("candidate_wrong_type")
    if not isinstance(candidate.source_case, SourceCase):
        raise LearningValidationError("source_case_wrong_type")
    return LearningCandidate(
        source_case=SourceCase(
            municipio=_sanitize_text(
                candidate.source_case.municipio,
                field="municipio",
                limit=MAX_MUNICIPIO_CHARS,
            ),
            snapshot_ref=_sanitize_text(
                candidate.source_case.snapshot_ref,
                field="snapshot_ref",
                limit=MAX_SNAPSHOT_REF_CHARS,
            ),
        ),
        observation=_sanitize_text(
            candidate.observation, field="observation", limit=MAX_TEXT_CHARS
        ),
        proposed_generalization=_sanitize_text(
            candidate.proposed_generalization,
            field="proposed_generalization",
            limit=MAX_TEXT_CHARS,
        ),
    )


def build_learning_event(
    candidate: LearningCandidate, *, created_at: datetime
) -> LearningEvent:
    checked = sanitize_candidate(candidate)
    canonical = {
        "observation": checked.observation,
        "proposed_generalization": checked.proposed_generalization,
        "schema_version": SCHEMA_VERSION,
        "source_case": {
            "municipio": checked.source_case.municipio,
            "snapshot_ref": checked.source_case.snapshot_ref,
        },
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return LearningEvent(
        id=hashlib.sha256(encoded).hexdigest(),
        schema_version=SCHEMA_VERSION,
        created_at=_injected_timestamp(created_at, field="created_at"),
        source_case=checked.source_case,
        observation=checked.observation,
        proposed_generalization=checked.proposed_generalization,
    )


def learning_event_payload(event: LearningEvent) -> dict[str, Any]:
    return {
        "created_at": event.created_at,
        "id": event.id,
        "observation": event.observation,
        "proposed_generalization": event.proposed_generalization,
        "schema_version": event.schema_version,
        "source_case": {
            "municipio": event.source_case.municipio,
            "snapshot_ref": event.source_case.snapshot_ref,
        },
        "status": event.status,
    }


def build_promotion_event(
    *, learning_id: str, actor: str, promoted_at: datetime
) -> PromotionEvent:
    if (
        not isinstance(learning_id, str)
        or len(learning_id) != 64
        or any(character not in "0123456789abcdef" for character in learning_id)
    ):
        raise LearningValidationError("invalid_learning_id")
    return PromotionEvent(
        schema_version=SCHEMA_VERSION,
        learning_id=learning_id,
        promoted_at=_injected_timestamp(promoted_at, field="promoted_at"),
        actor=_sanitize_text(actor, field="actor", limit=MAX_MUNICIPIO_CHARS),
    )


def promotion_event_payload(event: PromotionEvent) -> dict[str, Any]:
    return {
        "actor": event.actor,
        "event": event.event,
        "learning_id": event.learning_id,
        "promoted_at": event.promoted_at,
        "schema_version": event.schema_version,
    }
