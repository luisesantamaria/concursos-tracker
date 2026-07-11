from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "shared"))

import cascade_municipios as C  # noqa: E402
import verdict_extract as V  # noqa: E402


def test_single_result_structural_index_overrides_document_detail():
    text = "\n".join([
        "Prefeitura Municipal de Cidade Exemplo", "Concursos Públicos",
        "Formulário de busca", "Filtrar por palavra-chave", "Buscar",
        "1 resultado encontrado", "Exportar resultados", "Página 1",
        "CONCURSO PÚBLICO Nº 01/2026", "Publicado em: 02/04/2026",
        "DOWNLOADS DE DOCUMENTOS:",
        "EDITAL DE CONVOCAÇÃO Nº 02/2026", "Baixar agora!",
    ])
    anchors = [
        {"href": "https://cidadeexemplo.rs.gov.br/concursos?page=1", "text": "Página 1"},
        {"href": "https://cidadeexemplo.rs.gov.br/anexo.pdf", "text": "Edital de convocação"},
    ]
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concursos Públicos", anchors=anchors)
    assert state == "indice_oficial"
    assert predicates["has_event_listing"] is True
    assert predicates["is_single_event_document_detail"] is True
    assert predicates["has_structural_index_signals"] is True

    page = C.Page(
        url="https://cidadeexemplo.rs.gov.br/concursos?page=1", status=200,
        title="Concursos Públicos", text=text,
        links=[(anchor["href"], anchor["text"]) for anchor in anchors],
    )
    candidate = C.candidate_from_evidence(
        page.url, "fixture", "Concursos Públicos", "Cidade Exemplo", page,
    )
    assert candidate.fetchable is True


def test_year_navigation_without_listing_is_rejected():
    text = "\n".join([
        "Prefeitura", "Concursos Públicos", "Escolha o ano",
        "Concursos Públicos 2024", "Concursos Públicos 2025",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concursos Públicos")
    assert state == "revisar"
    assert predicates["has_event_listing"] is False
    assert predicates["page_role"] == "menu_sin_listado"


def test_numeric_news_article_without_listing_is_rejected():
    text = "\n".join([
        "Prefeitura", "Notícias", "Compartilhe:", "CONCURSO PÚBLICO",
        "7 fevereiro 2024 11:25", "A administração anunciou a futura banca.",
        "Veja também", "Notícias recentes",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concurso Público — Prefeitura")
    assert state == "nao_encontrado"
    assert predicates["is_single_article"] is True
    assert predicates["page_role"] == "noticia"


def test_single_event_document_detail_without_index_signals_is_rejected():
    text = "\n".join([
        "Prefeitura", "Portal", "Concurso Público",
        "CONCURSO PÚBLICO 2025", "DOWNLOADS DE DOCUMENTOS:",
        "EDITAL DE CONVOCAÇÃO Nº 02/2026", "Baixar agora!",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concurso Público 2025")
    assert state == "detalle_individual_rechazado"
    assert predicates["is_single_event_document_detail"] is True
    assert predicates["has_structural_index_signals"] is False

    page = C.Page(
        url="https://cidadeexemplo.rs.gov.br/concurso/2025", status=200,
        title="Concurso Público 2025", text=text,
    )
    candidate = C.candidate_from_evidence(
        page.url, "fixture", "Concurso Público", "Cidade Exemplo", page,
    )
    assert candidate.accessible is True
    assert candidate.eligible is False
    assert candidate.decision == "detalle_individual_rechazado"


def test_multiple_event_index_remains_official():
    text = "\n".join([
        "Prefeitura", "Concursos Públicos", "Lista de eventos",
        "CONCURSO PÚBLICO Nº 01/2026", "Publicado em: 02/04/2026",
        "CONCURSO PÚBLICO Nº 02/2025", "Publicado em: 02/03/2025",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concursos Públicos")
    assert state == "indice_oficial"
    assert predicates["has_event_listing"] is True


def test_incomplete_content_stays_review():
    text = "\n".join(["Portal", "Just a moment", "Checking your browser", "Enable JavaScript"])
    state, predicates = V.candidate_content_state(text, "processos")
    assert state == "revisar"
    assert predicates["content_complete"] is False
