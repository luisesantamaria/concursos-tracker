"""Append-only writer contract for staged learning events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ._jsonl import append_json_line
from .models import (
    LearningCandidate,
    LearningEvent,
    build_learning_event,
    learning_event_payload,
)


class AppendOnlyLearningStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(
        self, candidate: LearningCandidate, *, created_at: datetime
    ) -> LearningEvent:
        event = build_learning_event(candidate, created_at=created_at)
        append_json_line(self.path, learning_event_payload(event))
        return event
