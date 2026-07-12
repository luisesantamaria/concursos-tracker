"""Offline contracts for explicit unit filtering and the run1 canary selection."""

from __future__ import annotations

from pathlib import Path
import json
import random

import pytest

from scripts.fase2_municipios.v2.eval.run_golden_live import (
    GoldenLiveInputError,
    filter_golden_targets,
    main,
    parse_unit_specs,
)
from scripts.fase2_municipios.v2.eval.tests import test_run_golden_live as fixtures


pytestmark = pytest.mark.offline
CANARY_SEED = 2026071105
RUN1_RESIDUE = """
Aceguá|concurso_publico|transport_error|OSError|fetch|atende_net
Aceguá|processo_seletivo|transport_error|OSError|fetch|atende_net
Almirante Tamandaré do Sul|concurso_publico|semantic_error|InvalidAgentStepError/tool_forbids_output|A|rs_gov_br
Almirante Tamandaré do Sul|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Alvorada|concurso_publico|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Alvorada|processo_seletivo|timeout|PolicyCallError|A|rs_gov_br
Arambaré|concurso_publico|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Arambaré|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Anta Gorda|concurso_publico|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Anta Gorda|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Aratiba|concurso_publico|semantic_error|AgentLoopLimitError|A|rs_gov_br
Aratiba|processo_seletivo|semantic_error|InvalidAgentStepError/tool_forbids_output|A|rs_gov_br
Bagé|concurso_publico|semantic_error|AgentLoopLimitError|A|rs_gov_br
Bagé|processo_seletivo|semantic_error|InvalidAgentStepError/tool_forbids_output|A|rs_gov_br
Bento Gonçalves|concurso_publico|timeout|PolicyCallError|A|oxy_elotech
Bento Gonçalves|processo_seletivo|timeout|PolicyCallError|A|oxy_elotech
Canoas|concurso_publico|timeout|PolicyCallError|A|rs_gov_br
Canoas|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Caxias do Sul|concurso_publico|semantic_error|InvalidAgentStepError/tool_forbids_output|A|rs_gov_br
Caxias do Sul|processo_seletivo|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Gramado|concurso_publico|semantic_error|AgentLoopLimitError|A|atende_net
Gramado|processo_seletivo|semantic_error|InvalidAgentStepError/tool_forbids_output|A|atende_net
Gravataí|concurso_publico|evidence_insufficient|LiveFetchError|fetch|atende_net
Gravataí|processo_seletivo|evidence_insufficient|LiveFetchError|fetch|atende_net
Itaara|concurso_publico|semantic_error|InvalidAgentStepError/tool_forbids_output|A|rs_gov_br
Itaara|processo_seletivo|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Itaqui|concurso_publico|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Itaqui|processo_seletivo|semantic_error|InvalidAgentStepError/tool_forbids_output|A|rs_gov_br
Novo Hamburgo|concurso_publico|semantic_error|AgentLoopLimitError|A|rs_gov_br
Novo Hamburgo|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Passo Fundo|concurso_publico|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Passo Fundo|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Pelotas|concurso_publico|semantic_error|AgentLoopLimitError|A|rs_gov_br
Pelotas|processo_seletivo|semantic_error|AgentLoopLimitError|A|rs_gov_br
Porto Alegre|concurso_publico|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
Porto Alegre|processo_seletivo|evidence_insufficient|LiveFetchError|fetch|rs_gov_br
""".strip()


def _run1_units() -> list[dict[str, str]]:
    fields = ("municipio", "bucket", "status", "error", "phase", "family")
    return [dict(zip(fields, line.split("|"), strict=True)) for line in RUN1_RESIDUE.splitlines()]


def _select_canary(seed: int) -> tuple[dict[str, str], ...]:
    units = _run1_units()
    rng = random.Random(seed)

    def choose(predicate, selected):
        candidates = sorted(
            (unit for unit in units if predicate(unit) and unit not in selected),
            key=lambda unit: (unit["municipio"].casefold(), unit["bucket"]),
        )
        rng.shuffle(candidates)
        assert candidates
        return candidates[0]

    selected: list[dict[str, str]] = []
    selected.append(choose(lambda unit: unit["error"] == "AgentLoopLimitError", selected))
    selected.append(choose(
        lambda unit: unit["error"] == "InvalidAgentStepError/tool_forbids_output",
        selected,
    ))
    selected.append(choose(lambda unit: unit["status"] == "timeout", selected))
    selected.append(choose(lambda unit: unit["status"] == "evidence_insufficient", selected))
    selected.append(choose(
        lambda unit: unit["family"] != "rs_gov_br"
        and unit["status"] != "semantic_error",
        selected,
    ))
    return tuple(selected)


def _fixture_run(tmp_path: Path) -> fixtures.FixtureRun:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    golden = inputs / "golden.csv"
    url_map = inputs / "urls.csv"
    v1_dir = inputs / "v1"
    staging_root = tmp_path / "staging"
    return fixtures.FixtureRun(
        golden=golden,
        url_map=url_map,
        v1_dir=v1_dir,
        staging_root=staging_root,
        output_dir=staging_root / "run-units",
        golden_bytes=fixtures._write_golden(golden),
        url_map_bytes=fixtures._write_url_map(url_map),
        v1_bytes=fixtures._write_v1_corpus(v1_dir),
    )


def test_units_allowlist_executes_exact_pairs_without_sibling_buckets(
    tmp_path: Path,
    network_guard_spy,
) -> None:
    run = _fixture_run(tmp_path)
    selected = (
        (fixtures.MUNICIPIOS[0], fixtures.BUCKETS[1]),
        (fixtures.MUNICIPIOS[1], fixtures.BUCKETS[0]),
    )
    calls, fetcher_factory, adapter_factory = fixtures._factories()
    argv = fixtures._argv(run)
    for municipio, bucket in selected:
        argv.extend(("--units", f"{municipio}:{bucket}"))

    code = main(
        argv,
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    assert calls["adapters"][0].calls == list(selected)
    assert set(calls["adapters"][0].target_urls) == set(selected)
    audit = json.loads(
        (run.output_dir / fixtures.AUDIT_FILENAME).read_text(encoding="utf-8")
    )
    assert audit["coverage"] == {
        "total": len(selected), "covered": len(selected), "sin_cobertura_v1": 0,
    }
    assert audit["sin_cobertura_v1"] == []
    assert network_guard_spy.blocked_attempts == 0


def test_unit_parser_and_filter_fail_closed_without_changing_default() -> None:
    universe = tuple(
        (municipio, bucket)
        for municipio in fixtures.MUNICIPIOS for bucket in fixtures.BUCKETS
    )
    assert filter_golden_targets(universe, None) == universe
    parsed = parse_unit_specs([f"{fixtures.MUNICIPIOS[0]}:{fixtures.BUCKETS[1]}"])
    assert filter_golden_targets(universe, parsed) == parsed
    with pytest.raises(GoldenLiveInputError, match="requested_unit_not_in_golden"):
        filter_golden_targets(universe, (("Missing", fixtures.BUCKETS[0]),))
    with pytest.raises(GoldenLiveInputError, match="invalid_unit_spec_at"):
        parse_unit_specs(["Fixture Norte:wrong_bucket"])


@pytest.mark.parametrize("allow_missing", (False, True))
def test_requested_unit_missing_from_url_map_never_becomes_an_exclusion(
    tmp_path: Path,
    allow_missing: bool,
) -> None:
    run = _fixture_run(tmp_path)
    selected = (fixtures.MUNICIPIOS[0], fixtures.BUCKETS[1])
    rows = [
        row for row in fixtures._url_map_rows(run.url_map)
        if (row["municipio"], row["bucket"]) != selected
    ]
    fixtures._rewrite_url_map(run.url_map, rows=rows)
    calls, fetcher_factory, adapter_factory = fixtures._factories()
    argv = [*fixtures._argv(run), "--units", f"{selected[0]}:{selected[1]}"]
    if allow_missing:
        argv.append("--allow-sin-cobertura-v1")

    code = main(
        argv,
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 2
    assert calls["fetcher"] == 0
    assert not run.output_dir.exists()


def test_run1_canary_is_deterministic_representative_and_maps_to_five_units() -> None:
    first = _select_canary(CANARY_SEED)
    second = _select_canary(CANARY_SEED)

    assert first == second
    assert len(first) == 5
    assert [(item["municipio"], item["bucket"]) for item in first] == [
        ("Anta Gorda", "processo_seletivo"),
        ("Itaara", "concurso_publico"),
        ("Bento Gonçalves", "processo_seletivo"),
        ("Arambaré", "concurso_publico"),
        ("Aceguá", "concurso_publico"),
    ]
    semantic_a = [
        item for item in first
        if item["status"] == "semantic_error" and item["phase"] == "A"
    ]
    assert len(semantic_a) >= 2
    assert {item["error"] for item in semantic_a} >= {
        "AgentLoopLimitError", "InvalidAgentStepError/tool_forbids_output",
    }
    assert any(item["status"] == "timeout" for item in first)
    assert any(
        item["status"] == "evidence_insufficient"
        and item["error"] == "LiveFetchError"
        for item in first
    )
    assert any(item["family"] != "rs_gov_br" for item in first)
    assert all(item["status"] != "sin_cobertura_v1" for item in first)

    universe = tuple(
        (item["municipio"], item["bucket"]) for item in _run1_units()
    )
    allowlist = tuple((item["municipio"], item["bucket"]) for item in first)
    assert filter_golden_targets(universe, allowlist) == allowlist
