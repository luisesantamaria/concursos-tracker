"""Offline tests for frozen snapshots and literal citation verification."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import unicodedata

import pytest

from scripts.fase2_municipios.v2.snapshot import (
    anchor_citation,
    Citation,
    CitationBatchVerificationError,
    CitationFailure,
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


def test_nfd_quote_anchors_and_verifies_against_nfc_snapshot_text() -> None:
    """SUB-CAUSA 2c fix (holdout 12-jul: doutormauriciocardoso/saodomingosdosul
    /inhacora/estrela). El snapshot llega NFC-canonico desde la capa de decode
    (eval/live_abc_adapter.py); una cita en forma NFD (p.ej. copiada de un
    render que descompone acentos) debe anclar y verificar igual -- la forma
    Unicode no es motivo de rechazo, solo la ausencia literal lo es."""
    nfc_text = unicodedata.normalize(
        "NFC", "Processo Seletivo - Educação Municipal"
    )
    assert nfc_text == "Processo Seletivo - Educação Municipal"  # ya viene compuesto
    snapshot = build_snapshot([source("main", nfc_text)])

    nfd_quote = unicodedata.normalize("NFD", "Educação Municipal")
    assert nfd_quote != "Educação Municipal"  # confirma que son formas distintas

    citation = anchor_citation(snapshot, {"source_id": "main", "quote": nfd_quote})
    assert citation.quote == nfd_quote  # se preserva lo que dijo el modelo
    verify_citation(snapshot, citation)

    # El mismo NFD tambien verifica cuando llega con offsets explicitos (ruta
    # que toma el gate final tras el anclaje del certifier/prosecutor).
    explicit = anchor_citation(
        snapshot,
        {
            "source_id": "main",
            "start": citation.start,
            "end": citation.end,
            "quote": nfd_quote,
        },
        require_offsets=True,
    )
    verify_citation(snapshot, explicit)


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


def test_repeated_quote_without_offsets_anchors_first_occurrence_and_explicit_offsets_still_disambiguate() -> None:
    """SUB-CAUSA 1 fix (holdout 12-jul): existencia de evidencia alcanza --
    ambiguedad de offset ya no rechaza. Sin offsets se ancla a la primera
    ocurrencia (con el flag informativo); offsets explicitos siguen
    permitiendo anclar a cualquier otra ocurrencia puntual."""
    snapshot = build_snapshot([source("main", "Edital / Edital")])
    citation = anchor_citation(snapshot, {"source_id": "main", "quote": "Edital"})
    assert citation.start == 0
    assert citation.end == len("Edital")
    assert citation.ambiguous_occurrences == 2
    verify_citation(snapshot, citation)

    other = anchor_citation(
        snapshot,
        {"source_id": "main", "start": 9, "end": 15, "quote": "Edital"},
    )
    assert other.start == 9
    assert other.ambiguous_occurrences is None  # explicit offsets skip the check
    verify_citation(snapshot, other)


def test_ambiguous_citation_records_real_non_overlapping_occurrence_count() -> None:
    """Ambiguedad hoy solo era binaria (find(quote, position+1) >= 0). El
    conteo real deja de ser 'aparece mas de una vez' para decir cuantas, y
    ahora se registra como flag informativo en la cita anclada, no en un
    fallo."""
    snapshot = build_snapshot([source("main", "Edital / Edital / Edital")])
    citation = anchor_citation(snapshot, {"source_id": "main", "quote": "Edital"})
    assert citation.ambiguous_occurrences == 3
    verify_citation(snapshot, citation)


def test_two_occurrences_are_counted_exactly_as_two() -> None:
    snapshot = build_snapshot([source("main", "Concurso 01 aberto / Concurso 01 fim")])
    citation = anchor_citation(snapshot, {"source_id": "main", "quote": "Concurso 01"})
    assert citation.ambiguous_occurrences == 2


def test_quote_not_found_is_the_only_remaining_absence_failure() -> None:
    """La UNICA razon que sigue rechazando por ausencia real de evidencia: el
    texto no existe en la fuente. occurrence_count no aplica a estos fallos
    (quote_not_found, empty_source) -- nunca inventa un conteo."""
    snapshot = build_snapshot([source("main", "unico texto sin repetir")])
    with pytest.raises(CitationVerificationError) as not_found:
        anchor_citation(snapshot, {"source_id": "main", "quote": "no existe"})
    assert not_found.value.reason == "quote_not_found"
    assert not_found.value.occurrence_count is None

    empty_snapshot = build_snapshot([source("empty", "")])
    with pytest.raises(CitationVerificationError) as empty:
        anchor_citation(empty_snapshot, {"source_id": "main", "quote": "x"})
    assert empty.value.reason == "empty_source"
    assert empty.value.occurrence_count is None


def test_existing_citation_reader_code_unaware_of_ambiguity_flag_is_unaffected() -> None:
    """Compatibilidad: codigo existente que solo lee .source_id/.start/.end/
    .quote (sin conocer ambiguous_occurrences) sigue funcionando igual, y la
    cita anclada de una quote ambigua se comporta como cualquier otra."""
    snapshot = build_snapshot([source("main", "Edital / Edital")])
    citation = anchor_citation(snapshot, {"source_id": "main", "quote": "Edital"})
    assert citation.source_id == "main"
    assert citation.quote == "Edital"
    assert (citation.start, citation.end) == (0, len("Edital"))


def test_verify_all_forwards_occurrence_count_into_citation_failure_when_present() -> None:
    """verify_all/CitationFailure no rompe el batch: cuando el fallo subyacente
    trae occurrence_count, se propaga; sin el, se mantiene None (default)."""
    failure_with_count = CitationFailure(
        index=0, source_id="main", reason="quote_ambiguous",
        quote_preview="Edital", occurrence_count=3,
    )
    failure_without_count = CitationFailure(
        index=1, source_id="main", reason="quote_not_found", quote_preview="x",
    )
    assert failure_with_count.occurrence_count == 3
    assert failure_without_count.occurrence_count is None


def test_unknown_citation_fields_are_ignored() -> None:
    snapshot = build_snapshot([source("main", "evidence")])
    citation = anchor_citation(
        snapshot,
        {"source_id": "main", "quote": "evidence", "future_metadata": {"v": 2}},
    )
    assert citation == Citation("main", 0, 8, "evidence")
