"""Offline tests for frozen snapshots and literal citation verification."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    CitationBatchVerificationError,
    CitationVerificationError,
    DuplicateSourceError,
    EvidenceSource,
    build_snapshot,
    verify_all,
    verify_citation,
)


pytestmark = pytest.mark.offline
RETRIEVED_AT = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def source(source_id: str, content: str, *, url: str | None = None) -> EvidenceSource:
    return EvidenceSource(
        source_id=source_id,
        url=url or f"https://example.invalid/{source_id}",
        retrieved_at=RETRIEVED_AT,
        content=content,
    )


def test_hash_is_stable_under_input_reordering_and_changes_with_content() -> None:
    first = source("a", "Conteúdo alfa")
    second = source("b", "Conteúdo beta")
    third = source("c", "Conteúdo gama")

    ordered = build_snapshot([first, second, third])
    reordered = build_snapshot([third, first, second])
    changed = build_snapshot([first, source("b", "Conteúdo alterado"), third])

    assert ordered.sources == tuple(sorted(ordered.sources, key=lambda item: item.source_id))
    assert ordered.snapshot_sha256 == reordered.snapshot_sha256
    assert ordered.snapshot_sha256 != changed.snapshot_sha256
    assert first.content_sha256 != source("a", "Conteúdo alfa!").content_sha256


def test_snapshot_and_sources_are_deeply_immutable() -> None:
    snapshot = build_snapshot([source("a", "raw text"), source("b", "other")])

    assert isinstance(snapshot.sources, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot.snapshot_sha256 = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.sources[0].content = "changed"
    with pytest.raises(TypeError):
        snapshot.sources[0] = source("c", "changed")


def test_duplicate_source_id_is_rejected() -> None:
    with pytest.raises(DuplicateSourceError) as raised:
        build_snapshot([source("same", "one"), source("same", "two")])
    assert raised.value.source_id == "same"


def test_exact_textual_quote_passes_and_missing_quote_fails() -> None:
    snapshot = build_snapshot([source("page", "Concursos Públicos\n1 resultado")])

    verify_citation(snapshot, Citation("page", "Concursos Públicos"))
    with pytest.raises(CitationVerificationError) as raised:
        verify_citation(snapshot, Citation("page", "concursos públicos"))
    assert raised.value.reason == "quote_not_found"


def test_unknown_source_is_fail_closed() -> None:
    snapshot = build_snapshot([source("known", "evidence")])
    with pytest.raises(CitationVerificationError) as raised:
        verify_citation(snapshot, Citation("missing", "evidence"))
    assert raised.value.reason == "source_not_found"
    assert raised.value.source_id == "missing"


def test_raw_offsets_must_match_quote_exactly() -> None:
    content = "Prefixo — Concurso Público — sufixo"
    snapshot = build_snapshot([source("page", content)])
    quote = "Concurso Público"
    start = content.index(quote)

    verify_citation(snapshot, Citation("page", quote, start=start, end=start + len(quote)))
    with pytest.raises(CitationVerificationError) as raised:
        verify_citation(
            snapshot,
            Citation("page", quote, start=start + 1, end=start + len(quote) + 1),
        )
    assert raised.value.reason == "offset_quote_mismatch"


def test_verify_all_identifies_bad_index_without_dumping_source_content() -> None:
    secret_tail = "SENSITIVE-SOURCE-CONTENT-" * 20
    snapshot = build_snapshot([source("page", f"good one\ngood two\n{secret_tail}")])
    citations = (
        Citation("page", "good one"),
        Citation("page", "missing quote that is intentionally short"),
        Citation("page", "good two"),
    )

    with pytest.raises(CitationBatchVerificationError) as raised:
        verify_all(snapshot, citations)

    assert len(raised.value.failures) == 1
    assert raised.value.failures[0].index == 1
    assert raised.value.failures[0].source_id == "page"
    assert raised.value.failures[0].reason == "quote_not_found"
    assert secret_tail not in str(raised.value)
    assert len(raised.value.failures[0].quote_preview) <= 48


def test_injected_whitespace_normalizer_is_opt_in_and_offsets_remain_raw() -> None:
    snapshot = build_snapshot([source("page", "Alpha    Beta\nGamma")])
    citation = Citation("page", "Alpha Beta")
    collapse_spaces = lambda text: " ".join(text.split())

    with pytest.raises(CitationVerificationError):
        verify_citation(snapshot, citation)
    verify_citation(snapshot, citation, normalizer=collapse_spaces)

    with pytest.raises(CitationVerificationError) as raised:
        verify_citation(
            snapshot,
            Citation("page", "Alpha Beta", start=0, end=len("Alpha Beta")),
            normalizer=collapse_spaces,
        )
    assert raised.value.reason == "offset_quote_mismatch"
