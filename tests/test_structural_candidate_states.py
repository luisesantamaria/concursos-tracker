from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import verdict_extract as V  # noqa: E402


def test_single_event_is_enough_for_an_index():
    text = "\n".join([
        "Prefeitura", "Concursos Públicos", "Lista de eventos",
        "CONCURSO PÚBLICO Nº 01/2026", "Publicado em: 02/04/2026",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concursos Públicos")
    assert state == "indice_oficial"
    assert predicates["has_event_listing"] is True


def test_year_navigation_is_not_an_event_listing():
    text = "\n".join([
        "Prefeitura", "Concursos Públicos", "Escolha o ano",
        "Concursos Públicos 2024", "Concursos Públicos 2025",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concursos Públicos")
    assert state == "nao_encontrado"
    assert predicates["has_event_listing"] is False


def test_dated_editorial_article_is_rejected():
    text = "\n".join([
        "Prefeitura", "Notícias", "Compartilhe:", "CONCURSO PÚBLICO",
        "7 fevereiro 2024 11:25", "A administração anunciou a futura banca.",
        "Veja também", "Notícias recentes",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concurso Público — Prefeitura")
    assert state == "detalle_individual_rechazado"
    assert predicates["is_single_article"] is True


def test_single_governing_event_with_document_children_is_detail():
    text = "\n".join([
        "Prefeitura", "Portal", "Concurso Público",
        "CONCURSO PÚBLICO 2025", "DOWNLOADS DE DOCUMENTOS:",
        "EDITAL DE CONVOCAÇÃO Nº 02/2026", "Baixar agora!",
    ])
    state, predicates = V.candidate_content_state(
        text, "concursos", title="Concurso Público 2025")
    assert state == "detalle_individual_rechazado"
    assert predicates["is_single_event_document_detail"] is True


def test_incomplete_content_stays_review():
    text = "\n".join(["Portal", "Just a moment", "Checking your browser", "Enable JavaScript"])
    state, predicates = V.candidate_content_state(text, "processos")
    assert state == "revisar"
    assert predicates["content_complete"] is False
