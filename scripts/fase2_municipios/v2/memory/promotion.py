"""Explicit human-only promotion event contract."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ._jsonl import append_json_line
from .models import (
    PromotionEvent,
    build_promotion_event,
    promotion_event_payload,
)


def append_promotion_event(
    path: Path, *, learning_id: str, actor: str, promoted_at: datetime
) -> PromotionEvent:
    event = build_promotion_event(
        learning_id=learning_id,
        actor=actor,
        promoted_at=promoted_at,
    )
    append_json_line(Path(path), promotion_event_payload(event))
    return event
