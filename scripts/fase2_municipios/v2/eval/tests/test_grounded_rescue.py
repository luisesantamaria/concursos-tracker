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
    REQUIRED_MODEL,
    GeminiGroundedClient,
    GroundedAnswer,
    InterruptionState,
    Target,
    _new_redirect_session,
    build_queries,
    extract_answer_url_sources,
    micro_acquire_unit,
    read_targets,
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
    def __init__(self, status: int, message: str, *, pro_rejected: bool = False) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status, headers={})
        self.pro_rejected = pro_rejected


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
                [HttpError(404, "gemini-2.5-pro model not found", pro_rejected=True)],
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
                [HttpError(404, "gemini-2.5-pro model not found", pro_rejected=True)],
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
        self.assertEqual(4, len(session.calls))
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
            "superficie oficial estatica alternativa",
            "endpoint XHR AJAX",
            "URL final da listagem oficial",
            "documento ou indice oficial enlazado",
            "parametros reproduziveis de filtro e paginacao",
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

    def test_explicit_pro_rejection_is_only_then_flash_fallback(self) -> None:
        log: list[tuple] = []
        clients = {
            "FREE": SequencedClient(
                "free",
                [
                    HttpError(404, "gemini-2.5-pro model not found", pro_rejected=True),
                    sdk_response(),
                ],
                log,
            ),
            "PAID": SequencedClient(
                "paid",
                [HttpError(404, "gemini-2.5-pro model not found", pro_rejected=True)],
                log,
            ),
        }

        def factory(*, api_key, vertexai):
            self.assertFalse(vertexai)
            return clients[api_key]

        client = GeminiGroundedClient(
            {"GEMINI_API_KEY_FREE": "FREE", "GEMINI_API_KEY": "PAID"},
            client_factory=factory,
            sleep=lambda _: None,
        )
        answer = client.search("q", model=REQUIRED_MODEL, municipio="camaqua", bucket="concurso_publico")
        self.assertEqual(FALLBACK_MODEL, answer.model)
        self.assertEqual(
            [REQUIRED_MODEL, REQUIRED_MODEL, FALLBACK_MODEL],
            [item[1] for item in log],
        )
        self.assertEqual("explicit_pro_rejection", answer.fallbacks[0]["cause"])
        self.assertIn("model not found", answer.fallbacks[0]["exact_error"])
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
        self.assertEqual(["free", "free", "paid"], [item[0] for item in log])
        self.assertEqual("gemini_paid", answer.provider)
        self.assertEqual("quota_429", answer.fallbacks[-1]["cause"])
        self.assertEqual(2, client.telemetry["providers"]["gemini_free_1"]["errors"])
        self.assertEqual(1, client.telemetry["providers"]["gemini_paid"]["responses"])

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
