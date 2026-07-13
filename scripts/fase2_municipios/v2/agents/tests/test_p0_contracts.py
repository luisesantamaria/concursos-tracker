"""P0 offline contracts for direct A/B/C structured outputs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.fase2_municipios.v2.agents import certifier, prosecutor
from scripts.fase2_municipios.v2.agents.base import AgentOutputRejected
from scripts.fase2_municipios.v2.agents.schemas import (
    JUDGE_OUTPUT_SCHEMA,
    PROSECUTOR_OUTPUT_SCHEMA,
)
from scripts.fase2_municipios.v2.gemini.schema_validation import (
    JsonSchemaValidationError,
    validate_json_schema,
)
from scripts.fase2_municipios.v2.loader import load_canonical_resources
from scripts.fase2_municipios.v2.snapshot import (
    CitationVerificationError,
    EvidenceSource,
    build_snapshot,
)


pytestmark = pytest.mark.offline
ACCUSATION_CODES = tuple(sorted(prosecutor.REQUIRED_ACCUSATION_CODES))


def _snapshot(text: str = "ASCII ação 😀 fim"):
    return build_snapshot((EvidenceSource(
        source_id="main",
        url="https://example.invalid/indice",
        retrieved_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        content=text,
    ),))


def _affirmative_output(*, authority_citation: bool, offsets: bool = True):
    text = _snapshot().sources[0].content
    citations = []
    for dimension, quote in (
        ("identity", "ASCII"),
        ("page_role", "ação"),
        ("bucket", "😀"),
        ("stability", "fim"),
    ):
        start = text.index(quote)
        item = {
            "dimension": dimension,
            "quote": quote,
            "source_field": "main_content",
            "source_id": "main",
        }
        if offsets:
            item.update(start=start, end=start + len(quote))
        citations.append(item)
    if authority_citation:
        citations.append({
            "dimension": "authority",
            "quote": "ASCII",
            "source_field": "provenance",
            "source_id": "main",
            "start": 0,
            "end": len("ASCII"),
        })
    return {
        "decision": "indice_oficial",
        "citations": citations,
    }


def _prosecutor_output(*, result: str, unresolved: str | None = None):
    return {
        "result": result,
        "reason": "auditoria fechada",
        "confidence": "high",
        "insufficiency": "none",
        "accusations": [
            {
                "code": code,
                "outcome": "unresolved" if code == unresolved else "discarded",
                "citations": [],
            }
            for code in ACCUSATION_CODES
        ],
        "citations": [],
        "tool_request": None,
        "failure_mode_proposal": None,
    }


def test_max_length_counts_python_unicode_code_points() -> None:
    schema = {"type": "string", "maxLength": 3}
    validate_json_schema("á😀", schema)
    validate_json_schema("á😀x", schema)
    with pytest.raises(JsonSchemaValidationError) as raised:
        validate_json_schema("á😀xy", schema)
    assert raised.value.rule == "maxLength"


def test_direct_certifier_schema_is_closed_and_bounded() -> None:
    schema = load_canonical_resources().references["schema.json"]
    assert "insufficiency" in schema["required"]
    assert schema["properties"]["reason"]["maxLength"] == 400
    assert schema["properties"]["tool_request"] == {"type": "null"}
    assert schema["properties"]["learning_proposal"] == {"type": "null"}
    citation = schema["properties"]["citations"]["items"]
    assert citation["additionalProperties"] is False


def test_affirmative_A_requires_authority_and_hydrates_offsets() -> None:
    """Politica 12-jul (aprobada por Luis): el modelo entrega source_id+quote;
    los offsets los computa el codigo exigiendo ocurrencia literal UNICA. La
    dimension authority sigue siendo obligatoria para afirmativa."""
    with pytest.raises(AgentOutputRejected, match="authority"):
        certifier._certifier_invariants(
            _affirmative_output(authority_citation=False)
        )
    snapshot = _snapshot()
    prepared = certifier._prepare_certifier_output(
        snapshot,
        _affirmative_output(authority_citation=True, offsets=False),
    )
    text = snapshot.sources[0].content
    for item in prepared["citations"]:
        assert text[item["start"]:item["end"]] == item["quote"]


def test_model_offsets_are_ignored_and_recomputed_deterministically() -> None:
    """Offsets emitidos por el modelo son ruido demostrado (canario r1/r2): se
    descartan y se recomputan por anclaje unico. Offsets erroneos del modelo no
    pueden ni confirmar ni romper una cita literal valida."""
    snapshot = _snapshot()
    output = _affirmative_output(authority_citation=True, offsets=True)
    for item in output["citations"]:
        item["start"], item["end"] = 1, 3  # basura deliberada estilo canario r2
    prepared = certifier._prepare_certifier_output(snapshot, output)
    text = snapshot.sources[0].content
    for item in prepared["citations"]:
        assert text[item["start"]:item["end"]] == item["quote"]


def test_ambiguous_quote_anchors_to_first_occurrence_at_the_model_seam() -> None:
    """SUB-CAUSA 1 fix (12-jul, holdout): existencia de evidencia es el
    guardarrail del anclaje, no unicidad de offset. Una quote repetida en la
    fuente ancla a su primera ocurrencia en vez de rechazar."""
    text = "Concurso Publico 01 ... Concurso Publico 02"
    snapshot = _snapshot(text)
    output = {
        "decision": "indice_oficial",
        "citations": [{
            "dimension": "bucket",
            "quote": "Concurso Publico",
            "source_field": "main_content",
            "source_id": "main",
        }],
    }
    prepared = certifier._prepare_certifier_output(snapshot, output)
    item = prepared["citations"][0]
    assert item["start"] == text.index("Concurso Publico")
    assert text[item["start"]:item["end"]] == "Concurso Publico"


def _combined_output(*, bucket_quotes: tuple[str, ...], decision: str = "indice_oficial_combinado"):
    text = _snapshot().sources[0].content
    citations = []
    for dimension, quote in (
        ("authority", "ASCII"),
        ("identity", "ASCII"),
        ("page_role", "ação"),
        ("stability", "fim"),
    ):
        start = text.index(quote)
        citations.append({
            "dimension": dimension,
            "quote": quote,
            "source_field": "main_content",
            "source_id": "main",
            "start": start,
            "end": start + len(quote),
        })
    for quote in bucket_quotes:
        start = text.index(quote)
        citations.append({
            "dimension": "bucket",
            "quote": quote,
            "source_field": "main_content",
            "source_id": "main",
            "start": start,
            "end": start + len(quote),
        })
    return {"decision": decision, "citations": citations}


def test_combined_decision_rejects_single_bucket_citation() -> None:
    """Hueco FP real: 'indice_oficial_combinado' con UNA sola cita bucket solo
    prueba un tipo, no la combinacion de ambos. Cardinalidad, no semantica."""
    with pytest.raises(
        AgentOutputRejected, match="combined_requires_two_distinct_bucket_citations"
    ):
        certifier._certifier_invariants(_combined_output(bucket_quotes=("😀",)))


def test_combined_decision_rejects_two_identical_bucket_citations() -> None:
    """Dos citas bucket con el MISMO quote no prueban dos tipos distintos."""
    with pytest.raises(
        AgentOutputRejected, match="combined_requires_two_distinct_bucket_citations"
    ):
        certifier._certifier_invariants(
            _combined_output(bucket_quotes=("😀", "😀"))
        )


def test_combined_decision_accepts_two_distinct_bucket_citations() -> None:
    """Dos citas bucket textualmente distintas satisfacen el requisito."""
    certifier._certifier_invariants(
        _combined_output(bucket_quotes=("😀", "ASCII"))
    )  # no debe levantar


def test_non_combined_decisions_unaffected_by_double_bucket_requirement() -> None:
    """El requisito de doble cita bucket es exclusivo de 'combinado': una
    decision afirmativa normal sigue aceptando una sola cita bucket."""
    certifier._certifier_invariants(
        _affirmative_output(authority_citation=True)
    )  # decision="indice_oficial" (1 sola cita bucket) no debe levantar


def test_prosecutor_proved_citations_hydrate_from_quote_only() -> None:
    snapshot = _snapshot()
    output = _prosecutor_output(result="block")
    output["accusations"][0]["outcome"] = "proved"
    output["accusations"][0]["citations"] = [
        {"source_id": "main", "quote": "ação"}
    ]
    prepared = prosecutor._prepare_prosecutor_output(snapshot, output)
    hydrated = prepared["accusations"][0]["citations"][0]
    text = snapshot.sources[0].content
    assert text[hydrated["start"]:hydrated["end"]] == "ação"


def test_sustain_with_unanchorable_discarded_citation_is_accepted_and_dropped() -> None:
    """Fallo real Aratiba/CP: un sustain correcto (0 proved/15 discarded) no
    puede caer por una cita ILUSTRATIVA de una acusacion discarded que no
    ancla (chrome repetido u otro texto ausente/ambiguo). La skill las marca
    'recomendadas', no obligatorias (SKILL.md:54,78): se descarta SOLO esa
    cita puntual y se registra para observabilidad; el sustain se acepta."""
    snapshot = _snapshot()
    output = _prosecutor_output(result="sustain")
    discarded_code = output["accusations"][0]["code"]
    output["accusations"][0]["citations"] = [
        {"source_id": "main", "quote": "no-existe-en-snapshot"}
    ]
    prepared = prosecutor._prepare_prosecutor_output(snapshot, output)
    assert prepared["result"] == "sustain"
    assert prepared["accusations"][0]["citations"] == []
    dropped = prepared["dropped_optional_citations"]
    assert len(dropped) == 1
    assert dropped[0]["location"] == discarded_code
    assert dropped[0]["reason"] == "quote_not_found"


def test_block_with_unanchorable_proved_citation_still_hard_rejects() -> None:
    """El resultado 'block'/proved NUNCA se relaja: una cita de una acusacion
    proved que no ancla sigue siendo fallo duro bajo la nueva politica
    selectiva (solo discarded/unresolved son opcionales)."""
    snapshot = _snapshot()
    output = _prosecutor_output(result="block")
    output["accusations"][0]["outcome"] = "proved"
    output["accusations"][0]["citations"] = [
        {"source_id": "main", "quote": "no-existe-en-snapshot"}
    ]
    with pytest.raises(CitationVerificationError, match="quote_not_found"):
        prosecutor._prepare_prosecutor_output(snapshot, output)


def test_direct_B_schema_exposes_exact_closed_accusation_contract() -> None:
    properties = PROSECUTOR_OUTPUT_SCHEMA["properties"]
    assert properties["result"]["enum"] == ["sustain", "block", "review"]
    assert properties["reason"]["maxLength"] == 400
    assert {"confidence", "insufficiency"} <= set(
        PROSECUTOR_OUTPUT_SCHEMA["required"]
    )
    accusations = properties["accusations"]
    assert accusations["minItems"] == accusations["maxItems"] == 15
    assert set(accusations["items"]["properties"]["code"]["enum"]) == set(
        ACCUSATION_CODES
    )
    assert properties["tool_request"] == {"type": "null"}


def test_B_global_result_is_derived_fail_closed() -> None:
    with pytest.raises(AgentOutputRejected, match="review"):
        prosecutor._prosecutor_invariants(
            _prosecutor_output(result="review")
        )
    with pytest.raises(AgentOutputRejected, match="sustain"):
        prosecutor._prosecutor_invariants(
            _prosecutor_output(result="sustain", unresolved=ACCUSATION_CODES[0])
        )


def test_proved_accusation_without_citations_is_rejected() -> None:
    """Una acusacion proved SIN citas jamas puede sustentar un block: el
    invariante rechaza antes de que la propuesta llegue al orquestador."""
    output = _prosecutor_output(result="block")
    output["accusations"][0]["outcome"] = "proved"
    assert not output["accusations"][0]["citations"]
    with pytest.raises(
        AgentOutputRejected, match="proved_accusation_without_citations"
    ):
        prosecutor._prosecutor_invariants(output)


def test_C_reason_is_bounded() -> None:
    assert JUDGE_OUTPUT_SCHEMA["properties"]["reason"]["maxLength"] == 400


def test_prepare_collects_all_anchor_failures_for_repair() -> None:
    """El anclaje reporta TODOS los fallos de una vez (no solo el primero):
    la ronda de reparacion necesita la lista completa para corregir en un
    solo intento (canario r4: NH/Canoas arreglaron solo el primer fallo).
    'dup A' (ambigua, 2 ocurrencias) ya NO es un fallo (SUB-CAUSA 1 fix,
    12-jul): se ancla a la primera ocurrencia y se mezcla junto a dos fallos
    reales (quote_not_found + missing_required_fields) para probar que la
    coleccion de fallos sigue siendo completa."""
    snapshot = _snapshot("dup A dup A unico B fim")
    output = {
        "decision": "indice_oficial",
        "citations": [
            {"dimension": "identity", "quote": "dup A",
             "source_field": "main_content", "source_id": "main"},
            {"dimension": "bucket", "quote": "no-existe-en-snapshot",
             "source_field": "main_content", "source_id": "main"},
            {"dimension": "authority",
             "source_field": "main_content", "source_id": "main"},
            {"dimension": "stability", "quote": "unico B",
             "source_field": "main_content", "source_id": "main"},
        ],
    }
    with pytest.raises(CitationVerificationError) as raised:
        certifier._prepare_certifier_output(snapshot, output)
    failures = getattr(raised.value, "failures", ())
    assert len(failures) == 2
    reasons = sorted(getattr(f, "reason", "") for f in failures)
    assert reasons == ["missing_required_fields", "quote_not_found"]


def test_api_facing_schema_strips_unsupported_array_bounds() -> None:
    """Gemini rechaza minItems/maxItems en response_json_schema (400 en vivo,
    canario r4). El schema API-facing los pierde; la validacion LOCAL y los
    invariantes Python siguen exigiendo exactamente 15 acusaciones."""
    from scripts.fase2_municipios.v2.agents.base import sanitized_response_schema
    from scripts.fase2_municipios.v2.agents.schemas import PROSECUTOR_OUTPUT_SCHEMA

    sanitized = sanitized_response_schema(PROSECUTOR_OUTPUT_SCHEMA)
    accusations = sanitized["properties"]["accusations"]
    assert "minItems" not in accusations
    assert "maxItems" not in accusations
    # el schema LOCAL conserva la cardinalidad exacta
    assert PROSECUTOR_OUTPUT_SCHEMA["properties"]["accusations"]["minItems"] == 15
    assert PROSECUTOR_OUTPUT_SCHEMA["properties"]["accusations"]["maxItems"] == 15
