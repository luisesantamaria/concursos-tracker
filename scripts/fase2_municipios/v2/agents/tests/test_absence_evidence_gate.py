"""R-T1: bucket confirmation requires >=1 non-absence-message citation.

FP real (Canela/RS, holdout 12-jul): "concurso_publico" y "processo_seletivo"
confirmaron "indice_oficial"/"indice_oficial_combinado" sobre
https://servicosonline.canela.rs.gov.br:8383/sys523/publico/concursosPublicos.xhtml
-- un modulo de transparencia con filtros de entidade/tipo/ano funcionales
pero CERO concursos publicados desde 2013. La cita de bucket del certificador
A fue el propio mensaje de ausencia: "Nao foram encontrados Concursos /
Processos Seletivos com os filtros selecionados." Ver el HTML equivalente en
fixtures/indice_vacio_estructural_canela.html.

Estos tests ejercitan certifier._certifier_invariants()/_is_absence_message()
directamente (sin llamar al modelo), igual que los demas contratos P0 en
test_p0_contracts.py.
"""

from __future__ import annotations

import pytest

from scripts.fase2_municipios.v2.agents import certifier
from scripts.fase2_municipios.v2.agents.base import AgentOutputRejected


pytestmark = pytest.mark.offline


# Citations verbatim del stage A real de
# staging/fase2_v2/eval/holdout50_20260712/run_r1/observability/
# canela--concurso-publico--1f6ba659076b--attempt-001.json
_CANELA_REAL_CITATIONS = [
    {
        "dimension": "authority",
        "quote": "Portal da Transparência | Município de Canela",
        "source_field": "heading",
        "source_id": "main",
    },
    {
        "dimension": "identity",
        "quote": (
            "Filtros Entidade Todas PREFEITURA MUNICIPAL DE CANELA "
            "Câmara Municipal de Vereadores de Canela"
        ),
        "source_field": "main_content",
        "source_id": "main",
    },
    {
        "dimension": "page_role",
        "quote": "Página Inicial Recursos Humanos Edital de Concursos e Seleções Públicas",
        "source_field": "main_content",
        "source_id": "main",
    },
    {
        "dimension": "bucket",
        "quote": (
            "Não foram encontrados Concursos / Processos Seletivos "
            "com os filtros selecionados."
        ),
        "source_field": "main_content",
        "source_id": "main",
    },
    {
        "dimension": "stability",
        "quote": (
            "Período de Publicação Inicial/Final Ano Todos 2026 2025 2024 2023 "
            "2022 2021 2020 2019 2018 2017 2016 2015 2014 2013"
        ),
        "source_field": "main_content",
        "source_id": "main",
    },
]


def _base_citations() -> list[dict]:
    """The four non-bucket dimensions Canela's own output supplied, reused
    verbatim so each test only varies the bucket evidence under test."""
    return [dict(item) for item in _CANELA_REAL_CITATIONS if item["dimension"] != "bucket"]


def _bucket_citation(quote: str) -> dict:
    return {
        "dimension": "bucket",
        "quote": quote,
        "source_field": "main_content",
        "source_id": "main",
    }


def test_canela_real_output_is_degraded_to_review() -> None:
    """(a) Caso Canela real: unica cita de bucket = mensaje de ausencia ->
    AgentOutputRejected(reason=indice_vacio_sin_items), que el runner mapea a
    revisar (fail-closed) en vez de confirmar sobre un indice vacio."""
    output = {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "citations": list(_CANELA_REAL_CITATIONS),
    }
    with pytest.raises(AgentOutputRejected, match="indice_vacio_sin_items"):
        certifier._certifier_invariants(output)


def test_real_items_alongside_lateral_absence_text_is_not_degraded() -> None:
    """(b) Pagina con items reales Y ademas texto de ausencia en otra parte
    (p.ej. un contador "0 encontrados" residual de un filtro previo): si HAY
    al menos una cita de bucket real, la decision NO se degrada."""
    output = {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "citations": _base_citations() + [
            _bucket_citation("Concurso Público 001/2026 - Edital de Abertura"),
            _bucket_citation(
                "Não foram encontrados Concursos / Processos Seletivos "
                "com os filtros selecionados."
            ),
        ],
    }
    certifier._certifier_invariants(output)  # no debe levantar


def test_combined_with_items_of_only_one_type_is_degraded() -> None:
    """(c) 'indice_oficial_combinado' con DOS citas de bucket distintas donde
    una es un item real y la otra es un mensaje de ausencia: solo UN tipo
    tiene evidencia real, la combinacion se degrada."""
    output = {
        "decision": "indice_oficial_combinado",
        "bucket": "combinado",
        "citations": _base_citations() + [
            _bucket_citation("Concurso Público 001/2026 - Edital de Abertura"),
            _bucket_citation("Nenhum registro encontrado."),
        ],
    }
    with pytest.raises(
        AgentOutputRejected,
        match="indice_vacio_sin_items:combinado_solo_un_tipo_con_items",
    ):
        certifier._certifier_invariants(output)


def test_combined_with_real_items_of_both_types_is_not_degraded() -> None:
    """Control positivo: dos citas de bucket distintas, NINGUNA de ausencia
    -> la combinacion se mantiene confirmada (sin cambios de comportamiento)."""
    output = {
        "decision": "indice_oficial_combinado",
        "bucket": "combinado",
        "citations": _base_citations() + [
            _bucket_citation("Concurso Público 001/2026 - Edital de Abertura"),
            _bucket_citation("Processo Seletivo 002/2026 - Edital de Abertura"),
        ],
    }
    certifier._certifier_invariants(output)  # no debe levantar


@pytest.mark.parametrize(
    "quote",
    [
        "Não foram encontrados Concursos / Processos Seletivos com os filtros selecionados.",
        "nao foram encontrados concursos com os filtros selecionados",
        "NÃO FORAM ENCONTRADOS CONCURSOS",
        "Nenhum registro encontrado.",
        "NENHUM REGISTRO ENCONTRADO",
        "nenhum resultado para a busca realizada",
        "Não há registros para o período selecionado.",
        "Nenhum item encontrado para os filtros informados.",
        "Não existem registros cadastrados.",
        "Sem resultados para esta consulta.",
        "Nao   foram\nencontrados   registros",  # espacios/saltos irregulares
    ],
)
def test_blocklist_matches_absence_messages_with_and_without_accents(quote: str) -> None:
    """(d) El blocklist matchea con y sin acentos, en mayusculas/minusculas y
    tolera espaciado irregular de texto ya renderizado."""
    assert certifier._is_absence_message(quote) is True


@pytest.mark.parametrize(
    "quote",
    [
        "Concurso Público 001/2026 - Edital de Abertura",
        "Processo Seletivo Simplificado 003/2025",
        "Edital de Concursos e Seleções Públicas",
        "Resultado Final do Concurso 001/2026",
    ],
)
def test_blocklist_does_not_flag_real_evidence_quotes(quote: str) -> None:
    """Control negativo: citas de evidencia real (item/pagina) no matchean el
    blocklist de ausencia."""
    assert certifier._is_absence_message(quote) is False
