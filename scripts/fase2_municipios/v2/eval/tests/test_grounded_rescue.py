"""Offline contract tests for grounded_rescue (stdlib unittest; no sockets)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.fase2_municipios.v2.eval.grounded_rescue import (
    FALLBACK_MODEL,
    PAID_AUTHORIZATION,
    REQUIRED_MODEL,
    DailyQuotaExhausted,
    GeminiGroundedClient,
    GroundedAnswer,
    InterruptionState,
    PolicyFailure,
    PreventiveQuotaStop,
    QuotaGovernor,
    Target,
    _clean_url,
    _new_redirect_session,
    _parser,
    build_queries,
    dispatch_f3_adapter,
    extract_answer_url_sources,
    load_grounded_credentials,
    main,
    micro_acquire_unit,
    read_targets,
    read_url_map,
    rebuild_summary,
    run_micro_acquisitions,
    run_rescue,
    write_outputs,
)
from scripts.fase2_municipios.v2.eval.platform_probe_runner import FetchResult


class FakeFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, timeout: int) -> FetchResult:
        self.urls.append(url)
        return FetchResult(
            status_code=200,
            html=(
                "<html><h1>Prefeitura de Camaqua</h1>"
                "<li>Concurso Publico Edital 01/2025</li>"
                "<li>Processo Seletivo Edital 02/2026</li></html>"
            ),
            final_url=url,
        )


class MappingFetcher:
    def __init__(self, outcomes: dict[str, FetchResult | BaseException]) -> None:
        self.outcomes = outcomes
        self.urls: list[str] = []

    def get(self, url: str, timeout: int) -> FetchResult:
        self.urls.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGroundedClient:
    def __init__(self, answers: list[GroundedAnswer]) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self.telemetry = {"providers": {"gemini_free_1": {"calls": 0, "errors": 0, "responses": 0}}}

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        self.calls.append(query)
        self.telemetry["providers"]["gemini_free_1"]["calls"] += 1
        self.telemetry["providers"]["gemini_free_1"]["responses"] += 1
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


class FakeRedirectResponse:
    def __init__(self, status_code: int, location: str = "") -> None:
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}

    def close(self) -> None:
        return None


class FakeRedirectSession:
    def __init__(self, outcomes: dict[str, FakeRedirectResponse | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, int, bool]] = []
        self.headers = {
            "User-Agent": "generic-resolver",
            "Accept": "text/html",
        }

    def get(self, url: str, *, timeout: int, allow_redirects: bool) -> FakeRedirectResponse:
        self.calls.append((url, timeout, allow_redirects))
        outcome = self.outcomes[url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


def public_host_resolver(_host: str) -> tuple[str, ...]:
    return ("8.8.8.8",)


class HttpError(RuntimeError):
    def __init__(
        self, status: int, message: str, *, pro_rejected: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status, headers=headers or {})
        self.pro_rejected = pro_rejected


class Multi24AcquisitionTests(unittest.TestCase):
    ENTRY_URL = (
        "https://sistemas.progresso.rs.gov.br/multi24/sistemas/transparencia/index"
        "?entidade=1&secao=dinamico&id=6146"
    )
    CHILD_URL = (
        "https://sistemas.progresso.rs.gov.br/multi24/sistemas/transparencia/index"
        "?entidade=1&secao=dinamico&id=11730"
    )
    OFFICIAL_URL = "https://progresso.rs.gov.br"
    TRANSPARENCIA_URL = f"{OFFICIAL_URL}/transparencia"
    INVENTED_URL = (
        "https://sistemas.progresso.rs.gov.br/multi24/sistemas/transparencia/index"
        "?entidade=1&secao=dinamico&id=999999"
    )
    FIXTURES = Path(__file__).parent / "fixtures" / "f3_multi24"

    @classmethod
    def _fixture(cls, name: str) -> str:
        return (cls.FIXTURES / name).read_text(encoding="utf-8")

    @classmethod
    def _official_html(cls, *, linked: bool = True) -> str:
        anchor = (
            f'<a href="{cls.ENTRY_URL.replace("&", "&amp;")}">'
            "Portal da Transparencia</a>"
            if linked else "<p>Sem enlace para o portal</p>"
        )
        return (
            "<html><head><title>Municipio de Progresso</title></head>"
            "<body><header><p>Municipio de Progresso</p></header>"
            f"{anchor}</body></html>"
        )

    @classmethod
    def _fetcher(cls, *, linked: bool = True) -> MappingFetcher:
        return MappingFetcher({
            cls.ENTRY_URL: FetchResult(
                200, cls._fixture("progresso_tree.html"), cls.ENTRY_URL
            ),
            cls.OFFICIAL_URL: FetchResult(
                200, cls._official_html(linked=linked), cls.OFFICIAL_URL
            ),
            cls.CHILD_URL: FetchResult(
                200, cls._fixture("progresso_2026.html"), cls.CHILD_URL
            ),
            cls.INVENTED_URL: FetchResult(
                200, "<html>inventada</html>", cls.INVENTED_URL
            ),
        })

    def _acquire(
        self,
        directory: str,
        fetcher: MappingFetcher,
        *,
        cache: dict | None = None,
        adapter_dispatcher=dispatch_f3_adapter,
    ) -> dict:
        with patch(
            "scripts.fase2_municipios.v2.eval.grounded_rescue._municipality_site_base",
            return_value=self.OFFICIAL_URL,
        ):
            return micro_acquire_unit(
                Target("progresso", "concurso_publico", self.ENTRY_URL, "render_incierto"),
                self.ENTRY_URL,
                output_dir=Path(directory),
                fetcher=fetcher,
                timestamp_run="2026-07-13T12:00:00+00:00",
                renderer=lambda _url: None,
                adapter_dispatcher=adapter_dispatcher,
                multi24_authority_cache=cache,
            )

    def test_multi24_builds_authority_from_real_official_href(self) -> None:
        fetcher = self._fetcher()
        with tempfile.TemporaryDirectory() as directory:
            payload = self._acquire(directory, fetcher)
        adapter = payload["adaptador"]
        self.assertEqual("context_acquired", adapter["acquisition_provenance"]["result"])
        self.assertEqual(self.ENTRY_URL, adapter["acquisition_provenance"]["official_href"])
        self.assertEqual(
            [self.OFFICIAL_URL, self.ENTRY_URL],
            adapter["acquisition_provenance"]["official_navigation_chain"],
        )
        self.assertNotIn(self.TRANSPARENCIA_URL, fetcher.urls)
        self.assertTrue(adapter["candidates"])

    def test_multi24_depth_one_authority_is_cached_with_its_subpage_snapshot(self) -> None:
        homepage = (
            "<html><body><h1>Municipio de Progresso</h1>"
            f'<a href="{self.TRANSPARENCIA_URL}">Acesso a informacao</a>'
            "</body></html>"
        )
        subpage = (
            "<html><body><h1>Municipio de Progresso</h1>"
            f'<a href="{self.ENTRY_URL.replace("&", "&amp;")}">Portal</a>'
            "</body></html>"
        )
        fetcher = self._fetcher(linked=False)
        fetcher.outcomes[self.OFFICIAL_URL] = FetchResult(200, homepage, self.OFFICIAL_URL)
        fetcher.outcomes[self.TRANSPARENCIA_URL] = FetchResult(
            200, subpage, self.TRANSPARENCIA_URL
        )
        cache: dict = {}
        with tempfile.TemporaryDirectory() as directory:
            first = self._acquire(directory, fetcher, cache=cache)
            second = self._acquire(directory, fetcher, cache=cache)

        self.assertEqual(1, fetcher.urls.count(self.OFFICIAL_URL))
        self.assertEqual(1, fetcher.urls.count(self.TRANSPARENCIA_URL))
        self.assertFalse(first["adaptador"]["acquisition_provenance"]["official_cache_hit"])
        self.assertTrue(second["adaptador"]["acquisition_provenance"]["official_cache_hit"])
        self.assertEqual(
            self.TRANSPARENCIA_URL,
            second["adaptador"]["acquisition_provenance"]["official_source_url"],
        )

    def test_multi24_without_official_href_remains_dispatch_refusal(self) -> None:
        fetcher = self._fetcher(linked=False)
        with tempfile.TemporaryDirectory() as directory:
            payload = self._acquire(directory, fetcher)
        adapter = payload["adaptador"]
        self.assertEqual([], adapter["candidates"])
        self.assertEqual(
            "multi24_authority_or_linked_pages_missing",
            adapter["refusal_reason"],
        )
        self.assertEqual(
            "official_portal_href_missing",
            adapter["acquisition_provenance"]["result"],
        )

    def test_multi24_follows_official_transparency_subpage_for_authority(self) -> None:
        homepage = (
            "<html><head><title>Municipio de Progresso</title></head><body>"
            '<a href="/transparencia">Acesso a Transparencia</a>'
            "</body></html>"
        )
        subpage = (
            "<html><head><title>Municipio de Progresso - Transparencia</title></head>"
            "<body><p>Municipio de Progresso</p>"
            f'<a href="{self.ENTRY_URL.replace("&", "&amp;")}">'
            "Portal da Transparencia</a></body></html>"
        )
        fetcher = self._fetcher(linked=False)
        fetcher.outcomes[self.OFFICIAL_URL] = FetchResult(200, homepage, self.OFFICIAL_URL)
        fetcher.outcomes[self.TRANSPARENCIA_URL] = FetchResult(
            200, subpage, self.TRANSPARENCIA_URL
        )
        captured: dict = {}

        def capturing_dispatcher(**kwargs):
            captured.update(kwargs["context"])
            return dispatch_f3_adapter(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            payload = self._acquire(
                directory,
                fetcher,
                adapter_dispatcher=capturing_dispatcher,
            )

        provenance = payload["adaptador"]["acquisition_provenance"]
        authority = captured["multi24_authority"]
        self.assertEqual("context_acquired", provenance["result"])
        self.assertEqual(self.TRANSPARENCIA_URL, provenance["official_source_url"])
        self.assertEqual(
            [self.OFFICIAL_URL, self.TRANSPARENCIA_URL, self.ENTRY_URL],
            provenance["official_navigation_chain"],
        )
        self.assertEqual(
            self.TRANSPARENCIA_URL,
            authority.navigation_snapshots[0].final_url,
        )

    def test_multi24_depth_one_is_same_host_fail_closed_and_capped_at_five(self) -> None:
        internal = [f"{self.OFFICIAL_URL}/servicos/{index}" for index in range(1, 8)]
        external = "https://example.invalid/portal-transparencia"
        anchors = "".join(
            f'<a href="{url}">Portal de Servicos {index}</a>'
            for index, url in enumerate(internal, start=1)
        )
        homepage = (
            "<html><body><p>Municipio de Progresso</p>"
            f'{anchors}<a href="{external}">Portal da Transparencia externo</a>'
            "</body></html>"
        )
        fetcher = self._fetcher(linked=False)
        fetcher.outcomes[self.OFFICIAL_URL] = FetchResult(200, homepage, self.OFFICIAL_URL)
        for url in internal[:5]:
            fetcher.outcomes[url] = FetchResult(
                200,
                "<html><body><p>Municipio de Progresso</p><p>Sem portal</p></body></html>",
                url,
            )

        with tempfile.TemporaryDirectory() as directory:
            payload = self._acquire(directory, fetcher)

        adapter = payload["adaptador"]
        provenance = adapter["acquisition_provenance"]
        self.assertEqual([], adapter["candidates"])
        self.assertEqual("official_portal_href_missing", provenance["result"])
        self.assertEqual(internal[:5], provenance["official_subpages_reviewed"])
        self.assertEqual(5, len(provenance["official_navigation_attempts"]))
        self.assertNotIn(external, fetcher.urls)
        self.assertTrue(
            all(
                url == self.ENTRY_URL or url.startswith(self.OFFICIAL_URL)
                for url in fetcher.urls
            )
        )

    def test_multi24_fetches_children_only_from_adapter_real_edges(self) -> None:
        fetcher = self._fetcher()
        with tempfile.TemporaryDirectory() as directory:
            payload = self._acquire(directory, fetcher)
        provenance = payload["adaptador"]["acquisition_provenance"]
        self.assertEqual([self.CHILD_URL], provenance["linked_pages_fetched"])
        self.assertIn(self.CHILD_URL, fetcher.urls)
        self.assertNotIn(self.INVENTED_URL, fetcher.urls)

    def test_multi24_official_authority_fetch_is_cached_by_municipality(self) -> None:
        fetcher = self._fetcher()
        cache: dict = {}
        with tempfile.TemporaryDirectory() as directory:
            first = self._acquire(directory, fetcher, cache=cache)
            second = self._acquire(directory, fetcher, cache=cache)
        self.assertEqual(1, fetcher.urls.count(self.OFFICIAL_URL))
        self.assertFalse(first["adaptador"]["acquisition_provenance"]["official_cache_hit"])
        self.assertTrue(second["adaptador"]["acquisition_provenance"]["official_cache_hit"])

    def test_multi24_acquisition_never_confirms_a_candidate(self) -> None:
        fetcher = self._fetcher()
        with tempfile.TemporaryDirectory() as directory:
            payload = self._acquire(directory, fetcher)
        self.assertTrue(payload["adaptador"]["candidates"])
        self.assertTrue(
            all(candidate["confirmed"] is False for candidate in payload["adaptador"]["candidates"])
        )
        self.assertFalse(payload["veredicto_gate"]["pasa"])


def sdk_response(url: str = "https://camaqua.rs.gov.br/concursos") -> SimpleNamespace:
    metadata = {
        "grounding_chunks": [{"web": {"uri": url, "title": "Prefeitura"}}],
        "grounding_supports": [{"segment": {"text": "índice oficial"}}],
    }
    return SimpleNamespace(text=url, candidates=[{"grounding_metadata": metadata}])


class SequencedModels:
    def __init__(self, owner: "SequencedClient") -> None:
        self.owner = owner

    def generate_content(self, *, model, contents, config):
        self.owner.log.append((self.owner.label, model, config))
        outcome = self.owner.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SequencedClient:
    def __init__(self, label: str, outcomes: list[object], log: list[tuple]) -> None:
        self.label = label
        self.outcomes = outcomes
        self.log = log
        self.models = SequencedModels(self)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class GroundedRescueTests(unittest.TestCase):
    REDIRECT_URL = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-123"
    )
    OFFICIAL_URL = "https://camaqua.rs.gov.br/concursos"

    @staticmethod
    def _fetch_result(url: str) -> FetchResult:
        return FetchResult(
            status_code=200,
            html=(
                "<html><h1>Prefeitura de Camaqua</h1>"
                "<li>Concurso Publico Edital 01/2025</li>"
                "<li>Processo Seletivo Edital 02/2026</li></html>"
            ),
            final_url=url,
        )

    def test_grounding_redirect_resolves_and_official_final_passes_filter(self) -> None:
        fetcher = MappingFetcher({
            self.OFFICIAL_URL: self._fetch_result(self.OFFICIAL_URL),
        })
        redirect_session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, self.OFFICIAL_URL),
            self.OFFICIAL_URL: FakeRedirectResponse(200),
        })
        rows, _summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(
                text="",
                grounding_urls=(self.REDIRECT_URL,),
            )]),
            fetcher=fetcher,
            redirect_session_factory=lambda: redirect_session,
            redirect_host_resolver=public_host_resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(self.OFFICIAL_URL, rows[0].url_candidata)
        self.assertEqual("grounding", rows[0].fuente)
        self.assertEqual(self.REDIRECT_URL, rows[0].redirector_original)

    def test_grounding_redirect_to_non_official_host_is_discarded(self) -> None:
        non_official = "https://example.com/concursos"
        fetcher = MappingFetcher({})
        redirect_session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, non_official),
            non_official: FakeRedirectResponse(200),
        })
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(
                text="",
                grounding_urls=(self.REDIRECT_URL,),
            )]),
            fetcher=fetcher,
            redirect_session_factory=lambda: redirect_session,
            redirect_host_resolver=public_host_resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual([], rows)
        discarded = summary["unidades"]["camaqua/concurso_publico"]["descartadas"]
        self.assertEqual("host_no_oficial", discarded[0]["razon"])
        self.assertEqual(self.REDIRECT_URL, discarded[0]["redirector_original"])
        self.assertEqual([], fetcher.urls)

    def test_unresolvable_grounding_redirect_is_discarded_and_unit_continues(self) -> None:
        fetcher = MappingFetcher({
            self.OFFICIAL_URL: self._fetch_result(self.OFFICIAL_URL),
        })
        redirect_session = FakeRedirectSession({
            self.REDIRECT_URL: TimeoutError("simulated timeout"),
        })
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(
                text=f"Alternativa: {self.OFFICIAL_URL}",
                grounding_urls=(self.REDIRECT_URL,),
            )]),
            fetcher=fetcher,
            redirect_session_factory=lambda: redirect_session,
            redirect_host_resolver=public_host_resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        discarded = summary["unidades"]["camaqua/concurso_publico"]["descartadas"]
        self.assertEqual("redirect_no_resuelto", discarded[0]["razon"])
        self.assertEqual("completed", summary["unidades"]["camaqua/concurso_publico"]["estado"])
        self.assertEqual(1, len(rows))
        self.assertEqual("texto_modelo", rows[0].fuente)

    def test_model_text_url_is_extracted_with_text_source(self) -> None:
        rows, _summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(
                text=f"Pagina oficial: {self.OFFICIAL_URL}.",
            )]),
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(self.OFFICIAL_URL, rows[0].url_candidata)
        self.assertEqual("texto_modelo", rows[0].fuente)
        self.assertEqual("", rows[0].redirector_original)

    def test_deduplicates_grounding_and_text_after_redirect_resolution(self) -> None:
        fetcher = MappingFetcher({
            self.OFFICIAL_URL: self._fetch_result(self.OFFICIAL_URL),
        })
        redirect_session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, self.OFFICIAL_URL),
            self.OFFICIAL_URL: FakeRedirectResponse(200),
        })
        rows, _summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(
                text=self.OFFICIAL_URL,
                grounding_urls=(self.REDIRECT_URL,),
            )]),
            fetcher=fetcher,
            redirect_session_factory=lambda: redirect_session,
            redirect_host_resolver=public_host_resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("grounding", rows[0].fuente)
        self.assertEqual([self.OFFICIAL_URL], fetcher.urls)

    def test_grounding_redirect_resolution_is_cached_across_searches(self) -> None:
        fetcher = MappingFetcher({
            self.OFFICIAL_URL: self._fetch_result(self.OFFICIAL_URL),
        })
        redirect_session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, self.OFFICIAL_URL),
            self.OFFICIAL_URL: FakeRedirectResponse(200),
        })
        answer = GroundedAnswer(text="", grounding_urls=(self.REDIRECT_URL,))
        rows, _summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([answer, answer]),
            fetcher=fetcher,
            redirect_session_factory=lambda: redirect_session,
            redirect_host_resolver=public_host_resolver,
            max_searches=2,
            sleep_seconds=0,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(1, sum(call[0] == self.REDIRECT_URL for call in redirect_session.calls))

    def test_capacity_vetoed_combination_is_called_once_and_other_provider_works(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE1": SequencedClient(
                "free1",
                [HttpError(404, f"{REQUIRED_MODEL} model not found", pro_rejected=True)],
                log,
            ),
            "FREE2": SequencedClient("free2", [sdk_response(), sdk_response()], log),
            "PAID": SequencedClient("paid", [], log),
        }
        client = GeminiGroundedClient(
            {
                "GEMINI_API_KEY_FREE": "FREE1",
                "GEMINI_API_KEY_FREE_2": "FREE2",
                "GEMINI_API_KEY": "PAID",
            },
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            sleep=lambda _: None,
        )
        _rows, summary = run_rescue(
            [
                Target("camaqua", "concurso_publico", "pista"),
                Target("camaqua", "processo_seletivo", "pista"),
            ],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual(1, sum(item[0] == "free1" for item in log))
        self.assertEqual(2, sum(item[0] == "free2" for item in log))
        self.assertEqual(1, client.telemetry["providers"]["gemini_free_1"]["errors"])
        self.assertIn(
            ("gemini_free_1", REQUIRED_MODEL, "model_unavailable_for_provider"),
            summary["global"]["capacidad_vetada"],
        )

    def test_capacity_veto_for_free2_does_not_block_paid_provider(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE1": SequencedClient(
                "free1", [HttpError(429, "quota"), HttpError(429, "quota")], log
            ),
            "FREE2": SequencedClient(
                "free2",
                [HttpError(404, f"{REQUIRED_MODEL} model not found", pro_rejected=True)],
                log,
            ),
            "PAID": SequencedClient("paid", [sdk_response(), sdk_response()], log),
        }
        client = GeminiGroundedClient(
            {
                "GEMINI_API_KEY_FREE": "FREE1",
                "GEMINI_API_KEY_FREE_2": "FREE2",
                "GEMINI_API_KEY": "PAID",
            },
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            sleep=lambda _: None,
        )
        client.search("q1", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        client.search("q2", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual(1, sum(item[0] == "free2" for item in log))
        self.assertEqual(2, sum(item[0] == "paid" for item in log))
        self.assertEqual(1, client.telemetry["providers"]["gemini_free_2"]["errors"])

    def test_redirect_with_http_hop_is_rejected(self) -> None:
        session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, "http://camaqua.rs.gov.br/concursos"),
        })
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(text="", grounding_urls=(self.REDIRECT_URL,))]),
            fetcher=FakeFetcher(),
            redirect_session_factory=lambda: session,
            redirect_host_resolver=public_host_resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual([], rows)
        self.assertEqual(
            "redirect_no_resuelto",
            summary["unidades"]["camaqua/concurso_publico"]["descartadas"][0]["razon"],
        )

    def test_redirect_chain_longer_than_three_redirects_is_rejected(self) -> None:
        hops = [f"https://redirect.example/hop-{index}" for index in range(1, 5)]
        session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, hops[0]),
            hops[0]: FakeRedirectResponse(302, hops[1]),
            hops[1]: FakeRedirectResponse(302, hops[2]),
            hops[2]: FakeRedirectResponse(302, hops[3]),
        })
        rows, _summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(text="", grounding_urls=(self.REDIRECT_URL,))]),
            fetcher=FakeFetcher(),
            redirect_session_factory=lambda: session,
            redirect_host_resolver=public_host_resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual([], rows)
        self.assertEqual(3, len(session.calls))
        self.assertTrue(all(timeout <= 10 and not follow for _, timeout, follow in session.calls))

    def test_private_metadata_and_local_redirect_targets_are_rejected_at_every_hop(self) -> None:
        blocked = (
            "https://localhost/resource",
            "https://127.0.0.2/resource",
            "https://10.1.2.3/resource",
            "https://172.16.1.2/resource",
            "https://192.168.1.2/resource",
            "https://169.254.169.254/latest/meta-data",
            "https://metadata.google.internal/computeMetadata/v1",
            "https://[fe80::1]/resource",
        )
        for destination in blocked:
            with self.subTest(destination=destination, position="final"):
                final_session = FakeRedirectSession({
                    self.REDIRECT_URL: FakeRedirectResponse(302, destination),
                })
                rows, _ = run_rescue(
                    [Target("camaqua", "concurso_publico", "pista")],
                    client=FakeGroundedClient([GroundedAnswer(text="", grounding_urls=(self.REDIRECT_URL,))]),
                    fetcher=FakeFetcher(),
                    redirect_session_factory=lambda: final_session,
                    redirect_host_resolver=public_host_resolver,
                    max_searches=1,
                    sleep_seconds=0,
                )
                self.assertEqual([], rows)
                self.assertEqual(1, len(final_session.calls))
            with self.subTest(destination=destination, position="intermediate"):
                safe_hop = "https://redirect.example/intermediate"
                intermediate_session = FakeRedirectSession({
                    self.REDIRECT_URL: FakeRedirectResponse(302, safe_hop),
                    safe_hop: FakeRedirectResponse(302, destination),
                })
                rows, _ = run_rescue(
                    [Target("camaqua", "concurso_publico", "pista")],
                    client=FakeGroundedClient([GroundedAnswer(text="", grounding_urls=(self.REDIRECT_URL,))]),
                    fetcher=FakeFetcher(),
                    redirect_session_factory=lambda: intermediate_session,
                    redirect_host_resolver=public_host_resolver,
                    max_searches=1,
                    sleep_seconds=0,
                )
                self.assertEqual([], rows)
                self.assertEqual(2, len(intermediate_session.calls))

    def test_redirect_session_has_only_non_sensitive_headers(self) -> None:
        session = _new_redirect_session()
        try:
            header_names = {name.casefold() for name in session.headers}
            self.assertEqual({"user-agent", "accept"}, header_names)
            self.assertNotIn("authorization", header_names)
            self.assertFalse(any("api" in name or "cookie" in name for name in header_names))
            self.assertIsNone(session.auth)
            self.assertFalse(session.trust_env)
        finally:
            session.close()

    def test_redirect_hostname_resolving_to_private_ip_is_rejected(self) -> None:
        internal = "https://internal.example/resource"
        session = FakeRedirectSession({
            self.REDIRECT_URL: FakeRedirectResponse(302, internal),
        })

        def resolver(host: str) -> tuple[str, ...]:
            return ("10.0.0.8",) if host == "internal.example" else ("8.8.8.8",)

        rows, _ = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(text="", grounding_urls=(self.REDIRECT_URL,))]),
            fetcher=FakeFetcher(),
            redirect_session_factory=lambda: session,
            redirect_host_resolver=resolver,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual([], rows)
        self.assertEqual(1, len(session.calls))

    def test_text_url_normalizes_dangling_parenthesis_and_html_entity(self) -> None:
        answer = GroundedAnswer(
            text="Veja (https://camaqua.rs.gov.br/concursos?ano=2026&amp;tipo=aberto)."
        )
        self.assertEqual(
            [("https://camaqua.rs.gov.br/concursos?ano=2026&tipo=aberto", "texto_modelo")],
            extract_answer_url_sources(answer),
        )

    def test_text_only_url_is_never_a_grounding_citation(self) -> None:
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=FakeGroundedClient([GroundedAnswer(text=f"Pagina: {self.OFFICIAL_URL}.")]),
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("texto_modelo", rows[0].fuente)
        self.assertEqual("", rows[0].snippet_grounding)
        query = summary["unidades"]["camaqua/concurso_publico"]["queries"][0]
        self.assertEqual([], query["grounding_urls"])

    def test_completed_unit_is_atomically_persisted_with_required_fields(self) -> None:
        client = FakeGroundedClient([GroundedAnswer(
            text="https://camaqua.rs.gov.br/concursos",
            grounding_urls=("https://camaqua.rs.gov.br/concursos",),
            grounding_snippets=("Concurso Publico 01/2025",),
            provider="gemini_free_1",
        )])
        target = Target("camaqua", "concurso_publico", "pista")
        import scripts.fase2_municipios.v2.eval.grounded_rescue as module

        real_replace = module.os.replace
        with tempfile.TemporaryDirectory() as directory, patch.object(
            module.os, "replace", side_effect=real_replace
        ) as replace:
            output = Path(directory)
            run_rescue(
                [target],
                client=client,
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
            )
            unit_path = output / "unidad_camaqua_concurso_publico.json"
            payload = json.loads(unit_path.read_text(encoding="utf-8"))
            orphan_tmp = list(output.glob("*.tmp"))

        self.assertTrue(any(
            Path(call.args[0]).name.endswith(".json.tmp")
            and Path(call.args[1]).name == "unidad_camaqua_concurso_publico.json"
            for call in replace.call_args_list
        ))
        self.assertEqual({
            "schema_version", "municipio", "bucket", "sub_causa", "pista",
            "grounded", "telemetria", "microadquisicion", "estado", "causa",
            "paid_cap_alcanzado_en_unidad",
            "timestamp",
        }, set(payload))
        self.assertEqual("completed", payload["estado"])
        self.assertIsNone(payload["causa"])
        self.assertTrue(payload["grounded"]["queries"])
        self.assertTrue(payload["grounded"]["candidatas"])
        self.assertIn("grounding_snippets", payload["grounded"]["queries"][0])
        self.assertEqual([], orphan_tmp)

    def test_resume_skips_completed_and_retries_failed_units(self) -> None:
        targets = [
            Target("camaqua", "concurso_publico", "pista"),
            Target("camaqua", "processo_seletivo", "pista"),
        ]
        answer = GroundedAnswer(
            text="https://camaqua.rs.gov.br/concursos",
            provider="gemini_free_1",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run_rescue(
                targets,
                client=FakeGroundedClient([answer]),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
            )
            failed_path = output / "unidad_camaqua_processo_seletivo.json"
            failed = json.loads(failed_path.read_text(encoding="utf-8"))
            failed["estado"] = "failed"
            failed["causa"] = "simulated_failure"
            failed_path.write_text(json.dumps(failed), encoding="utf-8")

            resumed_client = FakeGroundedClient([answer])
            _, summary = run_rescue(
                targets,
                client=resumed_client,
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
                resume=True,
            )

            retried = json.loads(failed_path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(resumed_client.calls))
        self.assertEqual(1, summary["global"]["skipped_existing"])
        self.assertEqual("completed", retried["estado"])

    def test_summary_is_rebuilt_only_from_unit_files_on_disk(self) -> None:
        answer = GroundedAnswer(
            text="https://camaqua.rs.gov.br/concursos",
            provider="gemini_free_1",
        )
        client = FakeGroundedClient([answer])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run_rescue(
                [Target("camaqua", "concurso_publico", "pista")],
                client=client,
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
            )
            client.telemetry["providers"]["gemini_free_1"]["calls"] = 999
            summary = rebuild_summary(output)
        self.assertEqual(1, summary["global"]["calls_by_provider"]["gemini_free_1"])
        self.assertEqual(1, summary["global"]["unidades"])
        self.assertEqual(0, summary["global"]["paid_calls"])

    def test_simulated_interruption_persists_failed_unit_without_orphan_tmp(self) -> None:
        interruption = InterruptionState(requested=True, signal_name="SIGTERM")
        client = FakeGroundedClient([GroundedAnswer(text="")])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            _, summary = run_rescue(
                [Target("camaqua", "concurso_publico", "pista")],
                client=client,
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
                interruption=interruption,
            )
            payload = json.loads(
                (output / "unidad_camaqua_concurso_publico.json").read_text(encoding="utf-8")
            )
            orphan_tmp = list(output.glob("*.tmp"))
        self.assertEqual("failed", payload["estado"])
        self.assertEqual("interrupted", payload["causa"])
        self.assertEqual(1, summary["global"]["failed"])
        self.assertEqual([], orphan_tmp)

    def test_unit_telemetry_contains_every_provider_and_fallback_cause(self) -> None:
        answer = GroundedAnswer(
            text="",
            provider="gemini_free_1",
            fallbacks=({
                "from_provider": "gemini_free_1",
                "to_provider": "gemini_free_2",
                "cause": "quota_429",
            },),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run_rescue(
                [Target("camaqua", "concurso_publico", "pista")],
                client=FakeGroundedClient([answer]),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
            )
            payload = json.loads(
                (output / "unidad_camaqua_concurso_publico.json").read_text(encoding="utf-8")
            )
        self.assertEqual(
            {"gemini_free_1", "gemini_free_2", "gemini_paid"},
            set(payload["telemetria"]["providers"]),
        )
        self.assertEqual(1, payload["telemetria"]["providers"]["gemini_free_1"]["calls"])
        self.assertEqual(0, payload["telemetria"]["paid_calls"])
        self.assertEqual("quota_429", payload["telemetria"]["fallbacks"][0]["cause"])

    def test_correction31_csv_parses_31_targets_with_sub_causa(self) -> None:
        repo = Path(__file__).resolve().parents[5]
        path = repo / "staging/fase2_v2/eval/misiones_20260713/rescate_targets.csv"
        targets = read_targets(path)
        self.assertEqual(31, len(targets))
        self.assertEqual(
            ["municipio", "bucket", "sub_causa", "pista"],
            path.read_text(encoding="utf-8-sig").splitlines()[0].split(","),
        )
        self.assertEqual(
            {"url_mala": 19, "render_incierto": 10, "dificil_rederivado": 2},
            {cause: sum(target.sub_causa == cause for target in targets) for cause in {
                "url_mala", "render_incierto", "dificil_rederivado"
            }},
        )
        self.assertTrue(all(target.sub_causa for target in targets))
        self.assertEqual({
            ("camaqua", "processo_seletivo"),
            ("fortalezadosvalos", "concurso_publico"),
            ("gramadoxavier", "concurso_publico"),
            ("imbe", "concurso_publico"),
            ("imbe", "processo_seletivo"),
            ("lagoabonitadosul", "processo_seletivo"),
            ("saovendelino", "concurso_publico"),
            ("seberi", "concurso_publico"),
            ("senadorsalgadofilho", "concurso_publico"),
            ("senadorsalgadofilho", "processo_seletivo"),
        }, {
            (target.municipio, target.bucket)
            for target in targets if target.sub_causa == "render_incierto"
        })
        self.assertEqual({
            ("gentil", "concurso_publico"),
            ("sobradinho", "concurso_publico"),
        }, {
            (target.municipio, target.bucket)
            for target in targets if target.sub_causa == "dificil_rederivado"
        })

    def test_render_strategy_queries_follow_required_order(self) -> None:
        queries = build_queries(Target(
            "vistaalegre",
            "concurso_publico",
            "endpoint documentado /site/busca_editais",
            "render_incierto",
        ))
        expected = (
            "superfície oficial estática alternativa",
            "endpoint XHR AJAX",
            "URL final da listagem oficial",
            "documento ou índice oficial ligado",
            "parâmetros reproduzíveis de filtro e paginação",
        )
        self.assertEqual(5, len(queries))
        self.assertEqual(expected, tuple(marker for marker, query in zip(expected, queries) if marker in query))
        self.assertEqual(list(range(5)), [
            next(index for index, query in enumerate(queries) if marker in query)
            for marker in expected
        ])

    def test_micro_acquire_writes_all_required_durable_fields(self) -> None:
        rendered = SimpleNamespace(
            final_url="https://camaqua.atende.net/cidadao/pagina/concursos",
            text="Prefeitura de Camaquã\nConcurso Público 01/2025",
            status=200,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            payload = micro_acquire_unit(
                Target("camaqua", "concurso_publico", "shell", "render_incierto"),
                "https://camaqua.atende.net/cidadao/pagina/concursos",
                output_dir=output,
                fetcher=FakeFetcher(),
                timestamp_run="2026-07-13T12:00:00+00:00",
                renderer=lambda _: rendered,
            )
            path = output / "micro_camaqua_concurso_publico.json"
            self.assertTrue(path.is_file())
            durable = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload, durable)
            self.assertEqual({
                "url_inicial", "url_final", "trigger",
                "snapshot_recortado", "citas_candidatas",
                "veredicto_gate", "http_status", "timestamp",
                "cadena_redirects", "razon_seleccion_url",
            }, set(durable))
            self.assertTrue(durable["citas_candidatas"])
            self.assertTrue(durable["veredicto_gate"]["pasa"])

    def test_render_without_static_or_micro_evidence_has_no_candidate(self) -> None:
        client = FakeGroundedClient([GroundedAnswer(
            text="https://camaqua.atende.net/cidadao/pagina/processos-seletivos",
            model=REQUIRED_MODEL,
        )])
        target = Target("camaqua", "processo_seletivo", "shell", "render_incierto")
        shell_url = "https://camaqua.atende.net/cidadao/pagina/processos-seletivos"
        shell_fetcher = MappingFetcher({
            shell_url: FetchResult(
                status_code=200,
                html="<html><h1>Prefeitura de Camaqua</h1><p>Portal</p></html>",
                final_url=shell_url,
            ),
        })
        rows, summary = run_rescue(
            [target],
            client=client,
            fetcher=shell_fetcher,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual([], rows)
        with tempfile.TemporaryDirectory() as directory:
            rows = run_micro_acquisitions(
                [target],
                rows,
                summary,
                output_dir=Path(directory),
                fetcher=shell_fetcher,
                timestamp_run="2026-07-13T12:00:00+00:00",
                renderer=lambda _: None,
            )
        self.assertEqual([], rows)
        self.assertFalse(summary["unidades"]["camaqua/processo_seletivo"]["micro_veredicto"]["pasa"])

    def test_micro_acquire_without_url_writes_fail_closed_artifact_and_no_candidate(self) -> None:
        client = FakeGroundedClient([GroundedAnswer(text="", model=REQUIRED_MODEL)])
        target = Target("imbe", "concurso_publico", "snapshot Portal", "render_incierto")
        rows, summary = run_rescue(
            [target],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
        )
        fetcher = FakeFetcher()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            rows = run_micro_acquisitions(
                [target],
                rows,
                summary,
                output_dir=output,
                fetcher=fetcher,
                timestamp_run="2026-07-13T12:00:00+00:00",
                renderer=lambda _: self.fail("renderer must not run without a URL"),
            )
            durable = json.loads(
                (output / "micro_imbe_concurso_publico.json").read_text(encoding="utf-8")
            )
        self.assertEqual([], rows)
        self.assertEqual([], fetcher.urls)
        self.assertEqual("sin_url_candidata_grounded", durable["trigger"])
        self.assertEqual([], durable["citas_candidatas"])
        self.assertFalse(durable["veredicto_gate"]["pasa"])

    def test_respects_max_searches(self) -> None:
        client = FakeGroundedClient([GroundedAnswer(text="")])
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=3,
            sleep_seconds=0,
        )
        self.assertEqual(3, len(client.calls))
        self.assertEqual(3, summary["global"]["busquedas_grounded"])
        self.assertEqual([], rows)

    def test_discards_non_official_host_with_reason_and_without_fetch(self) -> None:
        client = FakeGroundedClient([
            GroundedAnswer(text="https://example.com/concursos", model=REQUIRED_MODEL)
        ])
        fetcher = FakeFetcher()
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=client,
            fetcher=fetcher,
            max_searches=1,
            sleep_seconds=0,
        )
        self.assertEqual([], rows)
        self.assertEqual([], fetcher.urls)
        discarded = summary["unidades"]["camaqua/concurso_publico"]["descartadas"]
        self.assertEqual("host_no_oficial", discarded[0]["razon"])

    def test_free1_model_unavailable_is_vetoed_once_and_model_never_rotates(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient(
                "free",
                [HttpError(404, f"{REQUIRED_MODEL} model not found", pro_rejected=True)],
                log,
            ),
            "FREE2": SequencedClient("free2", [sdk_response(), sdk_response()], log),
        }

        def factory(*, api_key, vertexai):
            self.assertFalse(vertexai)
            return clients[api_key]

        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE", "GEMINI_API_KEY_FREE_2": "FREE2"},
            client_factory=factory,
            free_only=True,
            sleep=lambda _: None,
        )
        answer = client.search("q", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        client.search("q2", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual(REQUIRED_MODEL, FALLBACK_MODEL)
        self.assertEqual(REQUIRED_MODEL, answer.model)
        self.assertEqual(["free", "free2", "free2"], [item[0] for item in log])
        events = client.telemetry["fallback_events"]
        self.assertEqual(1, sum(e["cause"] == "model_unavailable_for_provider" for e in events))
        self.assertEqual({"google_search": {}}, log[0][2]["tools"][0])
        self.assertNotIn("retrieval", json.dumps(log[0][2]).casefold())

    def test_quota_uses_free_retry_then_paid_key(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient(
                "free",
                [HttpError(429, "quota"), HttpError(429, "quota")],
                log,
            ),
            "PAID": SequencedClient("paid", [sdk_response()], log),
        }
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE", "GEMINI_API_KEY": "PAID"},
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            sleep=lambda _: None,
        )
        answer = client.search("q", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual(["free", "paid"], [item[0] for item in log])
        self.assertEqual("gemini_paid", answer.provider)
        self.assertEqual("quota_429", answer.fallbacks[-1]["cause"])
        self.assertEqual(1, client.telemetry["providers"]["gemini_free_1"]["errors"])
        self.assertEqual(1, client.telemetry["providers"]["gemini_paid"]["responses"])

    def test_observed_url_corruptions_are_normalized_before_parsing(self) -> None:
        expected = "https://camaqua.rs.gov.br/concursos?ano=2026&tipo=aberto"
        cases = (
            expected + "`",
            expected + "%60",
            "**" + expected + "**",
            "__" + expected + "__",
            f'“{expected}”).',
            f"[índice oficial]({expected})",
            "https://www.google.com/url?q=" + expected.replace("&", "%26"),
            expected.replace("&", "&amp;") + "\u200b",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(expected, _clean_url(raw))

    def test_joined_slug_becomes_natural_name_and_hint_survives_every_query(self) -> None:
        target = Target(
            "fortalezadosvalos", "concurso_publico", "documentos oficiais conhecidos"
        )
        queries = build_queries(target)
        self.assertEqual(5, len(queries))
        self.assertTrue(all('"Fortaleza dos Valos"' in query for query in queries))
        self.assertTrue(all("fortalezadosvalos" not in query for query in queries))
        self.assertTrue(all("Pista original: documentos oficiais conhecidos" in query for query in queries))
        self.assertTrue(all("site:pmfv.rs.gov.br" in query and "2026" in query for query in queries))
        self.assertTrue(all("concurso público" in query and "excluir processo seletivo" in query for query in queries))
        pss_queries = build_queries(Target(
            "fortalezadosvalos", "processo_seletivo", "mesma pista"
        ))
        self.assertTrue(all(
            "processo seletivo simplificado" in query
            and "excluir concurso público" in query
            and "Pista original: mesma pista" in query
            for query in pss_queries
        ))

    def test_micro_acquisition_does_not_infer_url_from_target_hint(self) -> None:
        original = "https://camaqua.rs.gov.br/concursos"
        target = Target("camaqua", "concurso_publico", f"Replay conhecido: {original}", "render_incierto")
        with tempfile.TemporaryDirectory() as directory:
            rows, _ = run_rescue(
                [target],
                client=FakeGroundedClient([GroundedAnswer(text="")]),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=Path(directory),
                micro_acquire=True,
                renderer=lambda _: self.fail("pista must not select an acquisition URL"),
            )
            durable = json.loads(
                (Path(directory) / "micro_camaqua_concurso_publico.json").read_text(encoding="utf-8")
            )
        self.assertEqual("", durable["url_inicial"])
        self.assertEqual("", durable["url_final"])
        self.assertEqual([], durable["cadena_redirects"])
        self.assertEqual("sin_candidata_evaluada_no_ambigua", durable["razon_seleccion_url"])
        self.assertEqual([], rows)

    def test_url_mala_with_detectable_multi24_dispatches_adapter(self) -> None:
        url = "https://sistemas.camaqua.rs.gov.br/multi24/transparencia/concursos"
        dispatch_calls: list[str] = []

        def dispatcher(**kwargs):
            dispatch_calls.append(kwargs["url"])
            return {"platform": "multi24", "adapter": "fake", "candidates": []}

        with tempfile.TemporaryDirectory() as directory:
            run_rescue(
                [Target("camaqua", "concurso_publico", "pista sin URL", "url_mala")],
                client=FakeGroundedClient([GroundedAnswer(text=url)]),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=Path(directory),
                micro_acquire=True,
                adapter_dispatcher=dispatcher,
                renderer=lambda _: None,
            )
        self.assertEqual([url], dispatch_calls)

    def test_url_mala_without_detectable_platform_does_not_dispatch_adapter(self) -> None:
        url = "https://camaqua.rs.gov.br/concursos"
        dispatch_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            run_rescue(
                [Target("camaqua", "concurso_publico", "pista sin URL", "url_mala")],
                client=FakeGroundedClient([GroundedAnswer(text=url)]),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=Path(directory),
                micro_acquire=True,
                adapter_dispatcher=lambda **kwargs: dispatch_calls.append(kwargs["url"]),
                renderer=lambda _: None,
            )
        self.assertEqual([], dispatch_calls)

    def test_adapters_only_url_map_dispatches_without_hint_urls_or_gemini(self) -> None:
        class BombClient:
            telemetry = {"providers": {"gemini_free_1": {"calls": 0}}}

            def search(self, *args, **kwargs):
                raise AssertionError("Gemini must be unreachable in adapters-only")

        progresso = (
            "https://sistemas.progresso.rs.gov.br/multi24/transparencia/index"
            "?entidade=1&secao=dinamico&id=6146"
        )
        dois_lajeados = (
            "https://doislajeados.atende.net/cidadao/pagina/concursos"
            "?filtro=abertos&ano=2026"
        )
        dispatch_calls: list[str] = []

        def dispatcher(**kwargs):
            dispatch_calls.append(kwargs["url"])
            return {"platform": "fake", "adapter": "fake", "candidates": []}

        with tempfile.TemporaryDirectory() as directory:
            url_map_path = Path(directory) / "urlmap.csv"
            url_map_path.write_text(
                "municipio,bucket,url\n"
                "PROGRESSO,CONCURSO PUBLICO,"
                f"{progresso.replace('&', '&amp;')}\n"
                "DÓIS   LAJEADOS,Processo Seletivo,"
                f"{dois_lajeados.replace('&', '&amp;')}\n",
                encoding="utf-8",
            )
            url_map = read_url_map(url_map_path)
            _, summary = run_rescue(
                [
                    Target("progresso", "concurso_publico", "portal a revisar", "url_mala"),
                    Target("doislajeados", "processo_seletivo", "verificar fonte", "url_mala"),
                ],
                client=BombClient(),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=Path(directory),
                adapters_only=True,
                url_map=url_map,
                adapter_dispatcher=dispatcher,
                renderer=lambda _: self.fail("adapters-only must not render"),
            )
        self.assertEqual([progresso, dois_lajeados], dispatch_calls)
        self.assertEqual(2, summary["global"]["unidades"])
        self.assertEqual(0, summary["global"]["model_requests"])

    def test_adapters_only_cli_accepts_url_map_flag(self) -> None:
        args = _parser().parse_args([
            "--targets", "targets.csv",
            "--url-map", "holdout_urlmap.csv",
            "--output-dir", "output",
            "--adapters-only",
        ])
        self.assertEqual(Path("holdout_urlmap.csv"), args.url_map)

    def test_adapters_only_skips_url_map_unit_without_detectable_platform(self) -> None:
        class BombClient:
            telemetry = {"providers": {"gemini_free_1": {"calls": 0}}}

            def search(self, *args, **kwargs):
                raise AssertionError("Gemini must be unreachable in adapters-only")

        dispatch_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            _, summary = run_rescue(
                [Target("camaqua", "processo_seletivo", "pista sem URL", "url_mala")],
                client=BombClient(),
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=Path(directory),
                adapters_only=True,
                url_map={
                    ("camaqua", "processoseletivo"):
                        "https://camaqua.rs.gov.br/concursos"
                },
                adapter_dispatcher=lambda **kwargs: dispatch_calls.append(kwargs["url"]),
                renderer=lambda _: self.fail("adapters-only must not render"),
            )
        self.assertEqual([], dispatch_calls)
        skipped = summary["unidades"]["camaqua/processo_seletivo"]
        self.assertEqual("skipped", skipped["estado"])
        self.assertEqual("sin_plataforma_detectable", skipped["causa"])
        self.assertEqual(0, summary["global"]["model_requests"])

    def test_free_only_run_never_constructs_or_calls_paid_client(self) -> None:
        args = _parser().parse_args([
            "--targets", "targets.csv",
            "--output-dir", "output",
            "--credentials-file", "credentials.env",
            "--free-only",
        ])
        self.assertTrue(args.free_only)
        self.assertFalse(args.paid_authorized)
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient(
                "free", [HttpError(429, "minute quota", headers={"Retry-After": "5"})], log
            ),
            "FREE2": SequencedClient("free2", [sdk_response()], log),
        }

        def factory(*, api_key, vertexai):
            if api_key == "PAID_BOMB":
                raise AssertionError("paid client must be structurally unreachable")
            return clients[api_key]

        client = GeminiGroundedClient(
            {
                "GEMINI_API_KEY_FREE": "FREE",
                "GEMINI_API_KEY_FREE_2": "FREE2",
                "GEMINI_API_KEY": "PAID_BOMB",
            },
            client_factory=factory,
            free_only=True,
            sleep=lambda _: None,
        )
        self.assertEqual(0, client.telemetry["paid_calls"])
        _, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
            free_only=True,
        )
        self.assertEqual(0, client.telemetry["paid_calls"])
        self.assertEqual(0, summary["global"]["paid_calls"])
        self.assertEqual(["free", "free2"], [item[0] for item in log])

    def test_cli_without_authorization_flag_preserves_policy_failure(self) -> None:
        with self.assertRaisesRegex(
            PolicyFailure,
            r"^FALLO_DE_POLITICA:rescate_cli_requiere_--free-only$",
        ):
            main([
                "--targets", "targets.csv",
                "--output-dir", "output",
                "--credentials-file", "credentials.env",
            ])

    def test_cli_authorization_flags_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            _parser().parse_args([
                "--targets", "targets.csv",
                "--output-dir", "output",
                "--credentials-file", "credentials.env",
                "--free-only",
                "--paid-authorized",
            ])
        self.assertEqual(2, raised.exception.code)

    def test_paid_authorized_reaches_paid_after_both_free_tiers_429(self) -> None:
        args = _parser().parse_args([
            "--targets", "targets.csv",
            "--output-dir", "output",
            "--credentials-file", "credentials.env",
            "--paid-authorized",
            "--max-paid-calls", "3",
        ])
        self.assertFalse(args.free_only)
        self.assertTrue(args.paid_authorized)
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient("free", [HttpError(429, "minute quota")], log),
            "FREE2": SequencedClient("free2", [HttpError(429, "minute quota")], log),
            "PAID": SequencedClient("paid", [sdk_response()], log),
        }
        client = GeminiGroundedClient(
            {
                "GEMINI_API_KEY_FREE": "FREE",
                "GEMINI_API_KEY_FREE_2": "FREE2",
                "GEMINI_API_KEY": "PAID",
            },
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            free_only=args.free_only,
            max_paid_calls=args.max_paid_calls,
            max_free2_attempts=1,
            sleep=lambda _: None,
        )
        _, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
            free_only=args.free_only,
            paid_authorization=(PAID_AUTHORIZATION if args.paid_authorized else None),
            max_paid_calls=args.max_paid_calls,
        )
        self.assertEqual(["free", "free2", "paid"], [item[0] for item in log])
        self.assertEqual(PAID_AUTHORIZATION, summary["policy"]["paid_authorization"])
        self.assertEqual(1, summary["global"]["paid_calls"])
        self.assertEqual(1, summary["global"]["telemetria"]["providers"]["gemini_paid"]["calls"])

    def test_paid_authorized_requires_max_paid_calls_before_work(self) -> None:
        with patch("sys.stderr") as stderr, self.assertRaises(SystemExit) as raised:
            main([
                "--targets", "missing-targets.csv",
                "--output-dir", "output",
                "--credentials-file", "missing-credentials.env",
                "--paid-authorized",
            ])
        self.assertEqual(2, raised.exception.code)
        self.assertIn(
            "--max-paid-calls es obligatorio con --paid-authorized",
            "".join(call.args[0] for call in stderr.write.call_args_list),
        )

    def test_paid_cap_blocks_fourth_paid_call_checkpoints_and_summarizes(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient(
                "free", [HttpError(429, "minute quota") for _ in range(4)], log
            ),
            "FREE2": SequencedClient(
                "free2", [HttpError(429, "minute quota") for _ in range(4)], log
            ),
            "PAID": SequencedClient("paid", [sdk_response() for _ in range(3)], log),
        }
        client = GeminiGroundedClient(
            {
                "GEMINI_API_KEY_FREE": "FREE",
                "GEMINI_API_KEY_FREE_2": "FREE2",
                "GEMINI_API_KEY": "PAID",
            },
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            max_paid_calls=3,
            max_free2_attempts=1,
            sleep=lambda _: None,
        )
        targets = [
            Target(name, "concurso_publico", "pista")
            for name in ("camaqua", "canoas", "esteio", "gravatai")
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            _, summary = run_rescue(
                targets,
                client=client,
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
                paid_authorization=PAID_AUTHORIZATION,
                max_paid_calls=3,
            )
            durable = json.loads(
                (output / "unidad_gravatai_concurso_publico.json").read_text(encoding="utf-8")
            )
        self.assertEqual(3, sum(item[0] == "paid" for item in log))
        self.assertEqual(3, client.telemetry["paid_calls"])
        self.assertEqual("failed", durable["estado"])
        self.assertEqual("paid_cap_alcanzado", durable["causa"])
        self.assertEqual(
            {"limite": 3, "alcanzado_en_unidad": "esteio/concurso_publico"},
            summary["paid_cap"],
        )

    def test_resume_subtracts_five_previous_paid_calls_from_cap_thirty(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient("free", [HttpError(429, "minute quota")], log),
            "FREE2": SequencedClient("free2", [HttpError(429, "minute quota")], log),
            "PAID": SequencedClient("paid", [sdk_response() for _ in range(25)], log),
        }
        client = GeminiGroundedClient(
            {
                "GEMINI_API_KEY_FREE": "FREE",
                "GEMINI_API_KEY_FREE_2": "FREE2",
                "GEMINI_API_KEY": "PAID",
            },
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            max_paid_calls=30,
            max_free2_attempts=1,
            sleep=lambda _: None,
        )
        targets = [
            Target(f"municipio{i}", "concurso_publico", "pista")
            for i in range(26)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            previous = {
                "schema_version": 1,
                "municipio": "previa",
                "bucket": "concurso_publico",
                "grounded": {"busquedas_usadas": 0, "candidatas": []},
                "telemetria": {
                    "providers": {"gemini_paid": {"calls": 5}},
                    "fallbacks": [],
                },
                "estado": "completed",
                "causa": None,
                "timestamp": "2026-07-14T00:00:00+00:00",
            }
            (output / "unidad_previa_concurso_publico.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )
            _, summary = run_rescue(
                targets,
                client=client,
                fetcher=FakeFetcher(),
                max_searches=1,
                sleep_seconds=0,
                output_dir=output,
                resume=True,
                paid_authorization=PAID_AUTHORIZATION,
                max_paid_calls=30,
            )
        self.assertEqual(25, sum(item[0] == "paid" for item in log))
        self.assertEqual(5, summary["global"]["paid_calls_previas"])
        self.assertEqual(25, summary["global"]["paid_calls_nuevas"])
        self.assertEqual(25, summary["global"]["tope_efectivo"])
        self.assertEqual(25, summary["paid_cap"]["limite"])

    def test_free_only_run_ignores_max_paid_calls(self) -> None:
        args = _parser().parse_args([
            "--targets", "targets.csv",
            "--output-dir", "output",
            "--credentials-file", "credentials.env",
            "--free-only",
            "--max-paid-calls", "1",
        ])
        log: list[tuple] = []
        free = SequencedClient("free", [sdk_response()], log)
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE"},
            client_factory=lambda **_: free,
            free_only=args.free_only,
            max_paid_calls=args.max_paid_calls,
            sleep=lambda _: None,
        )
        _, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
            free_only=args.free_only,
            max_paid_calls=args.max_paid_calls,
        )
        self.assertEqual(0, summary["global"]["paid_calls"])
        self.assertNotIn("paid_cap", summary)
        self.assertFalse(any(
            unit.get("causa") == "paid_cap_alcanzado"
            for unit in summary["unidades"].values()
        ))

    def test_global_call_budget_cli_accepts_value_above_default(self) -> None:
        args = _parser().parse_args([
            "--targets", "targets.csv",
            "--output-dir", "output",
            "--credentials-file", "credentials.env",
            "--free-only",
            "--global-call-budget", "250",
        ])
        self.assertEqual(250, args.global_call_budget)

    def test_free_only_credential_loader_does_not_require_or_return_paid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.env"
            path.write_text(
                "GEMINI_API_KEY_FREE=free-placeholder\n"
                "GEMINI_API_KEY=paid-placeholder\n",
                encoding="utf-8",
            )
            loaded = load_grounded_credentials(path, free_only=True)
        self.assertEqual({"GEMINI_API_KEY_FREE": "free-placeholder"}, loaded)

    def test_free_only_aborts_with_policy_state_if_paid_counter_changes(self) -> None:
        class ViolatingClient:
            def __init__(self) -> None:
                self.telemetry = {
                    "providers": {
                        "gemini_free_1": {"calls": 0, "errors": 0, "responses": 0},
                        "gemini_paid": {"calls": 0, "errors": 0, "responses": 0},
                    }
                }

            def search(self, query, *, model, municipio, bucket):
                self.telemetry["providers"]["gemini_paid"]["calls"] = 1
                return GroundedAnswer(text="")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(PolicyFailure):
                run_rescue(
                    [Target("camaqua", "concurso_publico", "pista")],
                    client=ViolatingClient(), fetcher=FakeFetcher(), max_searches=1,
                    sleep_seconds=0, output_dir=output, free_only=True,
                )
            unit = json.loads((output / "unidad_camaqua_concurso_publico.json").read_text(encoding="utf-8"))
        self.assertEqual("FALLO_DE_POLITICA", unit["estado"])

    def test_quota_governor_enforces_twelve_rpm_with_injected_clock(self) -> None:
        clock = FakeClock()
        log: list[tuple] = []
        free = SequencedClient("free", [sdk_response(), sdk_response()], log)
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE"},
            client_factory=lambda **_: free,
            free_only=True,
            clock=clock.monotonic,
            sleep=clock.sleep,
        )
        client.search("q1", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        client.search("q2", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual([5.0], clock.sleeps)
        self.assertEqual(2, client.telemetry["model_requests"])

    def test_free2_429_honors_retry_after_and_is_vetoed_for_run(self) -> None:
        clock = FakeClock()
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient(
                "free", [HttpError(429, "minute quota", headers={"Retry-After": "5"})], log
            ),
            "FREE2": SequencedClient(
                "free2", [HttpError(429, "minute quota", headers={"Retry-After": "7"})], log
            ),
        }
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE", "GEMINI_API_KEY_FREE_2": "FREE2"},
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            free_only=True,
            clock=clock.monotonic,
            sleep=clock.sleep,
        )
        with self.assertRaises(RuntimeError):
            client.search("q", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertIn(5.0, clock.sleeps)
        self.assertIn(7.0, clock.sleeps)
        self.assertEqual(2, client.telemetry["quota_429"])
        self.assertEqual(0, client.telemetry["paid_calls"])
        self.assertEqual(1, sum(item[0] == "free2" for item in log))

    def test_free1_quota_429_is_not_retried_by_second_unit(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient("free", [HttpError(429, "minute quota")], log),
            "FREE2": SequencedClient("free2", [sdk_response(), sdk_response()], log),
        }
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE", "GEMINI_API_KEY_FREE_2": "FREE2"},
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            free_only=True,
            sleep=lambda _: None,
        )
        _, summary = run_rescue(
            [
                Target("camaqua", "concurso_publico", "pista"),
                Target("canoas", "concurso_publico", "pista"),
            ],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
            free_only=True,
        )
        self.assertEqual(1, sum(item[0] == "free" for item in log))
        self.assertEqual(2, sum(item[0] == "free2" for item in log))
        self.assertIn(
            ("gemini_free_1", REQUIRED_MODEL, "quota_429_run_veto"),
            [tuple(item) for item in summary["capacidad_vetada"]],
        )

    def test_missing_search_count_metadata_records_unknown_without_estimate(self) -> None:
        log: list[tuple] = []
        free = SequencedClient("free", [sdk_response()], log)
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE"}, client_factory=lambda **_: free,
            free_only=True, sleep=lambda _: None,
        )
        client.search("q", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual(0, client.telemetry["google_search_queries"])
        self.assertEqual(1, client.telemetry["query_count_unknown"])
        self.assertEqual(1, client.telemetry["grounded_responses"])

    def test_real_search_query_metadata_is_counted_exactly(self) -> None:
        log: list[tuple] = []
        response = sdk_response()
        response.candidates[0]["grounding_metadata"]["web_search_queries"] = ["q1", "q2"]
        free = SequencedClient("free", [response], log)
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE"}, client_factory=lambda **_: free,
            free_only=True, sleep=lambda _: None,
        )
        client.search("q", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual(2, client.telemetry["google_search_queries"])
        self.assertEqual(0, client.telemetry["query_count_unknown"])

    def test_preventive_brake_stops_before_request_450(self) -> None:
        governor = QuotaGovernor(global_call_budget=500, sleep=lambda _: None)
        with self.assertRaises(PreventiveQuotaStop):
            governor.before_request(model_requests=449, google_search_queries=0)
        with self.assertRaises(PreventiveQuotaStop):
            governor.before_request(model_requests=0, google_search_queries=449)
        budget = QuotaGovernor(global_call_budget=3, sleep=lambda _: None)
        with self.assertRaises(PreventiveQuotaStop):
            budget.before_request(model_requests=3, google_search_queries=0)

    def test_quota_backoff_is_exponential_with_jitter_without_retry_after(self) -> None:
        governor = QuotaGovernor(
            global_call_budget=10, sleep=lambda _: None, jitter=lambda: 0.25
        )
        self.assertEqual(1.25, governor.backoff_seconds(1, None))
        self.assertEqual(2.25, governor.backoff_seconds(2, None))
        self.assertEqual(9.0, governor.backoff_seconds(3, 9.0))

    def test_preview_or_legacy_model_is_rejected_before_any_call(self) -> None:
        log: list[tuple] = []
        free = SequencedClient("free", [sdk_response()], log)
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE"}, client_factory=lambda **_: free,
            free_only=True,
        )
        with self.assertRaises(ValueError):
            client.search(
                "q", model="gemini-3.1-flash-lite-preview",
                municipio="camaqua", bucket="concurso_publico",
            )
        self.assertEqual([], log)

    def test_daily_free2_exhaustion_checkpoints_and_stops_without_paid(self) -> None:
        clock = FakeClock()
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient("free", [HttpError(429, "minute quota")], log),
            "FREE2": SequencedClient("free2", [HttpError(429, "requests per day exhausted")], log),
        }
        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE", "GEMINI_API_KEY_FREE_2": "FREE2"},
            client_factory=lambda *, api_key, vertexai: clients[api_key],
            free_only=True, clock=clock.monotonic, sleep=clock.sleep,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            _, summary = run_rescue(
                [Target("camaqua", "concurso_publico", "pista")],
                client=client, fetcher=FakeFetcher(), max_searches=1,
                sleep_seconds=0, output_dir=output, free_only=True,
            )
            unit = json.loads((output / "unidad_camaqua_concurso_publico.json").read_text(encoding="utf-8"))
        self.assertEqual("DETENIDA_CUOTA_DIARIA_FREE2", unit["estado"])
        self.assertEqual("checkpoint_atomico_cuota_diaria_free2", unit["causa"])
        self.assertEqual(0, summary["global"]["paid_calls"])

    def test_outputs_never_confirm_or_write_url_map(self) -> None:
        client = FakeGroundedClient([
            GroundedAnswer(text="https://camaqua.rs.gov.br/concursos", model=REQUIRED_MODEL)
        ])
        rows, summary = run_rescue(
            [Target("camaqua", "concurso_publico", "pista")],
            client=client,
            fetcher=FakeFetcher(),
            max_searches=1,
            sleep_seconds=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_outputs(output, rows, summary)
            self.assertEqual({"candidates.csv", "summary.json"}, {item.name for item in output.iterdir()})
            payload = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["policy"]["confirmation_performed"])
            self.assertFalse(payload["policy"]["writes_url_map"])
            self.assertFalse(payload["unidades"]["camaqua/concurso_publico"]["confirmacion"])
            self.assertFalse(any("url_map" in item.name for item in output.iterdir()))


if __name__ == "__main__":
    unittest.main()
