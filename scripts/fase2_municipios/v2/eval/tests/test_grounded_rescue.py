"""Offline contract tests for grounded_rescue (stdlib unittest; no sockets)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.fase2_municipios.v2.eval.grounded_rescue import (
    FALLBACK_MODEL,
    REQUIRED_MODEL,
    GeminiGroundedClient,
    GroundedAnswer,
    Target,
    build_queries,
    micro_acquire_unit,
    read_targets,
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
            html="<html><li>Edital 01/2025</li><li>Edital 02/2026</li></html>",
            final_url=url,
        )


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
                "veredicto_gate", "timestamp",
            }, set(durable))
            self.assertTrue(durable["citas_candidatas"])
            self.assertTrue(durable["veredicto_gate"]["pasa"])

    def test_render_without_static_or_micro_evidence_has_no_candidate(self) -> None:
        client = FakeGroundedClient([GroundedAnswer(
            text="https://camaqua.atende.net/cidadao/pagina/processos-seletivos",
            model=REQUIRED_MODEL,
        )])
        target = Target("camaqua", "processo_seletivo", "shell", "render_incierto")
        rows, summary = run_rescue(
            [target],
            client=client,
            fetcher=FakeFetcher(),
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
                fetcher=FakeFetcher(),
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
            "PAID": SequencedClient("paid", [sdk_response()], log),
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
        self.assertEqual([REQUIRED_MODEL, FALLBACK_MODEL], [item[1] for item in log])
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
