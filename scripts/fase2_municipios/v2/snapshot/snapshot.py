"""Immutable, content-addressed evidence with verifiable literal citations.

This module accepts content already captured by a caller. It performs no fetch,
file access, path resolution, symlink handling or clock lookup. Raw rendered text
is retained unchanged as a Python ``str`` and hashed explicitly as UTF-8. The
snapshot normalization is therefore identity; offsets are Python character
indices over that same string, never byte offsets.
Empty source content is allowed and receives the standard SHA-256 of ``b""``;
it remains reproducible evidence but cannot support a non-empty citation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime


QUOTE_PREVIEW_LIMIT = 48
OFFICIAL_SOURCE_IDS = frozenset({"main", "main_content", "title", "chrome", "page"})


class SnapshotError(ValueError):
    """Base class for secret-free snapshot construction failures."""


class DuplicateSourceError(SnapshotError):
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        super().__init__(f"duplicate source_id: {source_id}")


class SourceNotAllowedError(SnapshotError):
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        super().__init__(f"source_id fuera de allowlist oficial V2: {source_id}")


def _assert_source_allowed(source_id: str) -> None:
    if source_id not in OFFICIAL_SOURCE_IDS:
        raise SourceNotAllowedError(source_id)


def _quote_preview(quote: str) -> str:
    compact = quote.replace("\r", "\\r").replace("\n", "\\n")
    return compact[:QUOTE_PREVIEW_LIMIT]


class CitationVerificationError(SnapshotError):
    """One citation failed without exposing source content."""

    def __init__(
        self,
        *,
        source_id: str,
        reason: str,
        quote: str = "",
        occurrence_count: int | None = None,
    ) -> None:
        self.source_id = source_id
        self.reason = reason
        self.quote_preview = _quote_preview(quote)
        # Only populated for reason=quote_ambiguous today: the real count of
        # non-overlapping literal occurrences of ``quote`` in the source, so
        # repair guidance can be specific instead of a binary "it repeats".
        self.occurrence_count = occurrence_count
        preview = f", quote_preview={self.quote_preview!r}" if quote else ""
        occurrence = (
            f", occurrence_count={occurrence_count}"
            if occurrence_count is not None else ""
        )
        super().__init__(
            f"citation rejected: source_id={source_id}, reason={reason}"
            f"{preview}{occurrence}"
        )


@dataclass(frozen=True)
class CitationFailure:
    index: int
    source_id: str
    reason: str
    quote_preview: str
    occurrence_count: int | None = None


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
        _assert_source_allowed(self.source_id)
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

    def normalized_text(self, source_id: str) -> str:
        """Return the exact string used for citation character offsets."""
        return self.get_source(source_id).content


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
    start: int
    end: int
    quote: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise CitationVerificationError(
                source_id=str(self.source_id), reason="invalid_source_id"
            )
        _assert_source_allowed(self.source_id)
        if not isinstance(self.quote, str) or not self.quote:
            raise CitationVerificationError(
                source_id=self.source_id, reason="empty_quote"
            )
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


def anchor_citation(
    snapshot: EvidenceSnapshot,
    raw: Mapping[str, object],
    *,
    require_offsets: bool = False,
) -> Citation:
    """Hydrate omitted offsets only for one unique literal occurrence.

    Unknown envelope fields are ignored. ``source_id`` and ``quote`` are always
    mandatory. Both offsets may be omitted only at the model-response seam.
    """
    if not isinstance(raw, Mapping):
        raise CitationVerificationError(
            source_id="<missing>", reason="citation_not_object"
        )
    missing_core = tuple(name for name in ("source_id", "quote") if name not in raw)
    has_start = "start" in raw
    has_end = "end" in raw
    if missing_core or (require_offsets and (not has_start or not has_end)):
        raise CitationVerificationError(
            source_id=str(raw.get("source_id", "<missing>")),
            reason="missing_required_fields",
        )
    source_id = raw["source_id"]
    quote = raw["quote"]
    if not isinstance(source_id, str) or not source_id.strip():
        raise CitationVerificationError(
            source_id=str(source_id), reason="invalid_source_id"
        )
    _assert_source_allowed(source_id)
    if not isinstance(quote, str) or not quote:
        raise CitationVerificationError(source_id=source_id, reason="empty_quote")
    if has_start != has_end:
        raise CitationVerificationError(
            source_id=source_id, reason="incomplete_offsets", quote=quote
        )
    if has_start:
        citation = Citation(
            source_id=source_id,
            start=raw["start"],  # type: ignore[arg-type]
            end=raw["end"],  # type: ignore[arg-type]
            quote=quote,
        )
        verify_citation(snapshot, citation)
        return citation

    text = snapshot.normalized_text(source_id)
    if not text:
        raise CitationVerificationError(
            source_id=source_id, reason="empty_source", quote=quote
        )
    position = text.find(quote)
    if position < 0:
        raise CitationVerificationError(
            source_id=source_id, reason="quote_not_found", quote=quote
        )
    if text.find(quote, position + 1) >= 0:
        # str.count matches text.find's left-to-right, non-overlapping scan,
        # so this is the exact number of ambiguous anchor candidates a repair
        # attempt is choosing between (not just "more than one").
        raise CitationVerificationError(
            source_id=source_id,
            reason="quote_ambiguous",
            quote=quote,
            occurrence_count=text.count(quote),
        )
    citation = Citation(source_id, position, position + len(quote), quote)
    verify_citation(snapshot, citation)
    return citation


def verify_citation(
    snapshot: EvidenceSnapshot,
    citation: Citation,
) -> None:
    """Verify one strict citation against the snapshot's normalized string."""
    if not isinstance(citation, Citation):
        raise CitationVerificationError(
            source_id="<invalid>", reason="invalid_citation_type"
        )
    text = snapshot.normalized_text(citation.source_id)
    if not text:
        raise CitationVerificationError(
            source_id=citation.source_id,
            reason="empty_source",
            quote=citation.quote,
        )
    if citation.end > len(text):
        raise CitationVerificationError(
            source_id=citation.source_id,
            reason="offset_out_of_bounds",
            quote=citation.quote,
        )
    if text[citation.start:citation.end] != citation.quote:
        reason = "offset_quote_mismatch" if citation.quote in text else "quote_not_found"
        raise CitationVerificationError(
            source_id=citation.source_id,
            reason=reason,
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
) -> CitationVerificationReport:
    """Verify every citation, collecting all failures before rejecting a batch."""
    citation_tuple = tuple(citations)
    failures: list[CitationFailure] = []
    verified: list[int] = []
    source_ids: list[str] = []
    for index, citation in enumerate(citation_tuple):
        try:
            verify_citation(snapshot, citation)
        except CitationVerificationError as exc:
            failures.append(CitationFailure(
                index=index,
                source_id=exc.source_id,
                reason=exc.reason,
                quote_preview=exc.quote_preview,
                occurrence_count=getattr(exc, "occurrence_count", None),
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
