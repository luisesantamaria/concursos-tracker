"""Offline tests for the citation-repair instruction's occurrence enrichment.

Motivated by the Pelotas/CP live failure (12-jul): r1 and r2 both cited the
same duplicated chrome text ('Prefeitura Municipal de Pelotas' in header AND
footer) and the single repair round repeated the identical ambiguous quote.
This file covers _citation_repair_instruction directly (unit level) and one
end-to-end _run_direct flow reproducing that exact shape of failure.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from scripts.fase2_municipios.v2.agents import base
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    CitationFailure,
    CitationVerificationError,
    EvidenceSource,
    anchor_citation,
    build_snapshot,
)


pytestmark = pytest.mark.offline

ORIGINAL_INSTRUCTION_TEMPLATE = (
    "CITATION_REPAIR (unica oportunidad): tu respuesta fue rechazada por "
    "el validador determinista de citas. Fallas exactas:\n{detail}\n"
    "Reenvia el JSON COMPLETO con el mismo schema corrigiendo SOLO las "
    "citas fallidas: cada quote debe ser copia LITERAL del snapshot y "
    "ocurrir EXACTAMENTE UNA VEZ en su fuente; si un texto se repite, "
    "EXTIENDE la quote con el contexto vecino hasta hacerla unica. No "
    "cambies tu decision, no inventes contenido y no emitas start/end."
)


def _exc_with_cause(cause: BaseException | None) -> BaseException:
    exc = RuntimeError("outer citation rejection")
    exc.__cause__ = cause
    return exc


class _Batch(Exception):
    """Minimal duck-type of CitationBatchVerificationError for unit tests.

    Must subclass BaseException: __cause__ assignment requires it, exactly
    like the real CitationBatchVerificationError._citation_repair_instruction
    reads via ``exc.__cause__``.
    """

    def __init__(self, failures: tuple[Any, ...]) -> None:
        super().__init__("fixture batch")
        self.failures = failures


# --- Unit level: _citation_repair_instruction ------------------------------


def test_repair_instruction_without_occurrence_count_matches_original_wording() -> None:
    """Sin occurrence_count la instruccion actual se mantiene sin cambios."""
    cause = CitationVerificationError(
        source_id="main", reason="quote_not_found", quote="Concurso 02"
    )
    text = base.AgentRunner._citation_repair_instruction(_exc_with_cause(cause))

    detail = f"- source_id=main reason=quote_not_found quote_preview={cause.quote_preview!r}"
    assert text == ORIGINAL_INSTRUCTION_TEMPLATE.format(detail=detail)
    assert "Estrategia recomendada" not in text
    assert "aparece" not in text


def test_repair_instruction_includes_occurrence_count_and_strategy_when_available() -> None:
    cause = CitationVerificationError(
        source_id="chrome",
        reason="quote_ambiguous",
        quote="Prefeitura Municipal de Pelotas",
        occurrence_count=2,
    )
    text = base.AgentRunner._citation_repair_instruction(_exc_with_cause(cause))

    assert "CITATION_REPAIR" in text
    assert "quote_ambiguous" in text
    assert "la cita aparece 2 veces" in text
    assert "Estrategia recomendada" in text
    assert "linea estructuralmente unica" in text
    assert "contexto vecino" in text
    # El cuerpo original se preserva integro, la estrategia solo se agrega.
    assert text.startswith(ORIGINAL_INSTRUCTION_TEMPLATE.split("{detail}")[0])


def test_repair_instruction_batch_only_annotates_failures_with_a_count() -> None:
    failures = (
        CitationFailure(
            index=0, source_id="chrome", reason="quote_ambiguous",
            quote_preview="Prefeitura Municipal de Pelotas", occurrence_count=2,
        ),
        CitationFailure(
            index=1, source_id="main", reason="quote_not_found",
            quote_preview="Concurso 02",
        ),
    )
    text = base.AgentRunner._citation_repair_instruction(
        _exc_with_cause(_Batch(failures))
    )

    assert text.count("aparece") == 1
    assert "la cita aparece 2 veces" in text
    assert "Estrategia recomendada" in text  # triggered because ANY failure has a count


def test_repair_instruction_with_no_cause_still_returns_original_shape() -> None:
    text = base.AgentRunner._citation_repair_instruction(_exc_with_cause(None))
    assert "CITATION_REPAIR" in text
    assert "Estrategia recomendada" not in text


# --- End-to-end: _run_direct reproducing the Pelotas/CP shape --------------

QUOTE_ONLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "citations"],
    "properties": {
        "decision": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_id", "quote"],
                "properties": {
                    "source_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
            },
        },
    },
}


class SequencedClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, int]] = []

    def generate_structured(self, contents: Any, *, estimated_tokens: int) -> Any:
        self.calls.append((contents, estimated_tokens))
        return self.responses.pop(0)


def _citations(output: Any) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            source_id=item["source_id"], start=item["start"],
            end=item["end"], quote=item["quote"],
        )
        for item in output["citations"]
    )


def _hydrating_prepare(snapshot: Any, output: Any) -> Any:
    prepared = copy.deepcopy(dict(output))
    for item in prepared["citations"]:
        item.pop("start", None)
        item.pop("end", None)
        citation = anchor_citation(snapshot, item, require_offsets=False)
        item["start"] = citation.start
        item["end"] = citation.end
    return prepared


def _repair_runner(client: Any) -> base.AgentRunner:
    return base.AgentRunner(
        role="fixture",
        system_prompt="Return the structured decision.",
        client=client,
        output_schema=QUOTE_ONLY_SCHEMA,
        extract_citations=_citations,
        prepare_output=_hydrating_prepare,
        requires_citations=lambda output: output["decision"] == "indice_oficial",
        tools=None,
    )


def _pelotas_like_snapshot():
    """Header AND footer repeat the exact same chrome text (2 occurrences),
    reproducing the shape of the real Pelotas/CP r1==r2 failure."""
    return build_snapshot((EvidenceSource(
        source_id="chrome",
        url="https://pelotas.rs.gov.br/concursos",
        retrieved_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        content=(
            "Prefeitura Municipal de Pelotas\n"
            "Concursos Publicos e Processos Seletivos\n"
            "Rodape: Prefeitura Municipal de Pelotas - Todos os direitos reservados"
        ),
    ),))


def test_real_ambiguous_repair_flow_carries_occurrence_count_and_strategy() -> None:
    ambiguous = {
        "decision": "indice_oficial",
        "citations": [{"source_id": "chrome", "quote": "Prefeitura Municipal de Pelotas"}],
    }
    client = SequencedClient([ambiguous, dict(ambiguous)])

    result = _repair_runner(client).run(
        snapshot=_pelotas_like_snapshot(), task="certify fixture"
    )

    # Un solo techo de reparacion (politica 12-jul): repetir la misma cita
    # ambigua en la reparacion sigue siendo fail-closed, sin rondas extra.
    assert isinstance(result, base.SnapshotInvalidOutput)
    assert len(client.calls) == 2

    repair_text = client.calls[1][0][-1]["parts"][0]["text"]
    assert "CITATION_REPAIR" in repair_text
    assert "quote_ambiguous" in repair_text
    assert "la cita aparece 2 veces" in repair_text
    assert "Estrategia recomendada" in repair_text
