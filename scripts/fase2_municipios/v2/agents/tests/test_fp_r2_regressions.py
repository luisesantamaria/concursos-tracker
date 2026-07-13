"""Regression fixtures for the seven suspected FPs from holdout r2.

Each fixture is the complete ``stages.A.raw`` object copied from the matching
``run_r2_postpalancas/observability`` artifact.  These tests exercise only the
deterministic item-evidence invariant; they never invoke a model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from scripts.fase2_municipios.v2.agents import certifier
from scripts.fase2_municipios.v2.agents.base import AgentOutputRejected
from scripts.fase2_municipios.v2.eval import live_abc_adapter


pytestmark = pytest.mark.offline


def _raw(value: str) -> dict[str, Any]:
    return json.loads(value)


_CAMAQUA_CONCURSO_PUBLICO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"concurso_publico","candidate_id":"v1:eb899ea0788449a91cb246422d0d3cf66b2ce672","citations":[{"dimension":"authority","end":40,"quote":"Portal do Cidadão - MUNICIPIO DE CAMAQUA","source_field":"heading","source_id":"main","start":0},{"dimension":"identity","end":40,"quote":"MUNICIPIO DE CAMAQUA","source_field":"heading","source_id":"main","start":20},{"dimension":"page_role","end":1726,"quote":"Concursos Públicos","source_field":"main_content","source_id":"main","start":1708},{"dimension":"bucket","end":1726,"quote":"Concursos Públicos","source_field":"main_content","source_id":"main","start":1708},{"dimension":"stability","end":1755,"quote":"Concurso 2015 Concursos 2024","source_field":"main_content","source_id":"main","start":1727}],"confidence":"high","decision":"indice_oficial","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_listado","reason":"A página é o portal oficial do município de Camaquã e apresenta uma seção estruturada de \u0027Concursos Públicos\u0027 com histórico (2015, 2024), caracterizando um índice estável e oficial para o bucket solicitado.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)

_CANELA_CONCURSO_PUBLICO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"concurso_publico","candidate_id":"v1:20b19a786b6f77d0b25ad7f6636cadbcc2305509","citations":[{"dimension":"authority","end":120,"quote":"Portal da Transparência | Município de Canela","source_field":"heading","source_id":"main","start":75},{"dimension":"identity","end":3877,"quote":"PREFEITURA MUNICIPAL DE CANELA","source_field":"main_content","source_id":"main","start":3847},{"dimension":"page_role","end":2202,"quote":"Edital de Concursos e Seleções Públicas","source_field":"heading","source_id":"main","start":2163},{"dimension":"bucket","end":4057,"quote":"Concurso ou Processo Seletivo","source_field":"main_content","source_id":"main","start":4028},{"dimension":"stability","end":3918,"quote":"Filtros Entidade Todas PREFEITURA MUNICIPAL DE CANELA Câmara Municipal de Vereadores de Canela","source_field":"main_content","source_id":"main","start":3824}],"confidence":"high","decision":"indice_oficial","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_listado","reason":"A página é o portal oficial de transparência do Município de Canela, contendo estrutura de índice (filtros, seleção de entidade e tipo de publicação) para concursos e processos seletivos. Embora não haja resultados no momento, a estrutura é estável e oficial, caracterizando um índice válido.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)

_GRAMADO_XAVIER_PROCESSO_SELETIVO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"processo_seletivo","candidate_id":"v1:a2575d18489cfc191249e5007fd42ed5805ecaba","citations":[{"dimension":"authority","end":47,"quote":"MUNICÍPIO DE GRAMADO XAVIER","source_field":"heading","source_id":"main","start":20},{"dimension":"identity","end":47,"quote":"MUNICÍPIO DE GRAMADO XAVIER","source_field":"heading","source_id":"main","start":20},{"dimension":"page_role","end":1466,"quote":"PROCESSOS SELETIVOS 2026 PROCESSOS SELETIVO 2025 COMDICA - 2023 Processo Seletivo 2024 Processo Seletivo 2023 Processo Seletivo 2022 Processo Seletivo 2021 Processo Seletivo Anos Anteriores","source_field":"main_content","source_id":"main","start":1277},{"dimension":"bucket","end":1301,"quote":"PROCESSOS SELETIVOS 2026","source_field":"main_content","source_id":"main","start":1277},{"dimension":"stability","end":1466,"quote":"Processo Seletivo Anos Anteriores","source_field":"main_content","source_id":"main","start":1433}],"confidence":"high","decision":"indice_oficial","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_listado","reason":"A página é um índice oficial do município de Gramado Xavier que agrega, de forma estável e estruturada, os processos seletivos por ano, cumprindo todos os requisitos de autoridade, identidade e estabilidade para o bucket solicitado.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)

_NOVA_SANTA_RITA_CONCURSO_PUBLICO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"combinado","candidate_id":"v1:942b10be9e0e2fd721868b99dba1b4ab875a21f1","citations":[{"dimension":"authority","end":48,"quote":"MUNICÍPIO DE NOVA SANTA RITA","source_field":"heading","source_id":"main","start":20},{"dimension":"identity","end":48,"quote":"MUNICÍPIO DE NOVA SANTA RITA","source_field":"heading","source_id":"main","start":20},{"dimension":"page_role","end":265,"quote":"CONCURSO PÚBLICO 2022 CONCURSO PÚBLICO 2023 EDITAIS 2025","source_field":"main_content","source_id":"main","start":209},{"dimension":"bucket","end":419,"quote":"CONCURSO PÚBLICO - Edital 01/2024","source_field":"main_content","source_id":"main","start":386},{"dimension":"bucket","end":550,"quote":"Processo Seletivo 2023","source_field":"main_content","source_id":"main","start":528},{"dimension":"stability","end":3154,"quote":"Concurso Público 2010 Concursos ..","source_field":"main_content","source_id":"main","start":3120}],"confidence":"high","decision":"indice_oficial_combinado","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_combinado","reason":"A página é um índice oficial do município que agrega tanto concursos públicos quanto processos seletivos, apresentando estrutura estável com listagens históricas. A natureza combinada é comprovada por citações distintas para cada bucket.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)

_NOVA_SANTA_RITA_PROCESSO_SELETIVO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"processo_seletivo","candidate_id":"v1:d3b4c5f45985df7b14e1304a27e4bbb5811c158e","citations":[{"dimension":"authority","end":48,"quote":"MUNICÍPIO DE NOVA SANTA RITA","source_field":"heading","source_id":"main","start":20},{"dimension":"identity","end":48,"quote":"MUNICÍPIO DE NOVA SANTA RITA","source_field":"heading","source_id":"main","start":20},{"dimension":"page_role","end":357,"quote":"Processos Seletivos 2025","source_field":"main_content","source_id":"main","start":333},{"dimension":"bucket","end":357,"quote":"Processos Seletivos 2025","source_field":"main_content","source_id":"main","start":333},{"dimension":"stability","end":550,"quote":"Processo Seletivo 2023","source_field":"main_content","source_id":"main","start":528}],"confidence":"high","decision":"indice_oficial","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_listado","reason":"A página é o portal oficial do município de Nova Santa Rita e apresenta uma listagem estruturada de processos seletivos ao longo dos anos (2025, 2023, 2022, 2021, 2020), caracterizando um índice estável e oficial para o bucket solicitado.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)

_PALMITINHO_CONCURSO_PUBLICO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"concurso_publico","candidate_id":"v1:e292a722742e54beddc3e187db01de9902cb374e","citations":[{"dimension":"authority","end":91,"quote":"MUNICÍPIO DE PALMITINHO","source_field":"heading","source_id":"main","start":68},{"dimension":"identity","end":91,"quote":"MUNICÍPIO DE PALMITINHO","source_field":"heading","source_id":"main","start":68},{"dimension":"page_role","end":1323,"quote":"EDITAIS DE CONCURSO PÚBLICO","source_field":"heading","source_id":"main","start":1296},{"dimension":"bucket","end":1323,"quote":"EDITAIS DE CONCURSO PÚBLICO","source_field":"heading","source_id":"main","start":1296},{"dimension":"stability","end":1482,"quote":"A partir desse serviço é possível consultar e fazer download de informações dos concursos públicos relalizados.","source_field":"main_content","source_id":"main","start":1371}],"confidence":"high","decision":"indice_oficial","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_listado","reason":"A página é o portal oficial do município de Palmitinho, contendo uma seção dedicada e estável para consulta de editais de concursos públicos, conforme provado pelo título e descrição do serviço.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)

_PROGRESSO_PROCESSO_SELETIVO_A_RAW = _raw(
    r'''{"authority":"confirmada","bucket":"processo_seletivo","candidate_id":"v1:9ac801fc6e10ab3e7f3974fb62dcb772b57c9b63","citations":[{"dimension":"authority","end":46,"quote":"Portal da Transparência Município de Progresso","source_field":"heading","source_id":"main","start":0},{"dimension":"identity","end":12852,"quote":"Prefeitura Municipal de Progresso Rua 4 de Novembro, 1150, Centro, Progresso RS","source_field":"main_content","source_id":"main","start":12773},{"dimension":"page_role","end":5532,"quote":"Processo Seletivo 2025 Edital_Nº_2113_10_Abre_Inscrições_PS_Motorista Edital_Nº_2193_10_Abre_Inscrições_PS_Psicólogo_e_Monitor Infantil","source_field":"main_content","source_id":"main","start":5397},{"dimension":"bucket","end":5419,"quote":"Processo Seletivo 2025","source_field":"main_content","source_id":"main","start":5397},{"dimension":"stability","end":6360,"quote":"2026 Edital_N°_2270_10_Abre_Inscrições_PS_Médico_Veterinário_SIM Edital_Nº_2257_10 Abre_Inscrições_P.S._Monitores_Esporte","source_field":"main_content","source_id":"main","start":6239}],"confidence":"high","decision":"indice_oficial","evidence_state":"completa","identity":"confirmada","insufficiency":"none","learning_proposal":null,"page_role":"indice_listado","reason":"A página é o portal oficial de transparência do município de Progresso e contém uma seção estruturada e estável denominada \u0027Processo Seletivo\u0027, que agrega editais de processos seletivos de diversos anos (2017 a 2026), cumprindo todos os requisitos de um índice oficial para o bucket solicitado.","source_kind":"dominio_oficial_prefeitura","tool_request":null}'''
)


def _assert_item_evidence_rejection(raw: dict[str, Any]) -> None:
    with pytest.raises(
        AgentOutputRejected,
        match="indice_sin_evidencia_de_items",
    ):
        certifier._certifier_invariants(raw)


def test_camaqua_concurso_publico_real_raw_rejects_section_label() -> None:
    _assert_item_evidence_rejection(_CAMAQUA_CONCURSO_PUBLICO_A_RAW)


def test_canela_concurso_publico_real_raw_rejects_filter_label() -> None:
    _assert_item_evidence_rejection(_CANELA_CONCURSO_PUBLICO_A_RAW)


def test_gramado_xavier_processo_seletivo_real_raw_rejects_annual_menu() -> None:
    _assert_item_evidence_rejection(_GRAMADO_XAVIER_PROCESSO_SELETIVO_A_RAW)


def test_nova_santa_rita_concurso_publico_real_raw_rejects_menu_entries() -> None:
    _assert_item_evidence_rejection(_NOVA_SANTA_RITA_CONCURSO_PUBLICO_A_RAW)


def test_nova_santa_rita_processo_seletivo_real_raw_rejects_annual_menu() -> None:
    _assert_item_evidence_rejection(_NOVA_SANTA_RITA_PROCESSO_SELETIVO_A_RAW)


def test_palmitinho_concurso_publico_real_raw_rejects_service_heading() -> None:
    _assert_item_evidence_rejection(_PALMITINHO_CONCURSO_PUBLICO_A_RAW)


def test_progresso_processo_seletivo_real_raw_rejects_section_year_label() -> None:
    _assert_item_evidence_rejection(_PROGRESSO_PROCESSO_SELETIVO_A_RAW)


def test_keyword_plus_year_only_is_not_item_positive() -> None:
    assert certifier._is_item_positive_quote("PROCESSOS SELETIVOS 2026") is False


def test_keyword_plus_number_year_is_item_positive() -> None:
    assert certifier._is_item_positive_quote("Processo Seletivo 001/2026") is True


def test_keyword_plus_full_date_is_item_positive() -> None:
    assert certifier._is_item_positive_quote("Edital publicado em 13/07/2026") is True


def _single_bucket_output(quote: str) -> dict[str, Any]:
    return {
        "decision": "indice_oficial",
        "bucket": "concurso_publico",
        "citations": [
            {"dimension": "authority", "quote": "MUNICÍPIO DE EXEMPLO", "source_id": "main"},
            {"dimension": "identity", "quote": "MUNICÍPIO DE EXEMPLO", "source_id": "main"},
            {"dimension": "page_role", "quote": "Concursos Públicos", "source_id": "main"},
            {"dimension": "stability", "quote": "Arquivo", "source_id": "main"},
            {"dimension": "bucket", "quote": quote, "source_id": "main"},
        ],
    }


def _with_navigation_metadata(
    output: dict[str, Any],
    *,
    html: str,
    source_text: str,
) -> dict[str, Any]:
    fetched = live_abc_adapter.FetchedEvidence(
        requested_url="https://example.invalid/concursos",
        final_url="https://example.invalid/concursos",
        retrieved_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        status=200,
        content=source_text,
        html=html,
        title="Concursos Públicos",
    )
    snapshot, _ = live_abc_adapter.LiveABCAdapter._snapshots(fetched)
    return certifier._prepare_certifier_output(snapshot, output)


def test_item_citation_occurring_only_in_navigation_is_rejected() -> None:
    quote = "CONCURSO PÚBLICO - Edital 01/2024"
    output = _with_navigation_metadata(
        _single_bucket_output(quote),
        html=(
            f"<header><a>{quote}</a></header>"
            "<main>MUNICÍPIO DE EXEMPLO Concursos Públicos Arquivo</main>"
        ),
        source_text=f"{quote} MUNICÍPIO DE EXEMPLO Concursos Públicos Arquivo",
    )
    _assert_item_evidence_rejection(output)


def test_item_citation_occurring_in_navigation_and_body_is_accepted() -> None:
    quote = "CONCURSO PÚBLICO - Edital 01/2024"
    output = _with_navigation_metadata(
        _single_bucket_output(quote),
        html=(
            f"<nav><a>{quote}</a></nav>"
            f"<main>MUNICÍPIO DE EXEMPLO Concursos Públicos Arquivo {quote}</main>"
        ),
        source_text=(
            f"{quote} MUNICÍPIO DE EXEMPLO Concursos Públicos Arquivo {quote}"
        ),
    )
    certifier._certifier_invariants(output)


def test_navigation_zone_extractor_covers_landmarks_and_class_id_hints() -> None:
    zone_texts = live_abc_adapter._navigation_zone_texts(
        "<header>Cabeçalho</header>"
        "<footer>Rodapé</footer>"
        "<aside>Lateral</aside>"
        "<div class='primary-menu'>Menu</div>"
        "<section id='main-navigation'>Navegação</section>"
        "<div class='sidebar'>Barra lateral</div>"
        "<div id='megaMenu'>Mega menu</div>"
        "<main>Conteúdo real</main>"
    )
    combined = " | ".join(zone_texts)
    for expected in (
        "Cabeçalho", "Rodapé", "Lateral", "Menu",
        "Navegação", "Barra lateral", "Mega menu",
    ):
        assert expected in combined
    assert "Conteúdo real" not in combined
