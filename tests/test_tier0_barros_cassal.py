from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "shared"))

import cascade_municipios as C  # noqa: E402


class _Response:
    status_code = 200
    url = "https://www.barroscassal.rs.gov.br/"
    headers = {"content-type": "text/html; charset=UTF-8"}
    text = """<!doctype html>
<html lang="pt-BR">
  <head>
    <title>Portal da Transparência</title>
    <script src="/_next/static/chunks/main.js"></script>
  </head>
  <body><div id="__next"></div></body>
</html>"""


class _OfflineSession:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.requested_urls.append(url)
        return _Response()


class Tier0BarrosCassalTest(unittest.TestCase):
    def test_domain_confirmation_keeps_officiality_guardrails(self):
        third_party = C.Page(
            url="https://barroscassal.example.com", status=200,
            title="Portal da Transparência",
        )
        other_municipality = C.Page(
            url="https://outromunicipio.rs.gov.br", status=200,
            title="Portal da Transparência",
        )
        parked = C.Page(
            url="https://barroscassal.rs.gov.br", status=200,
            title="Site em construção",
        )

        self.assertFalse(C.is_matching_official_municipality_domain(
            third_party, "Barros Cassal",
        ))
        self.assertFalse(C.is_matching_official_municipality_domain(
            other_municipality, "Barros Cassal",
        ))
        self.assertFalse(C.is_matching_official_municipality_domain(
            parked, "Barros Cassal",
        ))

    def test_grounded_200_municipal_domain_is_not_site_not_found(self):
        grounded_response = {
            "candidates": [{
                "content": {"parts": [{
                    "text": "https://www.barroscassal.rs.gov.br/",
                }]},
                "groundingMetadata": {"groundingChunks": []},
            }],
        }
        session = _OfflineSession()

        with (
            patch.object(C, "gemini_api_key", return_value="offline-key"),
            patch.object(C, "gemini_post", return_value=grounded_response),
            patch.object(C, "tier0_find_site", return_value=None),
            patch.object(C, "tier1_collect_candidates", return_value=[]),
            patch.object(C, "_probe_known_index_paths", return_value=[]),
            patch.object(C, "tier2_grounded_search", return_value=[]),
            patch.object(C, "tier2_directed_bucket_search", return_value=[]),
        ):
            result = C.process_municipio(
                session, "Barros Cassal", "gemini-2.5-flash", use_playwright=False,
            )

        self.assertEqual(
            session.requested_urls, ["https://www.barroscassal.rs.gov.br"],
        )
        self.assertEqual(result.site_base, "https://www.barroscassal.rs.gov.br")
        self.assertNotEqual(result.notes, "site_not_found")
        self.assertTrue(result.method.startswith("t2site"))


if __name__ == "__main__":
    unittest.main()
