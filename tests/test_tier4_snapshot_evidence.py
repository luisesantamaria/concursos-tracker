from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "shared"))

import cascade_municipios as C  # noqa: E402


MUNICIPIO = "Nova Esperança"
BASE = "https://www.novaesperanca.rs.gov.br/"
INDEX = BASE + "concursos-e-processos-seletivos"


VALID_COMBINED = "\n".join([
    "Prefeitura Municipal de Nova Esperança",
    "Concursos e Processos Seletivos",
    "Lista de eventos",
    "CONCURSO PÚBLICO Nº 01/2026",
    "Publicado em: 02/04/2026",
    "CONCURSO PÚBLICO Nº 02/2025",
    "Publicado em: 02/03/2025",
    "PROCESSO SELETIVO SIMPLIFICADO Nº 03/2026",
    "Publicado em: 03/04/2026",
    "PROCESSO SELETIVO SIMPLIFICADO Nº 04/2025",
    "Publicado em: 03/03/2025",
])


def snapshot(url: str = INDEX, *, text: str = VALID_COMBINED,
             title: str = "Concursos e Processos Seletivos",
             status: int | None = None) -> C.EvidenceSnapshot:
    return C.EvidenceSnapshot(
        html=f"<html><head><title>{title}</title></head><body>{text}</body></html>",
        text=text,
        title=title,
        final_url=url,
        status=status,
        source="playwright",
    )


def collect_one(rendered: C.EvidenceSnapshot, *, href: str = INDEX,
                municipio: str = MUNICIPIO):
    renderer = Mock(return_value=rendered)
    candidates = C._tier4_candidates_from_links(
        [(href, "Concursos e Processos Seletivos")], municipio, renderer,
    )
    return candidates, renderer


def test_t1_tier4_snapshot_reaches_end_to_end_acceptance_despite_checkpoint_get():
    checkpoint_get = Mock(return_value=C.Page(
        url=INDEX, status=403, title="Vercel Security Checkpoint",
        text="Verifying you are human",
    ))
    renderer = Mock(return_value=snapshot())

    def tier4(_url, municipio):
        return C._tier4_candidates_from_links(
            [(INDEX, "Concursos e Processos Seletivos")], municipio, renderer,
        )

    gemini_response = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({
            "classificacoes": [{
                "id": 0, "forma": "indice", "tipo": "mixto",
                "razao": "índice oficial combinado",
            }],
            "melhor_id_concursos": 0,
            "melhor_id_processos": 0,
            "razao": "índice oficial combinado",
        })}]}}],
    }
    home = C.Page(
        url=BASE, status=200, title="Prefeitura Municipal de Nova Esperança",
        text="Prefeitura Municipal de Nova Esperança",
    )
    with (
        patch.object(C, "tier0_find_site", return_value=home),
        patch.object(C, "tier1_collect_candidates", return_value=[]),
        patch.object(C, "_probe_known_index_paths", return_value=[]),
        patch.object(C, "tier2_grounded_search", return_value=[]),
        patch.object(C, "tier2_directed_bucket_search", return_value=[]),
        patch.object(C, "tier4_playwright_collect", side_effect=tier4),
        patch.object(C, "gemini_api_key", return_value="offline-key"),
        patch.object(C, "gemini_post", return_value=gemini_response),
        patch.object(C, "fetch_page", checkpoint_get),
        patch.object(C, "_render_text", return_value=""),
    ):
        result = C.process_municipio(
            object(), MUNICIPIO, "gemini-2.5-flash", use_playwright=True,
        )

    assert result.url_concursos == INDEX
    assert result.url_processos_seletivos == INDEX
    assert result.confianza_concursos == "confirmado"
    assert result.confianza_processos == "confirmado"
    renderer.assert_called_once_with(INDEX)
    checkpoint_get.assert_not_called()


def test_t2_valid_snapshot_prevents_second_requests_read():
    requests_fetch = Mock(side_effect=AssertionError("second GET must not happen"))
    with patch.object(C, "fetch_page", requests_fetch):
        candidates, renderer = collect_one(snapshot())

    assert candidates[0].fetchable is True
    assert candidates[0].page is not None
    assert candidates[0].page.status is None
    assert candidates[0].source == "playwright"
    renderer.assert_called_once_with(INDEX)
    # RED before the fix: tier4_playwright_collect called fetch_page for this URL.
    requests_fetch.assert_not_called()


def test_snapshot_none_status_is_neutral_but_captured_http_error_is_rejected():
    neutral, _ = collect_one(snapshot(status=None))
    failed, _ = collect_one(snapshot(status=503))

    assert neutral[0].fetchable is True
    assert failed[0].fetchable is False


def test_canonical_candidate_api_accepts_http_or_playwright_evidence():
    browser_candidate = C.candidate_from_evidence(
        INDEX, "playwright", "Concursos", MUNICIPIO, snapshot(),
    )
    http_page = C.Page(
        url=INDEX, requested_url=INDEX, status=200,
        title="Concursos e Processos Seletivos", text=VALID_COMBINED,
    )
    http_candidate = C.candidate_from_evidence(
        INDEX, "grounding", "Concursos", MUNICIPIO, http_page,
    )

    assert browser_candidate.fetchable is True
    assert http_candidate.fetchable is True
    assert browser_candidate.source == "playwright"
    assert http_candidate.source == "grounding"


INVALID_RENDER_CASES = [
    pytest.param(
        "Página não encontrada\nErro 404\nConteúdo indisponível\nPrefeitura Municipal de Nova Esperança",
        "Página não encontrada", id="soft404",
    ),
    pytest.param(
        "Prefeitura Municipal de Nova Esperança\nSite em construção\nVolte em breve\nSem publicações",
        "Site em construção", id="dead-site",
    ),
    pytest.param(
        "\n".join([
            "Prefeitura Municipal de Nova Esperança", "Notícias", "Compartilhe:",
            "CONCURSO PÚBLICO", "7 fevereiro 2024 11:25",
            "A administração anunciou a futura banca.", "Veja também",
            "Notícias relacionadas",
        ]),
        "Concurso Público — Prefeitura", id="noticia",
    ),
    pytest.param(
        "\n".join([
            "Prefeitura Municipal de Nova Esperança", "Concursos Públicos",
            "Escolha o ano", "Concursos Públicos 2024", "Concursos Públicos 2025",
        ]),
        "Concursos Públicos", id="menu-sin-listado",
    ),
    pytest.param(
        "\n".join([
            "Prefeitura Municipal de Nova Esperança", "Portal", "Concurso Público",
            "CONCURSO PÚBLICO 2025", "DOWNLOADS DE DOCUMENTOS:",
            "EDITAL DE CONVOCAÇÃO Nº 02/2026", "Baixar agora!",
        ]),
        "Concurso Público 2025", id="detalle-individual",
    ),
]


@pytest.mark.parametrize(("text", "title"), INVALID_RENDER_CASES)
def test_t3_invalid_render_is_rejected_by_canonical_gates(text, title):
    candidates, _ = collect_one(snapshot(text=text, title=title))
    assert len(candidates) == 1
    assert candidates[0].fetchable is False


def test_t4_other_official_municipality_and_nonmunicipal_third_party_are_rejected():
    other_official = snapshot(
        "https://www.soledade.rs.gov.br/concursos",
        text=VALID_COMBINED.replace("Nova Esperança", "Soledade"),
    )
    third_party = snapshot(
        "https://concursos.example.org/listagem",
        text=VALID_COMBINED.replace("Nova Esperança", "Entidade Regional"),
    )

    other_candidates, _ = collect_one(other_official)
    third_candidates, _ = collect_one(third_party)

    assert other_candidates[0].fetchable is False
    assert third_candidates[0].fetchable is False


@pytest.mark.parametrize(("initial_url", "final_url", "expected"), [
    pytest.param(
        INDEX, "https://www.soledade.rs.gov.br/concursos", False,
        id="initial-passes-final-fails",
    ),
    pytest.param(
        "https://www.soledade.rs.gov.br/concursos", INDEX, True,
        id="initial-fails-final-passes",
    ),
])
def test_t5_identity_uses_final_url_after_redirect(initial_url, final_url, expected):
    candidates, renderer = collect_one(snapshot(final_url), href=initial_url)
    assert candidates[0].fetchable is expected
    assert candidates[0].url == final_url
    assert candidates[0].page.url == final_url
    renderer.assert_called_once_with(initial_url)


def test_t6_multiple_playwright_candidates_are_validated_individually():
    valid_url = INDEX
    invalid_url = BASE + "noticias/concurso-publico"
    rendered = {
        valid_url: snapshot(valid_url),
        invalid_url: snapshot(
            invalid_url,
            text="\n".join([
                "Prefeitura Municipal de Nova Esperança", "Notícias", "Compartilhe:",
                "CONCURSO PÚBLICO", "7 fevereiro 2024 11:25", "Nota individual",
                "Veja também", "Notícias relacionadas",
            ]),
            title="Notícia sobre Concurso Público",
        ),
    }
    renderer = Mock(side_effect=lambda url: rendered[url])

    candidates = C._tier4_candidates_from_links([
        (valid_url, "Concursos e Processos Seletivos"),
        (invalid_url, "Notícia Concurso Público"),
    ], MUNICIPIO, renderer)

    assert renderer.call_count == 2
    assert [candidate.url for candidate in candidates if candidate.fetchable] == [valid_url]
    assert [candidate.url for candidate in candidates if not candidate.fetchable] == [invalid_url]
