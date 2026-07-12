"""Write-only capture sink contract, pre-bound to structured data."""

from __future__ import annotations

from datetime import datetime

from .models import CaptureReport, LearningCandidate
from .store import AppendOnlyLearningStore


class SafeCaptureSink:
    def __init__(
        self,
        *,
        store: AppendOnlyLearningStore,
        candidate: LearningCandidate,
        created_at: datetime,
    ) -> None:
        self.store = store
        self.candidate = candidate
        self.created_at = created_at

    def capture(self) -> CaptureReport:
        try:
            event = self.store.append(self.candidate, created_at=self.created_at)
        except (OSError, TypeError, ValueError, UnicodeError):
            return CaptureReport(captured=False, error_code="capture_error")
        return CaptureReport(captured=True, learning_id=event.id)
