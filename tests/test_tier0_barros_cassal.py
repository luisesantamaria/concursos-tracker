from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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
    challenge = C.Page(
        url="https://www.barroscassal.rs.gov.br/", status=403,
        title="Vercel Security Checkpoint",
        text="Vercel Security Checkpoint - verifying you are human",
    )

    def _tier0_with(self, fetched_page, rendered):
        renderer = Mock(side_effect=lambda _url: C.RenderedPage(**rendered))
        with (
            patch.object(C, "domain_candidates", return_value=[
                "https://www.barroscassal.rs.gov.br/",
            ]),
            patch.object(C, "fetch_page", return_value=fetched_page),
        ):
            home = C.tier0_find_site(
                object(), "Barros Cassal", render_page=renderer,
            )
        return home, renderer

    def test_challenge_render_valid_recovers_official_site(self):
        home, renderer = self._tier0_with(self.challenge, {
            "html": """<html><head><title>Município de Barros Cassal</title></head>
                <body>Município de Barros Cassal
                <a href='/portal-da-transparencia/concursos-publicos'>
                Concursos Públicos</a></body></html>""",
            "text": (
                "Município de Barros Cassal, abertura do Concurso Público "
                "Edital 01/2026"
            ),
            "title": "Município de Barros Cassal",
            "final_url": "https://www.barroscassal.rs.gov.br/",
        })

        renderer.assert_called_once_with("https://www.barroscassal.rs.gov.br/")
        self.assertIsNotNone(home)
        self.assertIn("concursos-publicos", home.links[0][0])
        self.assertFalse(C.is_broad_landing(home.links[0][0]))

    def test_recovered_site_is_not_reported_as_site_not_found(self):
        renderer = Mock(side_effect=lambda _url: C.RenderedPage(
            html="""<html><body>Município de Barros Cassal
                <a href='/portal-da-transparencia/concursos-publicos'>
                Concursos Públicos</a></body></html>""",
            text="Município de Barros Cassal - Concurso Público Edital 01/2026",
            title="Município de Barros Cassal",
            final_url="https://www.barroscassal.rs.gov.br/",
        ))
        with (
            patch.object(C, "domain_candidates", return_value=[self.challenge.url]),
            patch.object(C, "fetch_page", return_value=self.challenge),
            patch.object(C, "gemini_api_key", return_value=""),
        ):
            result = C.process_municipio(
                object(), "Barros Cassal", "gemini-2.5-flash",
                use_playwright=False, render_page=renderer,
            )

        self.assertNotEqual(result.notes, "site_not_found")
        self.assertEqual(result.site_base, "https://www.barroscassal.rs.gov.br")

    def test_tier2_rendered_candidate_uses_normal_bucket_classification(self):
        index_url = (
            "https://www.barroscassal.rs.gov.br/"
            "portal-da-transparencia/concursos-publicos"
        )
        response = {
            "candidates": [{
                "content": {"parts": [{"text": index_url}]},
                "groundingMetadata": {"groundingChunks": []},
            }],
        }
        challenge = C.Page(
            url=index_url, status=403, title="Vercel Security Checkpoint",
            text="Verifying you are human",
        )
        renderer = Mock(side_effect=lambda _url: C.RenderedPage(
            html="""<html><body>Município de Barros Cassal, abertura do
                Concurso Público Edital 01/2026</body></html>""",
            text=(
                "Município de Barros Cassal, abertura do Concurso Público "
                "Edital 01/2026"
            ),
            title="Concursos Públicos - Município de Barros Cassal",
            final_url=index_url,
        ))
        with (
            patch.object(C, "gemini_post", return_value=response),
            patch.object(C, "fetch_page", return_value=challenge),
        ):
            candidates = C.tier2_grounded_search(
                object(), "gemini-2.5-flash", "Barros Cassal",
                "https://www.barroscassal.rs.gov.br/",
                render_page=renderer,
            )

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].fetchable)
        routed = C._route_classified_candidates(candidates, [{
            "id": 0, "forma": "indice", "tipo": "concurso",
            "razao": "listagem oficial",
        }])
        self.assertEqual(routed["url_concursos"], index_url)
        self.assertEqual(routed["decision_concursos"], "indice_oficial")

    def test_challenge_render_still_checkpoint_is_rejected(self):
        home, renderer = self._tier0_with(self.challenge, {
            "html": "<title>Vercel Security Checkpoint</title>",
            "text": "Checking your browser - verifying you are human",
            "title": "Vercel Security Checkpoint",
            "final_url": "https://www.barroscassal.rs.gov.br/",
        })

        renderer.assert_called_once()
        self.assertIsNone(home)

    def test_challenge_render_redirect_to_third_party_is_rejected(self):
        home, renderer = self._tier0_with(self.challenge, {
            "html": "<title>Município de Barros Cassal</title>",
            "text": "Município de Barros Cassal - Concurso Público",
            "title": "Município de Barros Cassal",
            "final_url": "https://barroscassal.atende.net/concursos",
        })

        renderer.assert_called_once()
        self.assertIsNone(home)

    def test_challenge_render_other_municipality_is_rejected(self):
        home, renderer = self._tier0_with(self.challenge, {
            "html": "<title>Município de Soledade</title>",
            "text": "Município de Soledade - Concurso Público Edital 01/2026",
            "title": "Município de Soledade",
            "final_url": "https://www.barroscassal.rs.gov.br/",
        })

        renderer.assert_called_once()
        self.assertIsNone(home)

    def test_static_happy_path_does_not_call_renderer(self):
        page = C.Page(
            url="https://www.barroscassal.rs.gov.br/", status=200,
            title="Município de Barros Cassal",
            text="Prefeitura Municipal de Barros Cassal",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with (
            patch.object(C, "domain_candidates", return_value=[page.url]),
            patch.object(C, "fetch_page", return_value=page),
        ):
            home = C.tier0_find_site(
                object(), "Barros Cassal", render_page=renderer,
            )

        self.assertIs(home, page)
        renderer.assert_not_called()

    def test_challenge_on_nonmatching_official_domain_does_not_render(self):
        page = C.Page(
            url="https://www.soledade.rs.gov.br/", status=403,
            title="Vercel Security Checkpoint",
            text="Checking your browser",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with (
            patch.object(C, "domain_candidates", return_value=[page.url]),
            patch.object(C, "fetch_page", return_value=page),
        ):
            home = C.tier0_find_site(
                object(), "Barros Cassal", render_page=renderer,
            )

        self.assertIsNone(home)
        renderer.assert_not_called()

    def test_network_failure_does_not_render(self):
        page = C.Page(
            url="https://www.barroscassal.rs.gov.br/",
            error="NameResolutionError: failed to resolve host",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with (
            patch.object(C, "domain_candidates", return_value=[page.url]),
            patch.object(C, "fetch_page", return_value=page),
        ):
            home = C.tier0_find_site(
                object(), "Barros Cassal", render_page=renderer,
            )

        self.assertIsNone(home)
        renderer.assert_not_called()

    def test_ordinary_http_error_without_signature_does_not_render(self):
        page = C.Page(
            url="https://www.barroscassal.rs.gov.br/", status=503,
            title="Service Unavailable", text="Please try again later",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with (
            patch.object(C, "domain_candidates", return_value=[page.url]),
            patch.object(C, "fetch_page", return_value=page),
        ):
            home = C.tier0_find_site(
                object(), "Barros Cassal", render_page=renderer,
            )

        self.assertIsNone(home)
        self.assertFalse(C.is_antibot_challenge(page))
        renderer.assert_not_called()

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


class OfficialFetchFallbackContractTest(unittest.TestCase):
    official_url = "https://www.barroscassal.rs.gov.br/"

    def _fetch(self, normal_page, rendered, *, url=None, municipio="Barros Cassal",
               official_url=None):
        renderer = Mock(return_value=rendered)
        with patch.object(C, "fetch_page", return_value=normal_page):
            page = C.fetch_page_with_official_fallback(
                object(), url or normal_page.url, municipio,
                official_url or self.official_url, render_page=renderer,
            )
        return page, renderer

    def test_wrapper_challenge_render_valid_revalidates_page(self):
        checkpoint = C.Page(
            url=self.official_url, status=403,
            title="Vercel Security Checkpoint",
            text="Vercel Security Checkpoint - verifying you are human " * 6,
        )
        rendered = C.RenderedPage(
            html="""<html><head><title>Município de Barros Cassal</title></head>
                <body>Prefeitura Municipal de Barros Cassal
                <a href='/concursos'>Concursos Públicos</a></body></html>""",
            text="Prefeitura Municipal de Barros Cassal - Concursos Públicos",
            title="Município de Barros Cassal",
            final_url=self.official_url,
        )

        page, renderer = self._fetch(checkpoint, rendered)

        renderer.assert_called_once_with(self.official_url)
        self.assertTrue(page.ok)
        self.assertFalse(C.is_soft_404(page))
        self.assertFalse(C.is_dead_site(page))
        self.assertEqual(page.requested_url, self.official_url)
        self.assertEqual(page.url, self.official_url)
        self.assertIn("/concursos", page.links[0][0])

    def test_wrapper_challenge_render_soft404_returns_original(self):
        checkpoint = C.Page(
            url=self.official_url, status=200,
            title="Vercel Security Checkpoint", text="Verifying you are human",
        )
        rendered = C.RenderedPage(
            html="<html><title>Página não encontrada</title><body>Erro 404</body></html>",
            text="Página não encontrada - erro 404",
            title="Página não encontrada",
            final_url=self.official_url,
        )

        page, renderer = self._fetch(checkpoint, rendered)

        renderer.assert_called_once_with(self.official_url)
        self.assertIs(page, checkpoint)

        unavailable = Mock(side_effect=ImportError("playwright unavailable"))
        with patch.object(C, "fetch_page", return_value=checkpoint):
            safe_page = C.fetch_page_with_official_fallback(
                object(), self.official_url, "Barros Cassal", self.official_url,
                render_page=unavailable,
            )
        unavailable.assert_called_once_with(self.official_url)
        self.assertIs(safe_page, checkpoint)

    def test_wrapper_third_party_checkpoint_never_renders(self):
        third_party = C.Page(
            url="https://barroscassal.example.com/", status=200,
            title="Vercel Security Checkpoint", text="Verifying you are human",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with patch.object(C, "fetch_page", return_value=third_party):
            page = C.fetch_page_with_official_fallback(
                object(), third_party.url, "Barros Cassal", self.official_url,
                render_page=renderer,
            )

        self.assertIs(page, third_party)
        renderer.assert_not_called()

    def test_wrapper_other_municipality_official_host_never_renders(self):
        other = C.Page(
            url="https://www.soledade.rs.gov.br/", status=200,
            title="Vercel Security Checkpoint", text="Verifying you are human",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with patch.object(C, "fetch_page", return_value=other):
            page = C.fetch_page_with_official_fallback(
                object(), other.url, "Barros Cassal", other.url,
                render_page=renderer,
            )

        self.assertIs(page, other)
        renderer.assert_not_called()

    def test_wrapper_normal_valid_page_never_renders(self):
        normal = C.Page(
            url=self.official_url, status=200,
            title="Município de Barros Cassal",
            text="Prefeitura Municipal de Barros Cassal",
        )
        renderer = Mock(side_effect=AssertionError("renderer must not run"))
        with patch.object(C, "fetch_page", return_value=normal):
            page = C.fetch_page_with_official_fallback(
                object(), normal.url, "Barros Cassal", self.official_url,
                render_page=renderer,
            )

        self.assertIs(page, normal)
        renderer.assert_not_called()

    def test_tier0_integration_recovers_and_routes_official_index(self):
        index_url = self.official_url + "portal-da-transparencia/concursos-publicos"
        checkpoint = C.Page(
            url=self.official_url, status=200,
            title="Vercel Security Checkpoint", text="Verifying you are human",
        )
        renderer = Mock(return_value=C.RenderedPage(
            html=f"""<html><head><title>Município de Barros Cassal</title></head>
                <body>Prefeitura Municipal de Barros Cassal
                <a href='{index_url}'>Concursos Públicos</a></body></html>""",
            text="Prefeitura Municipal de Barros Cassal - Concursos Públicos",
            title="Município de Barros Cassal",
            final_url=self.official_url,
        ))
        index_page = C.Page(
            url=index_url, status=200, title="Concursos Públicos",
            text="Concurso Público Edital 01/2026 Edital 02/2025",
        )
        picked = {
            "url_concursos": index_url,
            "url_processos_seletivos": "",
            "decision_concursos": "indice_oficial",
            "decision_processos": "nao_encontrado",
            "classification_complete": True,
            "razao": "listagem oficial",
        }
        with (
            patch.object(C, "domain_candidates", return_value=[self.official_url]),
            patch.object(C, "fetch_page", side_effect=[checkpoint, index_page]),
            patch.object(C, "gemini_api_key", return_value="offline-key"),
            patch.object(C, "tier3_classify_and_pick", return_value=picked),
            patch.object(C, "_probe_known_index_paths", return_value=[]),
            patch.object(C, "tier2_grounded_search", return_value=[]),
            patch.object(C, "tier2_directed_bucket_search", return_value=[]),
        ):
            result = C.process_municipio(
                object(), "Barros Cassal", "gemini-2.5-flash",
                use_playwright=False, render_page=renderer,
            )

        renderer.assert_called_once_with(self.official_url)
        self.assertEqual(result.site_base, self.official_url.rstrip("/"))
        self.assertEqual(result.url_concursos, index_url)
        self.assertEqual(picked["decision_concursos"], "indice_oficial")


if __name__ == "__main__":
    unittest.main()
