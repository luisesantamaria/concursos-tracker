"""Immutable, content-addressed evidence with verifiable literal citations.

This module accepts content already captured by a caller. It performs no fetch,
file access, path resolution, symlink handling or clock lookup. Raw rendered text
is retained byte-for-byte as a Python string and hashed explicitly as UTF-8.

Citation matching is exact by default. An injected normalizer may relax matching
for quote-only citations, but offsets always address and must match raw content.
Empty source content is allowed and receives the standard SHA-256 of ``b""``;
it remains reproducible evidence but cannot support a non-empty citation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime


Normalizer = Callable[[str], str]
QUOTE_PREVIEW_LIMIT = 48


class SnapshotError(ValueError):
    """Base class for secret-free snapshot construction failures."""


class DuplicateSourceError(SnapshotError):
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        super().__init__(f"duplicate source_id: {source_id}")


def _quote_preview(quote: str) -> str:
    compact = quote.replace("\r", "\\r").replace("\n", "\\n")
    return compact[:QUOTE_PREVIEW_LIMIT]


class CitationVerificationError(SnapshotError):
    """One citation failed without exposing source content."""

    def __init__(self, *, source_id: str, reason: str, quote: str = "") -> None:
        self.source_id = source_id
        self.reason = reason
        self.quote_preview = _quote_preview(quote)
        preview = f", quote_preview={self.quote_preview!r}" if quote else ""
        super().__init__(
            f"citation rejected: source_id={source_id}, reason={reason}{preview}"
        )


@dataclass(frozen=True)
class CitationFailure:
    index: int
    source_id: str
    reason: str
    quote_preview: str


class CitationBatchVerificationError(CitationVerificationError):
    """All citation failures from one batch, in deterministic input order."""

    def __init__(self, failures: tuple[CitationFailure, ...]) -> None:
        self.failures = failures
        self.source_id = ",".join(failure.source_id for failure in failures)
        self.reason = "batch_failed"
        self.quote_preview = ""
        summary = "; ".join(
            f"index={failure.index},source_id={failure.source_id},reason={failure.reason},"
            f"quote_preview={failure.quote_preview!r}"
            for failure in failures
        )
        SnapshotError.__init__(self, f"citation batch rejected: {summary}")


@dataclass(frozen=True)
class EvidenceSource:
    source_id: str
    url: str
    retrieved_at: datetime
    content: str = field(repr=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise SnapshotError("source_id must be a non-empty string")
        if not isinstance(self.url, str) or not self.url.strip():
            raise SnapshotError(f"url must be non-empty for source_id={self.source_id}")
        if not isinstance(self.retrieved_at, datetime):
            raise SnapshotError(f"retrieved_at must be datetime for source_id={self.source_id}")
        if self.retrieved_at.tzinfo is None:
            raise SnapshotError(
                f"retrieved_at must be timezone-aware for source_id={self.source_id}"
            )
        if not isinstance(self.content, str):
            raise SnapshotError(f"content must be string for source_id={self.source_id}")
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        object.__setattr__(self, "content_sha256", digest)


def _snapshot_digest(sources: tuple[EvidenceSource, ...]) -> str:
    payload = [
        [source.source_id, source.content_sha256]
        for source in sources
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EvidenceSnapshot:
    sources: tuple[EvidenceSource, ...]
    snapshot_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple):
            raise SnapshotError("sources must be an immutable tuple")
        if not self.sources:
            raise SnapshotError("snapshot must contain at least one source")
        if any(not isinstance(source, EvidenceSource) for source in self.sources):
            raise SnapshotError("sources must contain only EvidenceSource values")
        expected_order = tuple(sorted(self.sources, key=lambda source: source.source_id))
        if self.sources != expected_order:
            raise SnapshotError("sources must be sorted by source_id")
        ids = tuple(source.source_id for source in self.sources)
        if len(ids) != len(set(ids)):
            duplicate = next(source_id for source_id in ids if ids.count(source_id) > 1)
            raise DuplicateSourceError(duplicate)
        for source in self.sources:
            expected_source_hash = hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            if source.content_sha256 != expected_source_hash:
                raise SnapshotError(f"content hash mismatch for source_id={source.source_id}")
        if self.snapshot_sha256 != _snapshot_digest(self.sources):
            raise SnapshotError("snapshot_sha256 does not match frozen sources")

    def get_source(self, source_id: str) -> EvidenceSource:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise CitationVerificationError(
            source_id=source_id,
            reason="source_not_found",
        )


def build_snapshot(sources: Iterable[EvidenceSource]) -> EvidenceSnapshot:
    """Validate, detach, sort and hash already-provided evidence sources."""
    rebuilt: list[EvidenceSource] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, EvidenceSource):
            raise SnapshotError("sources must contain only EvidenceSource values")
        if source.source_id in seen:
            raise DuplicateSourceError(source.source_id)
        seen.add(source.source_id)
        rebuilt.append(EvidenceSource(
            source_id=source.source_id,
            url=source.url,
            retrieved_at=source.retrieved_at,
            content=source.content,
        ))
    if not rebuilt:
        raise SnapshotError("snapshot must contain at least one source")
    frozen_sources = tuple(sorted(rebuilt, key=lambda source: source.source_id))
    return EvidenceSnapshot(
        sources=frozen_sources,
        snapshot_sha256=_snapshot_digest(frozen_sources),
    )


@dataclass(frozen=True)
class Citation:
    source_id: str
    quote: str
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise CitationVerificationError(
                source_id=str(self.source_id), reason="invalid_source_id"
            )
        if not isinstance(self.quote, str) or not self.quote:
            raise CitationVerificationError(
                source_id=self.source_id, reason="empty_quote"
            )
        if (self.start is None) != (self.end is None):
            raise CitationVerificationError(
                source_id=self.source_id,
                reason="incomplete_offsets",
                quote=self.quote,
            )
        if self.start is not None:
            if (
                isinstance(self.start, bool)
                or isinstance(self.end, bool)
                or not isinstance(self.start, int)
                or not isinstance(self.end, int)
                or self.start < 0
                or self.end <= self.start
            ):
                raise CitationVerificationError(
                    source_id=self.source_id,
                    reason="invalid_offsets",
                    quote=self.quote,
                )


def verify_citation(
    snapshot: EvidenceSnapshot,
    citation: Citation,
    *,
    normalizer: Normalizer | None = None,
) -> None:
    """Verify one quote exactly, or via an injected quote-only normalizer."""
    source = snapshot.get_source(citation.source_id)
    if citation.start is not None:
        assert citation.end is not None
        if citation.end > len(source.content):
            raise CitationVerificationError(
                source_id=citation.source_id,
                reason="offset_out_of_bounds",
                quote=citation.quote,
            )
        if source.content[citation.start:citation.end] != citation.quote:
            raise CitationVerificationError(
                source_id=citation.source_id,
                reason="offset_quote_mismatch",
                quote=citation.quote,
            )
        return

    if normalizer is None:
        matched = citation.quote in source.content
    else:
        try:
            normalized_quote = normalizer(citation.quote)
            normalized_content = normalizer(source.content)
        except Exception as exc:
            raise CitationVerificationError(
                source_id=citation.source_id,
                reason=f"normalizer_failed:{type(exc).__name__}",
                quote=citation.quote,
            ) from exc
        if not isinstance(normalized_quote, str) or not isinstance(normalized_content, str):
            raise CitationVerificationError(
                source_id=citation.source_id,
                reason="normalizer_non_string",
                quote=citation.quote,
            )
        matched = bool(normalized_quote) and normalized_quote in normalized_content
    if not matched:
        raise CitationVerificationError(
            source_id=citation.source_id,
            reason="quote_not_found",
            quote=citation.quote,
        )


@dataclass(frozen=True)
class CitationVerificationReport:
    total: int
    verified_indices: tuple[int, ...]
    source_ids: tuple[str, ...]


def verify_all(
    snapshot: EvidenceSnapshot,
    citations: Iterable[Citation],
    *,
    normalizer: Normalizer | None = None,
) -> CitationVerificationReport:
    """Verify every citation, collecting all failures before rejecting a batch."""
    citation_tuple = tuple(citations)
    failures: list[CitationFailure] = []
    verified: list[int] = []
    source_ids: list[str] = []
    for index, citation in enumerate(citation_tuple):
        try:
            verify_citation(snapshot, citation, normalizer=normalizer)
        except CitationVerificationError as exc:
            failures.append(CitationFailure(
                index=index,
                source_id=exc.source_id,
                reason=exc.reason,
                quote_preview=exc.quote_preview,
            ))
        else:
            verified.append(index)
            source_ids.append(citation.source_id)
    if failures:
        raise CitationBatchVerificationError(tuple(failures))
    return CitationVerificationReport(
        total=len(citation_tuple),
        verified_indices=tuple(verified),
        source_ids=tuple(source_ids),
    )
