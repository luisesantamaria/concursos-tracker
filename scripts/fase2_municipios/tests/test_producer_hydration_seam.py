from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as cascade  # noqa: E402


MUNICIPIO = "Exemplo"
BASE = "https://www.exemplo.rs.gov.br"
URL = BASE + "/concursos"


def _hydrated(candidate: cascade.Candidate, source_tier: str) -> cascade.Candidate:
    candidate.source_tier = source_tier
    return candidate


def test_tier1_real_producer_calls_common_hydrator() -> None:
    home = cascade.Page(
        url=BASE, status=200, title="Prefeitura Municipal de Exemplo",
        text="Prefeitura Municipal de Exemplo",
        links=[(URL, "Concursos Públicos")],
    )
    with patch.object(
            cascade, "hydrate_candidate",
            side_effect=lambda candidate, *args, **kwargs: _hydrated(candidate, "tier1"),
    ) as hydrate:
        candidates = cascade.tier1_collect_candidates(Mock(), home, MUNICIPIO)

    assert candidates[0].source_tier == "tier1"
    hydrate.assert_called()


def _grounding_payload() -> dict:
    return {
        "candidates": [{
            "content": {"parts": [{"text": URL}]},
            "groundingMetadata": {"groundingChunks": []},
        }],
    }


def test_grounded_real_producer_calls_common_hydrator() -> None:
    with (
        patch.object(cascade, "gemini_post", return_value=_grounding_payload()),
        patch.object(
            cascade, "hydrate_candidate",
            side_effect=lambda candidate, *args, **kwargs: _hydrated(
                candidate, "tier2_grounded"),
        ) as hydrate,
    ):
        candidates = cascade.tier2_grounded_search(
            Mock(), "gemini-2.5-flash", MUNICIPIO, BASE,
        )

    assert candidates[0].source_tier == "tier2_grounded"
    hydrate.assert_called()


def test_directed_real_producer_calls_common_hydrator() -> None:
    with (
        patch.object(cascade, "gemini_post", return_value=_grounding_payload()),
        patch.object(
            cascade, "hydrate_candidate",
            side_effect=lambda candidate, *args, **kwargs: _hydrated(
                candidate, "tier2_directed"),
        ) as hydrate,
    ):
        candidates = cascade.tier2_directed_bucket_search(
            Mock(), "gemini-2.5-flash", MUNICIPIO,
            "www.exemplo.rs.gov.br", "concursos publicos",
        )

    assert candidates[0].source_tier == "tier2_directed"
    hydrate.assert_called()


def test_playwright_real_producer_calls_common_hydrator() -> None:
    snapshot = Mock()
    with patch.object(
            cascade, "hydrate_candidate",
            side_effect=lambda candidate, *args, **kwargs: _hydrated(candidate, "tier4"),
    ) as hydrate:
        candidates = cascade._tier4_candidates_from_links(
            [(URL, "Concursos Públicos")], MUNICIPIO,
            render_page=lambda _url: snapshot,
        )

    assert candidates[0].source_tier == "tier4"
    hydrate.assert_called_once()
