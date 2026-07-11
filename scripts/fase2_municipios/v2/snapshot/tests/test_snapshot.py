"""Offline tests for frozen snapshots and literal citation verification."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from scripts.fase2_municipios.v2.snapshot import (
    anchor_citation,
    Citation,
    CitationBatchVerificationError,
    CitationVerificationError,
    DuplicateSourceError,
    EvidenceSource,
    SourceNotAllowedError,
    build_snapshot,
    verify_all,
    verify_citation,
)


pytestmark = pytest.mark.offline
RETRIEVED_AT = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
TEST_SOURCE_IDS = {"a": "main", "b": "title", "c": "chrome", "same": "main", "known": "main", "empty": "main"}


def source(source_id: str, content: str, *, url: str | None = None) -> EvidenceSource:
    source_id = TEST_SOURCE_IDS.get(source_id, source_id)
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
    assert raised.value.source_id == "main"


def test_source_allowlist_tripwire_accepts_official_role_and_rejects_foreign() -> None:
    assert source("main", "official").source_id == "main"
    with pytest.raises(SourceNotAllowedError, match="allowlist oficial V2") as raised:
        source("radar_portal", "foreign")
    assert raised.value.source_id == "radar_portal"


def test_exact_textual_quote_passes_and_missing_quote_fails() -> None:
    snapshot = build_snapshot([source("page", "Concursos Públicos\n1 resultado")])

    citation = anchor_citation(snapshot, {"source_id": "page", "quote": "Concursos Públicos"})
    verify_citation(snapshot, citation)
    with pytest.raises(CitationVerificationError) as raised:
        anchor_citation(snapshot, {"source_id": "page", "quote": "concursos públicos"})
    assert raised.value.reason == "quote_not_found"


def test_unknown_source_is_fail_closed() -> None:
    snapshot = build_snapshot([source("known", "evidence")])
    with pytest.raises(SourceNotAllowedError) as raised:
        anchor_citation(snapshot, {"source_id": "missing", "quote": "evidence"})
    assert raised.value.source_id == "missing"


def test_raw_offsets_must_match_quote_exactly() -> None:
    content = "Prefixo — Concurso Público — sufixo"
    snapshot = build_snapshot([source("page", content)])
    quote = "Concurso Público"
    start = content.index(quote)

    verify_citation(snapshot, Citation("page", start, start + len(quote), quote))
    with pytest.raises(CitationVerificationError) as raised:
        verify_citation(
            snapshot,
            Citation("page", start + 1, start + len(quote) + 1, quote),
        )
    assert raised.value.reason == "offset_quote_mismatch"


def test_verify_all_identifies_bad_index_without_dumping_source_content() -> None:
    secret_tail = "SENSITIVE-SOURCE-CONTENT-" * 20
    snapshot = build_snapshot([source("page", f"good one\ngood two\n{secret_tail}")])
    citations = (
        Citation("page", 0, 8, "good one"),
        Citation("page", 9, 64, "missing quote that is intentionally short"),
        Citation("page", 9, 17, "good two"),
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
    citation = Citation("page", 0, len("Alpha Beta"), "Alpha Beta")
    collapse_spaces = lambda text: " ".join(text.split())

    with pytest.raises(TypeError):
        verify_citation(snapshot, citation, normalizer=collapse_spaces)
    with pytest.raises(CitationVerificationError) as raised:
        verify_citation(snapshot, citation)
    assert raised.value.reason == "quote_not_found"


@pytest.mark.parametrize("missing", ["source_id", "start", "end", "quote"])
def test_citation_contract_rejects_any_missing_required_field(missing: str) -> None:
    snapshot = build_snapshot([source("main", "evidence")])
    raw = {"source_id": "main", "start": 0, "end": 8, "quote": "evidence"}
    raw.pop(missing)

    with pytest.raises((CitationVerificationError, TypeError, KeyError)):
        anchor_citation(snapshot, raw, require_offsets=True)


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 1), (0, 9), (4, 3), (3, 3)],
)
def test_invalid_offset_ranges_are_rejected(start: int, end: int) -> None:
    snapshot = build_snapshot([source("main", "evidence")])
    with pytest.raises(CitationVerificationError):
        anchor_citation(
            snapshot,
            {"source_id": "main", "start": start, "end": end, "quote": "evidence"},
            require_offsets=True,
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [(True, 1), (0, False), (0.0, 1), (0, 1.0), ("0", 1), (0, "1"), (None, 1), (0, None)],
)
def test_invalid_offset_types_are_rejected(start: object, end: object) -> None:
    snapshot = build_snapshot([source("main", "evidence")])
    with pytest.raises(CitationVerificationError):
        anchor_citation(
            snapshot,
            {"source_id": "main", "start": start, "end": end, "quote": "e"},
            require_offsets=True,
        )


def test_absent_or_empty_source_is_rejected_without_crashing() -> None:
    snapshot = build_snapshot([source("empty", "")])
    with pytest.raises(CitationVerificationError) as empty:
        anchor_citation(snapshot, {"source_id": "main", "quote": "x"})
    assert empty.value.reason == "empty_source"
    with pytest.raises(SourceNotAllowedError) as absent:
        anchor_citation(snapshot, {"source_id": "absent", "quote": "x"})
    assert absent.value.source_id == "absent"


def test_unicode_offsets_are_python_str_character_indices() -> None:
    text = "Ação pública — seleção"
    snapshot = build_snapshot([source("main", text)])
    quote = "pública — seleção"
    citation = anchor_citation(snapshot, {"source_id": "main", "quote": quote})

    assert citation.start == text.index(quote)
    assert citation.end == citation.start + len(quote)
    assert citation.end < len(text.encode("utf-8"))
    verify_citation(snapshot, citation)


def test_main_and_chrome_citations_are_distinct_even_with_same_quote() -> None:
    snapshot = build_snapshot([
        source("main", "Editais oficiais"),
        source("chrome", "DOM: Editais oficiais"),
    ])
    main = anchor_citation(snapshot, {"source_id": "main", "quote": "Editais oficiais"})
    chrome = anchor_citation(snapshot, {"source_id": "chrome", "quote": "Editais oficiais"})

    assert main.source_id == "main" and main.start == 0
    assert chrome.source_id == "chrome" and chrome.start == 5
    with pytest.raises(CitationVerificationError):
        anchor_citation(snapshot, {"quote": "Editais oficiais"})


def test_repeated_quote_without_offsets_fails_closed_but_explicit_offsets_disambiguate() -> None:
    snapshot = build_snapshot([source("main", "Edital / Edital")])
    with pytest.raises(CitationVerificationError) as ambiguous:
        anchor_citation(snapshot, {"source_id": "main", "quote": "Edital"})
    assert ambiguous.value.reason == "quote_ambiguous"

    citation = anchor_citation(
        snapshot,
        {"source_id": "main", "start": 9, "end": 15, "quote": "Edital"},
    )
    verify_citation(snapshot, citation)


def test_unknown_citation_fields_are_ignored() -> None:
    snapshot = build_snapshot([source("main", "evidence")])
    citation = anchor_citation(
        snapshot,
        {"source_id": "main", "quote": "evidence", "future_metadata": {"v": 2}},
    )
    assert citation == Citation("main", 0, 8, "evidence")
