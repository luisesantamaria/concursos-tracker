"""Canonical resource certifier role over the generic bounded agent loop."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.fase2_municipios.v2.agents.base import (
    AgentOutputRejected,
    AgentRunner,
    sanitized_response_schema,
    skill_markdown_body,
)
from scripts.fase2_municipios.v2.agents.schemas import AGENT_STEP_SCHEMA
from scripts.fase2_municipios.v2.agents.tools import ToolLimits
from scripts.fase2_municipios.v2.gemini import (
    RoleModels,
    build_gemini_client,
    Transport,
)
from scripts.fase2_municipios.v2.loader import load_canonical_resources
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    CitationVerificationError,
    EvidenceSnapshot,
    anchor_citation,
)


AFFIRMATIVE_CERTIFIER_DECISIONS = frozenset({
    "indice_oficial",
    "indice_oficial_combinado",
    "portal_externo_oficial",
})
REQUIRED_CONFIRMATION_CITATION_DIMENSIONS = frozenset({
    "authority", "identity", "page_role", "bucket", "stability",
})

# R-T1 (FP real Canela, holdout 12-jul): un modulo de transparencia con
# filtros de entidad/tipo/ano funcionales pero CERO concursos en la historia
# fue confirmado ("indice_oficial") usando como UNICA cita de bucket el
# propio mensaje de ausencia ("Nao foram encontrados Concursos / Processos
# Seletivos com os filtros selecionados."). Blocklist content-neutral (misma
# lista para cualquier municipio/plataforma, no un hardcode municipal):
# frases pt-BR genericas de "sem resultados", verificadas contra los
# artefactos del holdout 12-jul y variantes razonables del mismo patron.
# \s+ tolera saltos de linea/espacios multiples del texto ya renderizado.
_ABSENCE_MESSAGE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"nao\s+foram\s+encontrados",
        r"nenhum\s+registro",
        r"nenhum\s+resultado",
        r"nao\s+ha\s+registros",
        r"nenhum\s+item\s+encontrado",
        r"nao\s+existem\s+registros",
        r"sem\s+resultados",
        r"nada\s+encontrado",
        r"nenhum\s+concurso\s+encontrado",
        r"nenhuma\s+publicacao\s+encontrada",
        r"sem\s+registros",
        r"nenhum\s+dado\s+encontrado",
    )
)

# R-T1 iteracion 2 (FP real Canela/concurso_publico, holdout 12-jul, run
# r2_postpalancas): la unica cita de bucket fue la ETIQUETA del filtro
# ('Concurso ou Processo Seletivo') -- no matchea el blocklist de ausencia de
# arriba (no es un mensaje de "nao encontrado"), pero tampoco prueba que
# exista un solo item real: es el nombre de la categoria del formulario, no
# evidencia de contenido. El gate se endurece de "no-ausencia" a
# "ITEM-POSITIVO": una cita de bucket confirmatoria debe contener una
# keyword de bucket content-neutral (edital/concurso/processo seletivo o
# simplificado/selecao) Y ADEMAS un marcador de instancia (numero de
# edital, par numero/ano, fecha completa, o un ano de 4 digitos pegado a la
# keyword). Filosofia identica a ITEM_MARKER_PATTERN en
# eval/platform_probe_runner.py (ya validado ahi: exige keyword + numero/ano
# adyacente para que una pagina cuente como indice estructural), replicada
# aqui de forma independiente y self-contained para una cita individual
# (ver docstring de ese modulo sobre no importar de/hacia cascade
# intocable).
# Formas singular Y plural del mismo keyword content-neutral (fix tras medir
# impacto en las 56 confirmadas de r2: "PROCESSOS SELETIVOS 2026"/"Processos
# Seletivos 2025" -- ambas palabras en plural -- no matcheaban la forma
# singular-only original y se rechazaban de mas). Plural es la MISMA
# categoria semantica, no un ablandamiento del criterio.
_ITEM_KEYWORD_PATTERN = re.compile(
    r"\b(?:editais|edital|concursos?|processos?\s+seletivos?"
    r"|processos?\s+simplificados?|selecao|selecoes)\b"
)
_ITEM_INSTANCE_MARKER_PATTERN = re.compile(
    r"(?:"
    r"\bn[o°]\.?\s*\d+"        # nº 001 / n° 12 / no. 5 (nº folds to "no")
    r"|\bnum\.?\s*\d+"               # num. 001 / núm 5
    r"|\d{1,4}\s*/\s*\d{2,4}"        # 001/2026 (numero/ano)
    r"|\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}"  # dd/mm/yyyy
    r")"
)
_ITEM_KEYWORD_YEAR_ADJACENT_PATTERN = re.compile(
    r"\b(?:editais|edital|concursos?|processos?\s+seletivos?"
    r"|processos?\s+simplificados?|selecao|selecoes)\b"
    r"[^\d\n]{0,40}\b20\d{2}\b"
)


def _fold_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _is_absence_message(quote: str) -> bool:
    """True when a citation's literal text is itself a pt-BR "no results" message.

    A citation whose quote matches (or is contained in) an absence message
    cannot serve as evidence that a bucket actually has items -- it proves
    the opposite. Matching is case- and accent-insensitive and does not
    depend on municipio/platform (content-neutral).
    """
    if not isinstance(quote, str) or not quote:
        return False
    folded = _fold_accents(quote).lower()
    return any(pattern.search(folded) for pattern in _ABSENCE_MESSAGE_PATTERNS)


def _is_item_positive_quote(quote: str) -> bool:
    """True when a bucket citation is content-neutral POSITIVE evidence of
    an item (not just a non-empty, non-absence quote).

    Requires a bucket keyword (edital/concurso/processo seletivo or
    simplificado/selecao) AND an instance marker: a numbered reference
    (nº/n°/no./num. + digits), a numero/ano pair (001/2026), a full date
    (dd/mm/yyyy), or a 4-digit year adjacent to the keyword. A quote that is
    merely a filter/category LABEL (e.g. "Concurso ou Processo Seletivo")
    has the keyword but no instance marker and fails this check -- it names
    a category, it does not point at a published item. Matching is case-
    and accent-insensitive and content-neutral (no municipio/platform
    hardcoding), mirroring ITEM_MARKER_PATTERN in
    eval/platform_probe_runner.py.
    """
    if not isinstance(quote, str) or not quote:
        return False
    folded = _fold_accents(quote).lower()
    if not _ITEM_KEYWORD_PATTERN.search(folded):
        return False
    if _ITEM_INSTANCE_MARKER_PATTERN.search(folded):
        return True
    return bool(_ITEM_KEYWORD_YEAR_ADJACENT_PATTERN.search(folded))


def _certifier_citations(output: Mapping[str, Any]) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            source_id=item["source_id"],
            start=item["start"],
            end=item["end"],
            quote=item["quote"],
        )
        for item in output["citations"]
    )


def _prepare_certifier_output(
    snapshot: EvidenceSnapshot, output: Mapping[str, Any]
) -> Mapping[str, Any]:
    prepared = copy.deepcopy(dict(output))
    failures: list[CitationVerificationError] = []
    for item in prepared["citations"]:
        # Politica 12-jul (aprobada por Luis): el modelo entrega source_id+quote
        # y el codigo computa los offsets exigiendo ocurrencia literal (solo
        # quote_not_found rechaza; una quote repetida ancla a su primera
        # ocurrencia -- existencia de evidencia, no unicidad de offset, ver
        # snapshot.anchor_citation). Los offsets que el modelo emita se
        # DESCARTAN: son ruido demostrado (canario r1: 20/20 sin end; r2:
        # longitudes erroneas), nunca evidencia. El gate final re-verifica
        # slice-equality sobre los offsets computados (require_offsets=True en
        # orchestration._strict_citations), sin cambios.
        item.pop("start", None)
        item.pop("end", None)
        try:
            citation = anchor_citation(snapshot, item, require_offsets=False)
        except CitationVerificationError as exc:
            # Recolectar TODOS los fallos (no solo el primero): la ronda de
            # reparacion necesita la lista completa para corregir de una vez.
            failures.append(exc)
            continue
        item["start"] = citation.start
        item["end"] = citation.end
    if failures:
        first = failures[0]
        first.failures = tuple(failures)
        raise first
    return prepared


def _certifier_invariants(output: Mapping[str, Any]) -> None:
    decision = output.get("decision")
    if decision not in AFFIRMATIVE_CERTIFIER_DECISIONS:
        return
    citations = [
        item for item in output.get("citations", ())
        if isinstance(item, Mapping)
    ]
    dimensions = {item.get("dimension") for item in citations}
    missing = REQUIRED_CONFIRMATION_CITATION_DIMENSIONS - dimensions
    if missing:
        raise AgentOutputRejected(
            role="certifier",
            reason="missing_confirmation_citation_dimensions:" + ",".join(sorted(missing)),
        )
    bucket_quotes_all = [
        item.get("quote") for item in citations
        if item.get("dimension") == "bucket"
    ]
    non_absence_bucket_quotes = {
        quote for quote in bucket_quotes_all
        if isinstance(quote, str) and not _is_absence_message(quote)
    }
    if not non_absence_bucket_quotes:
        # R-T1: un bucket confirmado exige >=1 cita de bucket VERIFICADA cuyo
        # texto NO sea un mensaje de ausencia (ver _is_absence_message). Un
        # indice estructuralmente valido (filtros, busqueda) pero vacio en
        # toda su historia no es un indice utilizable: degradar a revisar en
        # vez de confirmar sobre "nao encontramos nada".
        raise AgentOutputRejected(
            role="certifier",
            reason="indice_vacio_sin_items",
        )
    item_positive_bucket_quotes = {
        quote for quote in non_absence_bucket_quotes
        if _is_item_positive_quote(quote)
    }
    if not item_positive_bucket_quotes:
        # R-T1 iteracion 2 (FP real Canela/concurso_publico, holdout 12-jul,
        # run r2_postpalancas): "no-ausencia" no basta. La cita de bucket
        # puede ser una etiqueta de filtro/categoria (no dispara el
        # blocklist de ausencia porque no dice "nao encontrado") sin probar
        # que exista un solo item real. Endurecido a ITEM-POSITIVO: ver
        # _is_item_positive_quote.
        raise AgentOutputRejected(
            role="certifier",
            reason="indice_sin_evidencia_de_items",
        )
    if decision == "indice_oficial_combinado":
        # Politica 12-jul (hueco FP real): una superficie combinada exige DOS
        # evidencias de bucket textualmente distintas -- una cita bucket sola
        # solo prueba un tipo, no la combinacion. Cardinalidad pura: el codigo
        # cuenta quotes distintos, no decide cuales tipos son (eso es semantica
        # de A/B/C, fuera del alcance del codigo).
        bucket_quotes = set(bucket_quotes_all)
        if len(bucket_quotes) < 2:
            raise AgentOutputRejected(
                role="certifier",
                reason="combined_requires_two_distinct_bucket_citations",
            )
        if len(non_absence_bucket_quotes) < 2:
            # R-T1 para combinado: AMBOS tipos necesitan su propia evidencia
            # no-ausencia. Dos quotes distintos donde uno es un mensaje de
            # ausencia solo prueba UN tipo, no la combinacion de ambos.
            raise AgentOutputRejected(
                role="certifier",
                reason="indice_vacio_sin_items:combinado_solo_un_tipo_con_items",
            )
        if len(item_positive_bucket_quotes) < 2:
            # R-T1 iteracion 2 para combinado: AMBOS tipos necesitan su
            # propia evidencia ITEM-POSITIVA, no solo "no-ausencia". Dos
            # quotes distintos donde uno es una etiqueta de filtro solo
            # prueba UN tipo con item real.
            raise AgentOutputRejected(
                role="certifier",
                reason="indice_sin_evidencia_de_items:combinado_solo_un_tipo_con_items",
            )


def _certifier_repairable_reason(reason: str) -> bool:
    """Reasons this role gives the model one repair round for.

    Extends the base citation-anchoring repair (``citation_*``, see
    ``AgentRunner._citation_repair_instruction``) to also cover the R-T1
    iteracion 2 item-evidence gate (``indice_sin_evidencia_de_items*``): a
    lazy A that cited a filter LABEL while the page actually shows real
    items should recover on retry instead of losing a good confirmation
    to citation laziness. Reasons outside these two families still fail
    closed immediately (no change for e.g. missing-dimension rejections).
    """
    return reason.startswith("citation_") or reason.startswith(
        "indice_sin_evidencia_de_items"
    )


def _item_evidence_repair_instruction(reason: str) -> str:
    return (
        "ITEM_EVIDENCE_REPAIR (unica oportunidad): tu respuesta fue "
        f"rechazada por el validador determinista (reason={reason}). Una "
        "cita de dimension='bucket' que solo repite la ETIQUETA de un "
        "filtro/menu/categoria (p.ej. 'Concurso ou Processo Seletivo', "
        "'Edital de Concursos e Selecoes Publicas') NO prueba que exista un "
        "item real: nombra una categoria, no apunta a un item publicado. "
        "Reenvia el JSON COMPLETO con el mismo schema: si la pagina SI "
        "muestra un concurso/processo seletivo/edital especifico (con "
        "numero, par numero/ano, fecha o ano), cita ESE texto literal como "
        "evidencia de bucket. Si la pagina NO muestra ningun item real "
        "(solo filtros/categorias vacias), cambia tu decision a 'revisar' "
        "-- no repitas la misma cita de etiqueta. No inventes contenido que "
        "no este en el snapshot."
    )


def _certifier_repair_instruction(exc: BaseException) -> str:
    """Dispatch to the right repair prompt by rejection reason family."""
    reason = getattr(exc, "reason", "") or ""
    if reason.startswith("indice_sin_evidencia_de_items"):
        return _item_evidence_repair_instruction(reason)
    return AgentRunner._citation_repair_instruction(exc)


class CertifierAgent:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    @property
    def system_prompt(self) -> str:
        return self.runner.system_prompt

    def certify(self, *, snapshot: EvidenceSnapshot, task: str):
        return self.runner.run(snapshot=snapshot, task=task)


def build_certifier_agent(
    *,
    transport: Transport,
    limiter,
    repo_root: Path | None = None,
    skills_dir: Path | None = None,
    references_dir: Path | None = None,
    models: RoleModels | None = None,
    max_steps: int = 8,
    max_tool_calls: int = 6,
    estimated_tokens: int = 4_000,
    tool_limits: ToolLimits | None = None,
    invocation_mode: str = "tool_loop",
) -> CertifierAgent:
    if invocation_mode not in {"tool_loop", "direct"}:
        raise ValueError("invalid invocation_mode")
    tools = None if invocation_mode == "direct" else "local_snapshot"
    resources = load_canonical_resources(
        repo_root=repo_root,
        skills_dir=skills_dir,
        references_dir=references_dir,
    )
    role_models = models or RoleModels()
    client = build_gemini_client(
        transport=transport,
        limiter=limiter,
        model=role_models.certifier_model,
        response_schema=sanitized_response_schema(
            resources.references["schema.json"] if tools is None else AGENT_STEP_SCHEMA
        ),
    )
    runner = AgentRunner(
        role="certifier",
        system_prompt=skill_markdown_body(
            resources.skills["fase2-resource-certifier"].content
        ),
        client=client,
        output_schema=resources.references["schema.json"],
        extract_citations=_certifier_citations,
        prepare_output=_prepare_certifier_output,
        requires_citations=lambda output: output.get("decision")
        in AFFIRMATIVE_CERTIFIER_DECISIONS,
        output_invariant=_certifier_invariants,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        estimated_tokens=estimated_tokens,
        tool_limits=tool_limits,
        tools=tools,
        repairable_reason=_certifier_repairable_reason,
        repair_instruction=_certifier_repair_instruction,
    )
    return CertifierAgent(runner)
