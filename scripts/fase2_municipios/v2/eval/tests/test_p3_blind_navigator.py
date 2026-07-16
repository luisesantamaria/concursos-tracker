"""Offline unittest contracts for the deterministic P3 blind navigator."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from scripts.fase2_municipios.v2.eval import p3_blind_navigator as runner


class FailOnCall:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError(f"{self.label} must not be called")


class FakeSession:
    """Mock of the narrow Playwright session protocol used by the runner."""

    def __init__(
        self,
        states: dict[str, runner.PageState],
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.states = states
        self.failures = failures or {}
        self.visited: list[str] = []

    async def visit(self, url: str) -> runner.PageState:
        self.visited.append(url)
        if url in self.failures:
            raise self.failures[url]
        return self.states[url]

    async def close(self) -> None:
        return None


class PlaywrightError(Exception):
    """Playwright-shaped exception used without importing Playwright."""


def _state(
    url: str,
    text: str,
    links: tuple[runner.Link, ...] = (),
) -> runner.PageState:
    return runner.PageState(url=url, text=text, links=links, title="Prefeitura")


def _item_page(url: str, number: str = "01") -> runner.PageState:
    return _state(
        url,
        "Publicacoes e resultados\n"
        f"Concurso Publico nº {number}/2026 - Edital de abertura\n"
        "Concurso Publico nº 02/2025 - Edital e anexos\n"
        "Filtrar por ano",
    )


def _run_navigation(
    root: Path,
    states: dict[str, runner.PageState],
    *,
    failures: dict[str, Exception] | None = None,
    bucket: str = "concurso_publico",
) -> tuple[runner.NavigationOutcome, FakeSession, list[dict[str, object]]]:
    session = FakeSession(states, failures)
    output = root / "output"
    with runner.AuditLogger(output) as audit:
        outcome = asyncio.run(
            runner.navigate_unit(
                session,
                runner.Unit("Municipio Teste", bucket, "https://prefeitura.test/"),
                audit,
            )
        )
    events = [
        json.loads(line)
        for line in (output / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return outcome, session, events


class BlindNavigatorTests(unittest.TestCase):
    def _temporary_root(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory()

    def _assert_manifest_header_rejected_without_navigation(
        self, header: list[str]
    ) -> None:
        with self._temporary_root() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.csv"
            gate = root / "gate.md"
            equivalencias = root / "equivalencias.md"
            manifest.write_text(
                ",".join(header) + "\n" + ",".join("value" for _ in header) + "\n",
                encoding="utf-8",
            )
            gate.write_text("gate", encoding="utf-8")
            equivalencias.write_text("equivalencias", encoding="utf-8")

            playwright_call = FailOnCall("Playwright")
            navigation_call = FailOnCall("navigation")
            fake_playwright = types.ModuleType("playwright")
            fake_async_api = types.ModuleType("playwright.async_api")
            fake_async_api.async_playwright = playwright_call  # type: ignore[attr-defined]
            fake_playwright.async_api = fake_async_api  # type: ignore[attr-defined]
            previous_playwright = sys.modules.get("playwright")
            previous_async_api = sys.modules.get("playwright.async_api")
            previous_navigate_unit = runner.navigate_unit
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.async_api"] = fake_async_api
            runner.navigate_unit = navigation_call  # type: ignore[assignment]
            args = argparse.Namespace(
                manifest=str(manifest),
                gate=str(gate),
                equivalencias=str(equivalencias),
                output_dir=str(root / "output"),
            )
            try:
                with self.assertRaises(runner.BlindAccessViolation) as caught:
                    asyncio.run(runner.run(args))
            finally:
                runner.navigate_unit = previous_navigate_unit
                if previous_playwright is None:
                    sys.modules.pop("playwright", None)
                else:
                    sys.modules["playwright"] = previous_playwright
                if previous_async_api is None:
                    sys.modules.pop("playwright.async_api", None)
                else:
                    sys.modules["playwright.async_api"] = previous_async_api

            self.assertIn(repr(runner.MANIFEST_FIELDNAMES), str(caught.exception))
            self.assertIn(repr(header), str(caught.exception))
            self.assertEqual(playwright_call.calls, 0)
            self.assertEqual(navigation_call.calls, 0)

    def test_manifest_rejects_url_confirmada_column_without_navigation(self) -> None:
        self._assert_manifest_header_rejected_without_navigation(
            ["municipio", "bucket", "site_base", "url_confirmada"]
        )

    def test_manifest_rejects_other_extra_column_without_navigation(self) -> None:
        self._assert_manifest_header_rejected_without_navigation(
            ["municipio", "bucket", "site_base", "fuente"]
        )

    def test_manifest_rejects_reordered_columns_without_navigation(self) -> None:
        self._assert_manifest_header_rejected_without_navigation(
            ["bucket", "municipio", "site_base"]
        )

    def test_manifest_accepts_exact_columns_in_exact_order(self) -> None:
        with self._temporary_root() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.csv"
            gate = root / "gate.md"
            equivalencias = root / "equivalencias.md"
            output = root / "output"
            manifest.write_text(
                "municipio,bucket,site_base\n"
                "Municipio Teste,concurso_publico,https://prefeitura.test/\n",
                encoding="utf-8",
            )
            gate.write_text("gate", encoding="utf-8")
            equivalencias.write_text("equivalencias", encoding="utf-8")
            with runner.AuditLogger(output) as audit:
                files = runner.BlindFileAccess(
                    manifest=manifest,
                    gate=gate,
                    equivalencias=equivalencias,
                    output_dir=output,
                    audit=audit,
                )
                units, _ = runner.load_inputs(files, manifest, gate, equivalencias)
            self.assertEqual(
                units,
                [runner.Unit("Municipio Teste", "concurso_publico", "https://prefeitura.test/")],
            )

    def test_final_output_is_one_url_never_a_candidate_list(self) -> None:
        with self._temporary_root() as temporary:
            states = {
                "https://prefeitura.test/": _state(
                    "https://prefeitura.test/",
                    "Portal municipal",
                    (runner.Link("Concursos", "/concursos"),),
                ),
                "https://prefeitura.test/concursos": _item_page(
                    "https://prefeitura.test/concursos"
                ),
            }
            outcome, _, _ = _run_navigation(Path(temporary), states)
            self.assertEqual(outcome.result, "https://prefeitura.test/concursos")
            self.assertIsInstance(outcome.result, str)
            self.assertIsNone(outcome.reason)
            self.assertTrue(outcome.citations)

    def test_isolated_pdf_download_error_does_not_kill_unit_or_siblings(self) -> None:
        with self._temporary_root() as temporary:
            pdf_url = "https://prefeitura.test/files/edital.pdf"
            surface_url = "https://prefeitura.test/concursos"
            states = {
                "https://prefeitura.test/": _state(
                    "https://prefeitura.test/",
                    "Portal municipal",
                    (
                        runner.Link("Concursos em PDF", pdf_url),
                        runner.Link("Editais", "/concursos"),
                    ),
                ),
                surface_url: _item_page(surface_url),
            }
            outcome, session, events = _run_navigation(
                Path(temporary),
                states,
                failures={pdf_url: PlaywrightError("Page.goto: Download is starting")},
            )
            self.assertEqual(session.visited, ["https://prefeitura.test/", pdf_url, surface_url])
            self.assertEqual(outcome.result, surface_url)
            branch_error = next(event for event in events if event["event"] == "branch_navigation_error")
            self.assertTrue(branch_error["discarded"])

    def test_sibling_branches_continue_after_one_relevant_branch_fails(self) -> None:
        with self._temporary_root() as temporary:
            failing_url = "https://prefeitura.test/concursos-a"
            sibling_url = "https://prefeitura.test/concursos-b"
            states = {
                "https://prefeitura.test/": _state(
                    "https://prefeitura.test/",
                    "Portal",
                    (
                        runner.Link("Concursos A", "/concursos-a"),
                        runner.Link("Concursos B", "/concursos-b"),
                    ),
                ),
                sibling_url: _item_page(sibling_url),
            }
            outcome, session, _ = _run_navigation(
                Path(temporary),
                states,
                failures={failing_url: PlaywrightError("browser context closed")},
            )
            self.assertEqual(session.visited, ["https://prefeitura.test/", failing_url, sibling_url])
            self.assertEqual(outcome.result, "REVISAR")
            self.assertIn("exploracion_incompleta", outcome.reason or "")

    def test_timeout_on_relevant_branch_produces_review(self) -> None:
        with self._temporary_root() as temporary:
            candidate_url = "https://prefeitura.test/concursos"
            timeout_url = "https://prefeitura.test/editais"
            states = {
                "https://prefeitura.test/": _state(
                    "https://prefeitura.test/",
                    "Portal",
                    (
                        runner.Link("Concursos", "/concursos"),
                        runner.Link("Editais", "/editais"),
                    ),
                ),
                candidate_url: _item_page(candidate_url),
            }
            outcome, session, _ = _run_navigation(
                Path(temporary),
                states,
                failures={timeout_url: TimeoutError("networkidle timeout after 30000ms")},
            )
            self.assertIn(timeout_url, session.visited)
            self.assertEqual(outcome.result, "REVISAR")
            self.assertNotEqual(outcome.result, candidate_url)
            self.assertIn("TimeoutError", outcome.reason or "")

    def test_two_surviving_candidates_preserve_ambiguity(self) -> None:
        with self._temporary_root() as temporary:
            states = {
                "https://prefeitura.test/": _state(
                    "https://prefeitura.test/",
                    "Portal",
                    (
                        runner.Link("Concursos", "/concursos-a"),
                        runner.Link("Editais de concursos", "/concursos-b"),
                    ),
                ),
                "https://prefeitura.test/concursos-a": _item_page(
                    "https://prefeitura.test/concursos-a", "10"
                ),
                "https://prefeitura.test/concursos-b": _item_page(
                    "https://prefeitura.test/concursos-b", "20"
                ),
            }
            outcome, _, _ = _run_navigation(Path(temporary), states)
            self.assertEqual(outcome.result, "REVISAR")
            self.assertEqual(outcome.reason, "ambiguedad_entre_superficies_plausibles")

    def test_error_free_event_sequence_matches_base_commit_snapshot(self) -> None:
        """Snapshot derived from navigate_unit at base commit 323798f."""

        with self._temporary_root() as temporary:
            root_url = "https://prefeitura.test/"
            surface_url = "https://prefeitura.test/concursos"
            archive_url = "https://prefeitura.test/arquivo"
            publications_url = "https://prefeitura.test/publicacoes"
            surface = _item_page(surface_url)
            states = {
                root_url: _state(
                    root_url,
                    "Portal",
                    (
                        runner.Link("Concursos", "/concursos"),
                        runner.Link("Publicacoes", "/publicacoes"),
                    ),
                ),
                surface_url: runner.PageState(
                    url=surface.url,
                    text=surface.text,
                    links=(runner.Link("Editais anteriores", "/arquivo"),),
                    title=surface.title,
                ),
                archive_url: _state(archive_url, "Arquivo sem itens"),
                publications_url: _state(publications_url, "Publicacoes gerais"),
            }
            outcome, session, events = _run_navigation(Path(temporary), states)
            semantic_events = []
            for event in events:
                if event["event"] == "url_visit":
                    semantic_events.append(
                        ("visit", event["requested_url"], event["depth"])
                    )
                elif event["event"] == "semantic_interaction":
                    semantic_events.append(
                        (
                            "interaction",
                            event["number"],
                            event["from_url"],
                            event["href"],
                            event["exact_text"],
                        )
                    )

            self.assertEqual(
                semantic_events,
                [
                    ("visit", root_url, 0),
                    ("interaction", 1, root_url, surface_url, "Concursos"),
                    ("visit", surface_url, 1),
                    ("interaction", 2, surface_url, archive_url, "Editais anteriores"),
                    ("visit", archive_url, 2),
                    ("interaction", 3, root_url, publications_url, "Publicacoes"),
                    ("visit", publications_url, 1),
                ],
            )
            self.assertEqual(
                session.visited,
                [root_url, surface_url, archive_url, publications_url],
            )
            self.assertEqual(outcome.result, surface_url)
            self.assertIsNone(outcome.reason)
            self.assertEqual(outcome.interaction_count, 3)
            self.assertEqual(outcome.additional_pages, 3)
            self.assertEqual(
                [entry["url"] for entry in outcome.explored],
                [root_url, surface_url, archive_url, publications_url],
            )

    def test_navigation_error_reason_includes_full_exception_and_failure_stage(self) -> None:
        with self._temporary_root() as temporary:
            message = "Page.goto: browser context closed during networkidle"
            outcome, _, events = _run_navigation(
                Path(temporary),
                {},
                failures={"https://prefeitura.test/": PlaywrightError(message)},
            )
            self.assertEqual(outcome.result, "REVISAR")
            self.assertIn(message, outcome.reason or "")
            error = next(event for event in events if event["event"] == "unit_navigation_error")
            self.assertEqual(error["failure_stage"], "captura_estado")
            self.assertIn(message, str(error["message"]))

    def test_exception_authorization_bearer_token_is_redacted(self) -> None:
        with self._temporary_root() as temporary:
            raw_token = "raw-token-value"
            outcome, _, events = _run_navigation(
                Path(temporary),
                {},
                failures={
                    "https://prefeitura.test/": PlaywrightError(
                        f"Authorization: Bearer {raw_token}"
                    )
                },
            )
            self.assertIn("<redacted>", outcome.reason or "")
            self.assertNotIn(raw_token, outcome.reason or "")
            error = next(event for event in events if event["event"] == "unit_navigation_error")
            self.assertIn("<redacted>", str(error["message"]))
            self.assertNotIn(raw_token, json.dumps(error))

    def test_internal_whitelist_and_hard_limits_are_respected(self) -> None:
        with self._temporary_root() as temporary:
            root_links = tuple(
                runner.Link(f"Concursos {index}", f"/concursos-{index}")
                for index in range(10)
            ) + (runner.Link("Concursos externos", "https://evil.example/concursos"),)
            states = {
                "https://prefeitura.test/": _state(
                    "https://prefeitura.test/", "Portal", root_links
                )
            }
            for index in range(10):
                url = f"https://prefeitura.test/concursos-{index}"
                states[url] = _state(url, "Area de concursos sem itens")
            outcome, session, _ = _run_navigation(Path(temporary), states)
            self.assertLessEqual(outcome.interaction_count, runner.MAX_INTERACTIONS)
            self.assertLessEqual(outcome.additional_pages, runner.MAX_ADDITIONAL_PAGES)
            self.assertLessEqual(len(session.visited), 1 + runner.MAX_ADDITIONAL_PAGES)
            self.assertNotIn("https://evil.example/concursos", session.visited)

    def test_audit_log_records_every_declared_input_open(self) -> None:
        with self._temporary_root() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.csv"
            gate = root / "gate.md"
            equivalencias = root / "equivalencias.md"
            output = root / "output"
            manifest.write_text(
                "municipio,bucket,site_base\n"
                "Sao Jose,concurso_publico,https://prefeitura.test/\n",
                encoding="utf-8",
            )
            gate.write_text("# Gate\n", encoding="utf-8")
            equivalencias.write_text("# Equivalencias\n", encoding="utf-8")
            with runner.AuditLogger(output) as audit:
                files = runner.BlindFileAccess(
                    manifest=manifest,
                    gate=gate,
                    equivalencias=equivalencias,
                    output_dir=output,
                    audit=audit,
                )
                runner.load_inputs(files, manifest, gate, equivalencias)
            events = [
                json.loads(line)
                for line in (output / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            allowed_reads = {
                Path(event["path"]).resolve()
                for event in events
                if event["event"] == "file_open"
                and event.get("allowed") is True
                and event.get("purpose") == "read"
            }
            self.assertEqual(allowed_reads, {manifest.resolve(), gate.resolve(), equivalencias.resolve()})

    def test_forbidden_paths_raise_explicitly(self) -> None:
        with self._temporary_root() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.csv"
            gate = root / "gate.md"
            equivalencias = root / "equivalencias.md"
            output = root / "staging" / "corrida_actual"
            for path in (manifest, gate, equivalencias):
                path.write_text("declarado", encoding="utf-8")
            with runner.AuditLogger(output) as audit:
                files = runner.BlindFileAccess(
                    manifest=manifest,
                    gate=gate,
                    equivalencias=equivalencias,
                    output_dir=output,
                    audit=audit,
                )
                for relative_path in (
                    Path("golden_set_v1.csv"),
                    Path("url_map_secret.csv"),
                    Path("staging") / "otra_corrida" / "resultado.json",
                ):
                    with self.subTest(relative_path=relative_path):
                        with self.assertRaises(runner.BlindAccessViolation):
                            files.open(root / relative_path, "r")

    def test_utf8_accented_municipality_round_trip(self) -> None:
        with self._temporary_root() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.csv"
            gate = root / "gate.md"
            equivalencias = root / "equivalencias.md"
            output = root / "output"
            manifest.write_text(
                "municipio,bucket,site_base\n"
                "São José do Herval,processo_seletivo,https://prefeitura.test/\n",
                encoding="utf-8",
            )
            gate.write_text("validacao", encoding="utf-8")
            equivalencias.write_text("selecao = processo seletivo", encoding="utf-8")
            with runner.AuditLogger(output) as audit:
                files = runner.BlindFileAccess(
                    manifest=manifest,
                    gate=gate,
                    equivalencias=equivalencias,
                    output_dir=output,
                    audit=audit,
                )
                units, provenance = runner.load_inputs(files, manifest, gate, equivalencias)
                outcome = runner.NavigationOutcome(
                    result="REVISAR",
                    reason="teste_utf8",
                    final_path=[],
                    explored=[],
                    snapshots=[],
                    citations=[],
                    interaction_count=0,
                    additional_pages=0,
                )
                destination = output / runner.safe_unit_filename(units[0])
                files.atomic_write_json(
                    destination,
                    runner.outcome_payload(
                        units[0],
                        outcome,
                        "2026-07-14T00:00:00+00:00",
                        "2026-07-14T00:00:01+00:00",
                        provenance,
                    ),
                )
            decoded = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(decoded["municipio"], "São José do Herval")
            self.assertIn("São_José_do_Herval", destination.name)


if __name__ == "__main__":
    unittest.main()
