"""Frozen evidence and fail-closed citation verification for Fase 2 V2."""

from .snapshot import (
    Citation,
    CitationBatchVerificationError,
    CitationFailure,
    CitationVerificationError,
    CitationVerificationReport,
    DuplicateSourceError,
    EvidenceSnapshot,
    EvidenceSource,
    OFFICIAL_SOURCE_IDS,
    SnapshotError,
    SourceNotAllowedError,
    anchor_citation,
    build_snapshot,
    verify_all,
    verify_citation,
)

__all__ = [
    "Citation",
    "CitationBatchVerificationError",
    "CitationFailure",
    "CitationVerificationError",
    "CitationVerificationReport",
    "DuplicateSourceError",
    "EvidenceSnapshot",
    "EvidenceSource",
    "OFFICIAL_SOURCE_IDS",
    "SnapshotError",
    "SourceNotAllowedError",
    "anchor_citation",
    "build_snapshot",
    "verify_all",
    "verify_citation",
]
