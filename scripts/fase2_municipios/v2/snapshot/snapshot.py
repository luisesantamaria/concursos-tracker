"""Immutable, content-addressed evidence with verifiable literal citations.

This module accepts content already captured by a caller. It performs no fetch,
file access, path resolution, symlink handling or clock lookup. Raw rendered text
is retained unchanged as a Python ``str`` and hashed explicitly as UTF-8. The
snapshot normalization is therefore identity; offsets are Python character
indices over that same string, never byte offsets.
Empty source content is allowed and receives the standard SHA-256 of ``b""``;
it remains reproducible evidence but cannot support a non-empty citation.

Citation comparison precedence (SUB-CAUSA 1/2, holdout 12-jul):
- Unicode form: stored content arrives NFC-canonical from the decode layer
  (``eval/live_abc_adapter.py``); a foreign quote (model output) may arrive in
  a different normal form (e.g. NFD). Every literal search/compare below
  normalizes the quote to NFC before matching -- never the stored content, so
  offsets stay exact indices into the untouched string.
- Ambiguity: a quote existing 2+ times in the source proves EXISTENCE of
  evidence; offset ambiguity alone is not grounds to reject it. Omitted-offset
  anchoring resolves to the first literal occurrence and records
  ``ambiguous_occurrences`` informationally. Only a quote that does not exist
  at all fails closed (``quote_not_found``).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
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
        # Historical: occurrence_count used to accompany the (now-removed)
        # quote_ambiguous failure. Ambiguity no longer fails closed (see
        # Citation.ambiguous_occurrences), but the field stays on this
        # exception/CitationFailure for any other reason that wants to carry
        # a real occurrence count instead of a binary signal.
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
    # Informational only (never a rejection reason): set when this citation was
    # anchored without explicit offsets and its quote occurred 2+ times in the
    # source. Holds the real non-overlapping occurrence count; None means the
    # citation was unambiguous (or its offsets were given explicitly, which
    # already pins one occurrence and skips the ambiguity check entirely).
    ambiguous_occurrences: int | None = None

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
    """Hydrate omitted offsets by anchoring to the first literal occurrence.

    Unknown envelope fields are ignored. ``source_id`` and ``quote`` are always
    mandatory. Both offsets may be omitted only at the model-response seam. A
    quote repeated in the source anchors to its first occurrence and is not
    rejected for that alone (see ``Citation.ambiguous_occurrences``); only a
    quote that does not exist in the source fails closed.
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
    # NFC precedence (see module docstring): the stored source is assumed
    # already NFC-canonical (decode layer); the foreign quote is normalized
    # here before searching so accented literals in a different Unicode
    # normal form (e.g. NFD) still match. Only the search copy is normalized;
    # ``position``/offsets remain exact indices into ``text`` because NFC is
    # idempotent on already-NFC content (normalize(text) == text in that case).
    normalized_quote = unicodedata.normalize("NFC", quote)
    position = text.find(normalized_quote)
    if position < 0:
        raise CitationVerificationError(
            source_id=source_id, reason="quote_not_found", quote=quote
        )
    # Existencia de evidencia es el requisito, no unicidad de offset (SUB-CAUSA
    # 1, holdout 12-jul): una quote que aparece 2+ veces YA prueba que el texto
    # existe en la fuente. Ya no se rechaza -- se ancla a la primera ocurrencia
    # (str.find's left-to-right, non-overlapping scan) y se registra un flag
    # puramente informativo con el conteo real, nunca un motivo de fallo.
    occurrence_count = None
    if text.find(normalized_quote, position + 1) >= 0:
        occurrence_count = text.count(normalized_quote)
    citation = Citation(
        source_id, position, position + len(normalized_quote), quote,
        ambiguous_occurrences=occurrence_count,
    )
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
    slice_ = text[citation.start:citation.end]
    if slice_ != citation.quote:
        # NFC precedence (see module docstring): a citation carried through
        # from anchor_citation, or re-verified downstream (e.g. the strict
        # final gate), may still hold its quote in a different Unicode normal
        # form than the (already-NFC) snapshot slice. Only fall back to NFC
        # comparison when the raw slice-equality fails, so the common already-
        # matching case pays no extra normalization cost.
        if unicodedata.normalize("NFC", slice_) != unicodedata.normalize(
            "NFC", citation.quote
        ):
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
