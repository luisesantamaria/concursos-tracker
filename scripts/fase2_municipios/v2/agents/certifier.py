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
    )
    return CertifierAgent(runner)
