"""R-T1 iteracion 2: bucket confirmation requires >=1 ITEM-POSITIVE citation.

FP real (Canela/RS, holdout 12-jul, run r2_postpalancas): "concurso_publico"
confirmo "indice_oficial" (confianza high) sobre
https://servicosonline.canela.rs.gov.br:8383/sys523/publico/concursosPublicos.xhtml
-- un portal PERPETUAMENTE VACIO (verificado a mano en navegador: el
municipio publica en otro lado). La regla anti-ausencia (test_absence_
evidence_gate.py, implementada el dia anterior) NO lo atrapo porque esta vez
la cita de bucket fue la ETIQUETA DEL FILTRO -- "Concurso ou Processo
Seletivo" -- que no es un mensaje de ausencia (no dispara el blocklist) pero
tampoco prueba que exista un solo item real. Citas verbatim del stage A real:
staging/fase2_v2/eval/holdout50_20260712/run_r2_postpalancas/observability/
canela--concurso-publico--1f6ba659076b--attempt-001.json (y su hermano
processo-seletivo, decision=nao_encontrado, usado como control de
no-regresion).

Este archivo ejercita certifier._certifier_invariants()/_is_item_positive_
quote() directamente (sin llamar al modelo) y el enganche de reparacion en
agents/base.py (_run_direct), igual que los demas contratos P0.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.fase2_municipios.v2.agents import base, certifier
from scripts.fase2_municipios.v2.agents.base import AgentOutputRejected
from scripts.fase2_municipios.v2.snapshot import anchor_citation, build_snapshot
from scripts.fase2_municipios.v2.snapshot import EvidenceSource
from datetime import datetime, timezone


pytestmark = pytest.mark.offline


# Citations verbatim del stage A real (canela/concurso_publico, run
# r2_postpalancas) -- ver docstring del modulo para la ruta del artefacto.
_CANELA_CONCURSO_PUBLICO_A_RAW: dict[str, Any] = {
    "authority": "confirmada",
    "bucket": "concurso_publico",
    "candidate_id": "v1:20b19a786b6f77d0b25ad7f6636cadbcc2305509",
    "citations": [
        {
            "dimension": "authority",
            "end": 120,
            "quote": "Portal da Transparência | Município de Canela",
            "source_field": "heading",
            "source_id": "main",
            "start": 75,
        },
        {
            "dimension": "identity",
            "end": 3877,
            "quote": "PREFEITURA MUNICIPAL DE CANELA",
            "source_field": "main_content",
            "source_id": "main",
            "start": 3847,
        },
        {
            "dimension": "page_role",
            "end": 2202,
            "quote": "Edital de Concursos e Seleções Públicas",
            "source_field": "heading",
            "source_id": "main",
            "start": 2163,
        },
        {
            "dimension": "bucket",
            "end": 4057,
            "quote": "Concurso ou Processo Seletivo",
            "source_field": "main_content",
            "source_id": "main",
            "start": 4028,
        },
        {
            "dimension": "stability",
            "end": 3918,
            "quote": (
                "Filtros Entidade Todas PREFEITURA MUNICIPAL DE CANELA "
                "Câmara Municipal de Vereadores de Canela"
            ),
            "source_field": "main_content",
            "source_id": "main",
            "start": 3824,
        },
    ],
    "confidence": "high",
    "decision": "indice_oficial",
    "evidence_state": "completa",
    "identity": "confirmada",
    "insufficiency": "none",
    "page_role": "indice_listado",
    "reason": (
        "A pagina e o portal oficial de transparencia do Municipio de "
        "Canela, contendo estrutura de indice (filtros, selecao de "
        "entidade e tipo de publicacao) para concursos e processos "
        "seletivos. Embora nao haja resultados no momento, a estrutura "
        "e estavel e oficial, caracterizando um indice valido."
    ),
    "source_kind": "dominio_oficial_prefeitura",
}

# Hermano real: processo_seletivo sobre la misma pagina, decision=
# nao_encontrado (NO afirmativa) -- control de no-regresion: el invariante
# debe seguir sin levantar para decisiones no afirmativas.
_CANELA_PROCESSO_SELETIVO_A_RAW: dict[str, Any] = {
    **_CANELA_CONCURSO_PUBLICO_A_RAW,
    "bucket": "processo_seletivo",
    "decision": "nao_encontrado",
}


# --- (1) PRUEBA Canela: las DOS unidades reales por el invariante nuevo ---


def test_canela_concurso_publico_real_raw_rejects_label_only_bucket_citation() -> None:
    """concurso_publico DEBE rechazar: la unica cita de bucket es la ETIQUETA
    del filtro ('Concurso ou Processo Seletivo'), sin nº/ano/fecha -- no es
    evidencia de que exista un item real."""
    with pytest.raises(AgentOutputRejected, match="indice_sin_evidencia_de_items"):
        certifier._certifier_invariants(_CANELA_CONCURSO_PUBLICO_A_RAW)


def test_canela_processo_seletivo_real_raw_is_unaffected_not_affirmative() -> None:
    """processo_seletivo (decision=nao_encontrado) no es una decision
    afirmativa: el invariante no debe tocarla (no-regresion)."""
    certifier._certifier_invariants(_CANELA_PROCESSO_SELETIVO_A_RAW)  # no debe levantar


# --- _is_item_positive_quote: casos positivos y negativos -------------------


@pytest.mark.parametrize(
    "quote",
    [
        "Concurso Público 001/2026 - Edital de Abertura",
        "Processo Seletivo Simplificado 003/2025",
        "Edital nº 12/2026",
        "Edital n° 12/2026",
        "Edital no. 12/2026",
        "Concurso Público num. 7/2026",
        "Resultado Final do Concurso 01/2026",
        "Processo de Seleção 004/2026",
        "EDITAL 001/2026",
        "concurso publico 5/26",  # sin acentos, ano de 2 digitos
    ],
)
def test_item_positive_quotes_are_recognized(quote: str) -> None:
    assert certifier._is_item_positive_quote(quote) is True


@pytest.mark.parametrize(
    "quote",
    [
        "Concurso ou Processo Seletivo",  # etiqueta de filtro real (Canela)
        "Edital de Concursos e Seleções Públicas",  # page_role, sin numero
        "Concursos Públicos",  # keyword plural, sin marcador de instancia
        "Filtrar",
        "Buscar",
        "",
        "Portal da Transparência | Município de Canela",  # sin keyword
        "001/2026",  # marcador sin keyword de bucket
        "Recursos Humanos",
        # Un año aislado identifica una sección anual, no un ítem publicado.
        "Seleção Pública 2026",
        "PROCESSOS SELETIVOS 2026",
    ],
)
def test_non_item_positive_quotes_are_rejected(quote: str) -> None:
    assert certifier._is_item_positive_quote(quote) is False


def test_absence_message_is_never_item_positive() -> None:
    """Un mensaje de ausencia no debe colar como item-positivo aunque
    contenga la palabra 'concursos' (doble capa: blocklist Y gate nuevo)."""
    quote = "Não foram encontrados Concursos / Processos Seletivos com os filtros selecionados."
    assert certifier._is_absence_message(quote) is True
    assert certifier._is_item_positive_quote(quote) is False


# --- _certifier_invariants: casos sinteticos de aislamiento ------------------


def _base_citations() -> list[dict]:
    return [dict(item) for item in _CANELA_CONCURSO_PUBLICO_A_RAW["citations"]
            if item["dimension"] != "bucket"]


def _bucket_citation(quote: str) -> dict:
    return {
        "dimension": "bucket", "quote": quote,
        "source_field": "main_content", "source_id": "main",
    }


def test_label_only_bucket_citation_is_rejected_even_without_absence_wording() -> None:
    """Reproduce el hueco exacto: una etiqueta de categoria (no un mensaje de
    ausencia) no debe alcanzar para confirmar."""
    output = {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "citations": _base_citations() + [_bucket_citation("Concurso ou Processo Seletivo")],
    }
    with pytest.raises(AgentOutputRejected, match="indice_sin_evidencia_de_items"):
        certifier._certifier_invariants(output)


def test_item_positive_bucket_citation_alongside_label_is_accepted() -> None:
    """Si ADEMAS de la etiqueta hay una cita real de item, la decision se
    mantiene (>=1 item-positiva alcanza, igual que el gate de ausencia)."""
    output = {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "citations": _base_citations() + [
            _bucket_citation("Concurso ou Processo Seletivo"),
            _bucket_citation("Concurso Público 001/2026 - Edital de Abertura"),
        ],
    }
    certifier._certifier_invariants(output)  # no debe levantar


def test_combined_with_one_label_and_one_real_item_is_degraded() -> None:
    """'indice_oficial_combinado' con una cita item-positiva y otra que es
    solo etiqueta: solo UN tipo tiene evidencia real de item."""
    output = {
        "decision": "indice_oficial_combinado",
        "bucket": "combinado",
        "citations": _base_citations() + [
            _bucket_citation("Concurso Público 001/2026 - Edital de Abertura"),
            _bucket_citation("Concurso ou Processo Seletivo"),
        ],
    }
    with pytest.raises(
        AgentOutputRejected,
        match="indice_sin_evidencia_de_items:combinado_solo_un_tipo_con_items",
    ):
        certifier._certifier_invariants(output)


def test_combined_with_two_item_positive_citations_is_not_degraded() -> None:
    output = {
        "decision": "indice_oficial_combinado",
        "bucket": "combinado",
        "citations": _base_citations() + [
            _bucket_citation("Concurso Público 001/2026 - Edital de Abertura"),
            _bucket_citation("Processo Seletivo 002/2026 - Edital de Abertura"),
        ],
    }
    certifier._certifier_invariants(output)  # no debe levantar


# --- Enganche de reparacion (agents/base.py _run_direct) --------------------


def test_certifier_repairable_reason_covers_citation_and_item_evidence_families() -> None:
    assert certifier._certifier_repairable_reason("citation_verification_failed:1") is True
    assert certifier._certifier_repairable_reason("indice_sin_evidencia_de_items") is True
    assert certifier._certifier_repairable_reason(
        "indice_sin_evidencia_de_items:combinado_solo_un_tipo_con_items"
    ) is True
    # Reasons fuera de estas dos familias NO se reparan (p.ej. dimensiones
    # faltantes, o el gate de ausencia -- ambos siguen fail-closed directo).
    assert certifier._certifier_repairable_reason("missing_confirmation_citation_dimensions:authority") is False
    assert certifier._certifier_repairable_reason("indice_vacio_sin_items") is False
    assert certifier._certifier_repairable_reason("affirmative_result_without_citations") is False


def test_certifier_repair_instruction_dispatches_item_evidence_message() -> None:
    exc = AgentOutputRejected(role="certifier", reason="indice_sin_evidencia_de_items")
    text = certifier._certifier_repair_instruction(exc)
    assert "ITEM_EVIDENCE_REPAIR" in text
    assert "ETIQUETA" in text
    assert "revisar" in text


def test_certifier_repair_instruction_falls_back_to_citation_instruction() -> None:
    from scripts.fase2_municipios.v2.snapshot import CitationVerificationError

    cause = CitationVerificationError(source_id="main", reason="quote_not_found", quote="x")
    exc = RuntimeError("outer")
    exc.__cause__ = cause
    text = certifier._certifier_repair_instruction(exc)
    assert "CITATION_REPAIR" in text
    assert "ITEM_EVIDENCE_REPAIR" not in text


CERTIFIER_LIKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "bucket", "citations"],
    "properties": {
        "decision": {"type": "string"},
        "bucket": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dimension", "source_id", "quote"],
                "properties": {
                    "dimension": {"type": "string"},
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
        self.calls: list[Any] = []

    def generate_structured(self, contents: Any, *, estimated_tokens: int) -> Any:
        self.calls.append(contents)
        return self.responses.pop(0)


def _citations(output: Any) -> tuple:
    from scripts.fase2_municipios.v2.snapshot import Citation

    return tuple(
        Citation(source_id=item["source_id"], start=item["start"], end=item["end"], quote=item["quote"])
        for item in output["citations"]
    )


def _hydrating_prepare(snapshot: Any, output: Any) -> Any:
    import copy

    prepared = copy.deepcopy(dict(output))
    for item in prepared["citations"]:
        item.pop("start", None)
        item.pop("end", None)
        citation = anchor_citation(snapshot, item, require_offsets=False)
        item["start"] = citation.start
        item["end"] = citation.end
    return prepared


def _item_evidence_runner(client: Any) -> base.AgentRunner:
    return base.AgentRunner(
        role="certifier",
        system_prompt="Return the structured decision.",
        client=client,
        output_schema=CERTIFIER_LIKE_SCHEMA,
        extract_citations=_citations,
        prepare_output=_hydrating_prepare,
        requires_citations=lambda output: output["decision"] in certifier.AFFIRMATIVE_CERTIFIER_DECISIONS,
        output_invariant=certifier._certifier_invariants,
        tools=None,
        repairable_reason=certifier._certifier_repairable_reason,
        repair_instruction=certifier._certifier_repair_instruction,
    )


def _label_and_item_snapshot():
    """Pagina donde SI hay un item real ademas de la etiqueta del filtro --
    escenario recuperable: A cito perezoso la etiqueta, pero el item real
    esta en el snapshot y una re-cita deberia encontrarlo."""
    return build_snapshot((EvidenceSource(
        source_id="main",
        url="https://example.invalid/concursos",
        retrieved_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        content=(
            "Portal da Transparência | Município de Exemplo\n"
            "Edital de Concursos e Seleções Públicas\n"
            "Concurso ou Processo Seletivo\n"
            "Concurso Público 001/2026 - Edital de Abertura\n"
            "PREFEITURA MUNICIPAL DE EXEMPLO"
        ),
    ),))


def _label_only_snapshot():
    """Pagina PERPETUAMENTE VACIA (como Canela real): solo la etiqueta del
    filtro, ningun item real en ninguna parte del snapshot -- escenario NO
    recuperable, la reparacion debe fallar igual que el original."""
    return build_snapshot((EvidenceSource(
        source_id="main",
        url="https://example.invalid/concursos",
        retrieved_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        content=(
            "Portal da Transparência | Município de Exemplo\n"
            "Edital de Concursos e Seleções Públicas\n"
            "Concurso ou Processo Seletivo\n"
            "PREFEITURA MUNICIPAL DE EXEMPLO"
        ),
    ),))


def _output_with_bucket_quote(quote: str) -> dict[str, Any]:
    return {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "citations": [
            {"dimension": "authority", "source_id": "main", "quote": "Portal da Transparência | Município de Exemplo"},
            {"dimension": "identity", "source_id": "main", "quote": "PREFEITURA MUNICIPAL DE EXEMPLO"},
            {"dimension": "page_role", "source_id": "main", "quote": "Edital de Concursos e Seleções Públicas"},
            {"dimension": "bucket", "source_id": "main", "quote": quote},
            {"dimension": "stability", "source_id": "main", "quote": "PREFEITURA MUNICIPAL DE EXEMPLO"},
        ],
    }


def test_lazy_label_citation_recovers_on_repair_when_real_item_exists() -> None:
    """Escenario 'ideal' pedido en la mision: A cito perezosamente la
    ETIQUETA teniendo un item real en la pagina -- el reintento de
    reparacion debe encontrarlo y confirmar."""
    lazy = _output_with_bucket_quote("Concurso ou Processo Seletivo")
    repaired = _output_with_bucket_quote("Concurso Público 001/2026 - Edital de Abertura")
    client = SequencedClient([lazy, repaired])

    result = _item_evidence_runner(client).run(
        snapshot=_label_and_item_snapshot(), task="certify fixture"
    )

    assert isinstance(result, base.AgentRunResult)
    assert len(client.calls) == 2
    # El segundo prompt debe llevar la instruccion de reparacion de item-evidencia.
    second_prompt = str(client.calls[1])
    assert "ITEM_EVIDENCE_REPAIR" in second_prompt
    bucket_citation = next(
        c for c in result.output["citations"] if c["dimension"] == "bucket"
    )
    assert bucket_citation["quote"] == "Concurso Público 001/2026 - Edital de Abertura"


def test_label_citation_stays_rejected_when_page_has_no_real_item() -> None:
    """Escenario Canela real: ni siquiera con el reintento hay un item real
    que citar -- la pagina esta vacia. Fail-closed tras UN reintento, igual
    que el techo de reparacion existente."""
    lazy = _output_with_bucket_quote("Concurso ou Processo Seletivo")
    still_lazy = _output_with_bucket_quote("Concurso ou Processo Seletivo")
    client = SequencedClient([lazy, dict(still_lazy)])

    result = _item_evidence_runner(client).run(
        snapshot=_label_only_snapshot(), task="certify fixture"
    )

    assert isinstance(result, base.SnapshotInvalidOutput)
    assert len(client.calls) == 2


def test_default_runner_never_repairs_item_evidence_reason_without_opt_in() -> None:
    """Contrato de base.py: sin pasar repairable_reason/repair_instruction
    (comportamiento por defecto, el que usa cualquier otro rol), un rechazo
    de invariante NO relacionado con citas nunca se repara -- no-regresion
    del contrato existente (ver test_non_citation_rejection_never_triggers_repair)."""
    lazy = _output_with_bucket_quote("Concurso ou Processo Seletivo")
    client = SequencedClient([lazy])

    default_runner = base.AgentRunner(
        role="certifier",
        system_prompt="Return the structured decision.",
        client=client,
        output_schema=CERTIFIER_LIKE_SCHEMA,
        extract_citations=_citations,
        prepare_output=_hydrating_prepare,
        requires_citations=lambda output: output["decision"] in certifier.AFFIRMATIVE_CERTIFIER_DECISIONS,
        output_invariant=certifier._certifier_invariants,
        tools=None,
        # repairable_reason/repair_instruction omitidos a proposito.
    )

    result = default_runner.run(snapshot=_label_and_item_snapshot(), task="certify fixture")

    assert isinstance(result, base.SnapshotInvalidOutput)
    assert len(client.calls) == 1  # sin reintento: la pagina SI tenia item real, pero no se le dio la oportunidad
