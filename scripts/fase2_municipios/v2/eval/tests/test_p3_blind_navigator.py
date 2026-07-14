"""Offline contract tests for p3_blind_navigator.

Playwright is represented by FakeSession; these tests never make a network
request and do not require a browser process.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval import p3_blind_navigator as runner


class FakeSession:
    """Mock of the narrow Playwright session protocol used by the runner."""

    def __init__(self, states: dict[str, runner.PageState]) -> None:
        self.states = states
        self.visited: list[str] = []

    async def visit(self, url: str) -> runner.PageState:
        self.visited.append(url)
        return self.states[url]

    async def close(self) -> None:
        return None


def _state(
    url: str,
    text: str,
    links: tuple[runner.Link, ...] = (),
) -> runner.PageState:
    return runner.PageState(url=url, text=text, links=links, title="Prefeitura")


def _run_navigation(
    tmp_path: Path,
    states: dict[str, runner.PageState],
    *,
    bucket: str = "concurso_publico",
    municipio: str = "São José",
) -> tuple[runner.NavigationOutcome, FakeSession]:
    session = FakeSession(states)
    with runner.AuditLogger(tmp_path / "output") as audit:
        outcome = asyncio.run(
            runner.navigate_unit(
                session,
                runner.Unit(municipio, bucket, "https://prefeitura.test/"),
                audit,
            )
        )
    return outcome, session


def _item_page(url: str, number: str = "01") -> runner.PageState:
    return _state(
        url,
        "Publicações e resultados\n"
        f"Concurso Público nº {number}/2026 - Edital de abertura\n"
        "Concurso Público nº 02/2025 - Edital e anexos\n"
        "Filtrar por ano",
    )


def test_final_output_is_one_url_never_a_candidate_list(tmp_path: Path) -> None:
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

    outcome, _session = _run_navigation(tmp_path, states)

    assert outcome.result == "https://prefeitura.test/concursos"
    assert isinstance(outcome.result, str)
    assert not isinstance(outcome.result, list)
    assert outcome.reason is None
    assert outcome.citations


def test_genuine_ambiguity_returns_revisar(tmp_path: Path) -> None:
    states = {
        "https://prefeitura.test/": _state(
            "https://prefeitura.test/",
            "Portal municipal",
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

    outcome, _session = _run_navigation(tmp_path, states)

    assert outcome.result == "REVISAR"
    assert outcome.reason == "ambiguedad_entre_superficies_plausibles"
    assert not isinstance(outcome.result, list)


def test_internal_whitelist_and_hard_limits_are_respected(tmp_path: Path) -> None:
    root_links = tuple(
        runner.Link(f"Concursos {index}", f"/concursos-{index}")
        for index in range(10)
    ) + (runner.Link("Concursos externos", "https://evil.example/concursos"),)
    states: dict[str, runner.PageState] = {
        "https://prefeitura.test/": _state(
            "https://prefeitura.test/", "Portal", root_links
        )
    }
    for index in range(10):
        url = f"https://prefeitura.test/concursos-{index}"
        states[url] = _state(
            url,
            "Área de concursos sem itens",
            tuple(
                runner.Link(f"Editais {child}", f"/editais-{index}-{child}")
                for child in range(3)
            ),
        )
        for child in range(3):
            child_url = f"https://prefeitura.test/editais-{index}-{child}"
            states[child_url] = _state(child_url, "Nenhum resultado")

    outcome, session = _run_navigation(tmp_path, states)

    assert outcome.interaction_count <= runner.MAX_INTERACTIONS == 5
    assert outcome.additional_pages <= runner.MAX_ADDITIONAL_PAGES == 3
    assert len(session.visited) <= 1 + runner.MAX_ADDITIONAL_PAGES
    assert "https://evil.example/concursos" not in session.visited


def test_audit_log_records_every_declared_input_open(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    gate = tmp_path / "gate.md"
    equivalencias = tmp_path / "equivalencias.md"
    output = tmp_path / "output"
    manifest.write_text(
        "municipio,bucket,site_base\n"
        "São José,concurso_publico,https://prefeitura.test/\n",
        encoding="utf-8",
    )
    gate.write_text("# Gate\n", encoding="utf-8")
    equivalencias.write_text("# Equivalências\n", encoding="utf-8")

    with runner.AuditLogger(output) as audit:
        files = runner.BlindFileAccess(
            manifest=manifest,
            gate=gate,
            equivalencias=equivalencias,
            output_dir=output,
            audit=audit,
        )
        units, _provenance = runner.load_inputs(files, manifest, gate, equivalencias)

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
    assert allowed_reads == {manifest.resolve(), gate.resolve(), equivalencias.resolve()}
    assert units[0].municipio == "São José"


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("golden_set_v1.csv"),
        Path("url_map_secret.csv"),
        Path("staging") / "otra_corrida" / "resultado.json",
    ],
)
def test_forbidden_paths_raise_explicitly(
    tmp_path: Path, relative_path: Path
) -> None:
    manifest = tmp_path / "manifest.csv"
    gate = tmp_path / "gate.md"
    equivalencias = tmp_path / "equivalencias.md"
    output = tmp_path / "staging" / "corrida_actual"
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
        forbidden = tmp_path / relative_path
        with pytest.raises(runner.BlindAccessViolation):
            files.open(forbidden, "r")


def test_utf8_accented_municipality_round_trip(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    gate = tmp_path / "gate.md"
    equivalencias = tmp_path / "equivalencias.md"
    output = tmp_path / "output"
    manifest.write_text(
        "municipio,bucket,site_base\n"
        "São José do Herval,processo_seletivo,https://prefeitura.test/\n",
        encoding="utf-8",
    )
    gate.write_text("validação", encoding="utf-8")
    equivalencias.write_text("seleção = processo seletivo", encoding="utf-8")

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
                units[0], outcome, "2026-07-14T00:00:00+00:00", "2026-07-14T00:00:01+00:00", provenance
            ),
        )

    decoded = json.loads(destination.read_text(encoding="utf-8"))
    assert decoded["municipio"] == "São José do Herval"
    assert "São_José_do_Herval" in destination.name
