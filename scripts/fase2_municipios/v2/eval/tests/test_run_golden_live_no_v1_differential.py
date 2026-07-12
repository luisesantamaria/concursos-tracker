"""Offline tests for ``--no-v1-differential`` (pure V2-vs-golden evaluation).

This mode opts the turnkey runner out of the V1 requirement entirely: the
live V2 acquisition/adjudication loop is unchanged, but no Run497V1Source or
CassetteProducer is built, no schema-1 cassette/differential/flips are
written, and a failed/blocked unit is *reported*, not a reason to abort the
whole run. Fixtures mirror ``test_run_golden_live.py`` (see also
``test_canary_units.py`` for the same reuse pattern).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCOutcome,
    LiveCause,
    LiveCauseKind,
)
from scripts.fase2_municipios.v2.eval.run_golden_live import (
    AUDIT_FILENAME,
    FINAL_FILENAMES,
    V1_DIFFERENTIAL_MODE,
    V2_ONLY_FILENAMES,
    V2_ONLY_MODE,
    GoldenLiveInputError,
    GoldenTargetCoverage,
    main,
    write_v2_only_differential,
)
from scripts.fase2_municipios.v2.eval.tests import test_run_golden_live as fixtures


pytestmark = pytest.mark.offline


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
        output_dir=staging_root / "run-no-v1",
        golden_bytes=fixtures._write_golden(golden),
        url_map_bytes=fixtures._write_url_map(url_map),
        v1_bytes={},
    )


def _argv_no_v1(
    run: fixtures.FixtureRun, *, v1_corpus_dir: Path | None = None
) -> list[str]:
    argv = [
        "--provider", "gemini_free",
        "--tools", "none",
        "--grounding", "off",
        "--golden", str(run.golden),
        "--url-map", str(run.url_map),
        "--output-dir", str(run.output_dir),
        "--seed", "0",
        "--no-v1-differential",
    ]
    if v1_corpus_dir is not None:
        argv.extend(["--v1-corpus-dir", str(v1_corpus_dir)])
    return argv


# --------------------------------------------------------------------------- #
# CLI end-to-end: the runner loop is unaffected, only the closing stage differs
# --------------------------------------------------------------------------- #


def test_runs_without_v1_corpus_dir_and_writes_v2_only_outputs(
    tmp_path: Path,
    network_guard_spy,
) -> None:
    network_guard_spy.reset()
    run = _fixture_run(tmp_path)
    calls, fetcher_factory, adapter_factory = fixtures._factories()

    code = main(
        _argv_no_v1(run),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    assert network_guard_spy.blocked_attempts == 0
    assert len(calls["adapters"][0].calls) == len(fixtures.MUNICIPIOS) * len(fixtures.BUCKETS)

    filenames = {item.name for item in run.output_dir.iterdir()}
    assert filenames == {
        *V2_ONLY_FILENAMES,
        AUDIT_FILENAME,
        "events.jsonl",
        "progress.csv",
        "checkpoint.json",
        "snapshots",
    }
    assert filenames.isdisjoint(FINAL_FILENAMES)

    document = json.loads(
        (run.output_dir / V2_ONLY_FILENAMES[0]).read_text(encoding="utf-8")
    )
    assert document["mode"] == V2_ONLY_MODE
    assert document["coverage"] == {"total": 4, "covered": 4, "sin_cobertura_v1": 0}
    assert len(document["rows"]) == 4
    for row in document["rows"]:
        assert row["golden_expectation"] == "confirm"
        assert row["v2_decision"] == "indice_oficial"
        assert row["v2_vs_golden"] == "match"
        assert row["cause_kind"] == "success"
        assert row["cause_code"] == "consensus"

    with (run.output_dir / V2_ONLY_FILENAMES[1]).open(
        encoding="utf-8", newline=""
    ) as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 4
    assert set(csv_rows[0]) == {
        "municipio", "bucket", "golden_expectation", "golden_urls",
        "v2_decision", "v2_url", "v2_vs_golden",
        "cause_kind", "cause_code", "revisar_por",
    }

    audit = json.loads((run.output_dir / AUDIT_FILENAME).read_text(encoding="utf-8"))
    assert audit["mode"] == V2_ONLY_MODE
    assert audit["complete"] is True
    assert audit["inputs"]["v1_manifest_sha256"] is None
    assert audit["producer_diagnostics"] == []
    assert audit["coverage"] == document["coverage"]


def test_units_missing_from_url_map_are_excluded_not_aborted(tmp_path: Path) -> None:
    run = _fixture_run(tmp_path)
    excluded = (fixtures.MUNICIPIOS[0], fixtures.BUCKETS[0])
    rows = [
        row for row in fixtures._url_map_rows(run.url_map)
        if (row["municipio"], row["bucket"]) != excluded
    ]
    fixtures._rewrite_url_map(run.url_map, rows=rows)
    calls, fetcher_factory, adapter_factory = fixtures._factories()

    code = main(
        _argv_no_v1(run),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    assert excluded not in calls["adapters"][0].calls
    assert len(calls["adapters"][0].calls) == 3

    document = json.loads(
        (run.output_dir / V2_ONLY_FILENAMES[0]).read_text(encoding="utf-8")
    )
    assert document["coverage"] == {"total": 4, "covered": 3, "sin_cobertura_v1": 1}
    assert document["sin_cobertura_v1"] == [{
        "municipio": excluded[0],
        "bucket": excluded[1],
        "executed": False,
        "motivo": "sin_cobertura_v1",
    }]
    assert "fuera del fixture url_map" in document["sin_cobertura_v1_note"]
    assert len(document["rows"]) == 3
    assert all((row["municipio"], row["bucket"]) != excluded for row in document["rows"])

    audit = json.loads((run.output_dir / AUDIT_FILENAME).read_text(encoding="utf-8"))
    assert audit["coverage"] == document["coverage"]
    assert audit["sin_cobertura_v1"] == document["sin_cobertura_v1"]
    assert audit["complete"] is True


def test_failed_unit_is_reported_not_used_to_abort_the_run(tmp_path: Path) -> None:
    run = _fixture_run(tmp_path)
    blocked = (fixtures.MUNICIPIOS[0], fixtures.BUCKETS[0])
    calls, fetcher_factory, adapter_factory = fixtures._factories(blocked_unit=blocked)

    code = main(
        _argv_no_v1(run),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    document = json.loads(
        (run.output_dir / V2_ONLY_FILENAMES[0]).read_text(encoding="utf-8")
    )
    assert len(document["rows"]) == 4
    failed_row = next(
        row for row in document["rows"]
        if (row["municipio"], row["bucket"]) == blocked
    )
    assert failed_row["v2_decision"] == "revisar"
    assert failed_row["v2_url"] == ""
    assert failed_row["cause_kind"] == "access_failure"
    assert failed_row["cause_code"] == "ExternalAccessBlocked"
    assert failed_row["v2_vs_golden"] == "differ"

    audit = json.loads((run.output_dir / AUDIT_FILENAME).read_text(encoding="utf-8"))
    assert audit["complete"] is True
    failed_unit = next(
        unit for unit in audit["units"]
        if (unit["municipio"], unit["bucket"]) == blocked
    )
    assert failed_unit["decision"] == "revisar"


def test_v1_corpus_dir_is_never_read_even_if_invalid(tmp_path: Path) -> None:
    """The flag opts out of V1 entirely: a nonexistent --v1-corpus-dir must
    not be validated or read when --no-v1-differential is set."""
    run = _fixture_run(tmp_path)
    calls, fetcher_factory, adapter_factory = fixtures._factories()

    code = main(
        _argv_no_v1(run, v1_corpus_dir=tmp_path / "does-not-exist"),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    assert (run.output_dir / V2_ONLY_FILENAMES[0]).exists()


def test_allow_sin_cobertura_v1_is_ignored_with_a_warning(
    tmp_path: Path, capsys
) -> None:
    run = _fixture_run(tmp_path)
    calls, fetcher_factory, adapter_factory = fixtures._factories()

    code = main(
        [*_argv_no_v1(run), "--allow-sin-cobertura-v1"],
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    stderr = capsys.readouterr().err
    assert "--allow-sin-cobertura-v1" in stderr
    assert "ignored" in stderr


# --------------------------------------------------------------------------- #
# Regression: default behaviour (no flag) is untouched
# --------------------------------------------------------------------------- #


def test_without_the_flag_v1_corpus_dir_stays_required_with_a_clear_error(
    tmp_path: Path,
) -> None:
    run = _fixture_run(tmp_path)
    argv = [
        "--provider", "gemini_free",
        "--tools", "none",
        "--grounding", "off",
        "--golden", str(run.golden),
        "--url-map", str(run.url_map),
        "--output-dir", str(run.output_dir),
        "--seed", "0",
    ]
    calls, fetcher_factory, adapter_factory = fixtures._factories()

    with pytest.raises(SystemExit) as exit_info:
        main(
            argv,
            environ={"GEMINI_API_KEY_FREE": "offline-free"},
            staging_root=run.staging_root,
            fetcher_factory=fetcher_factory,
            adapter_factory=adapter_factory,
        )

    assert exit_info.value.code == 2
    assert calls["fetcher"] == 0
    assert not run.output_dir.exists()


def test_default_mode_still_writes_schema1_cassette_and_no_v2_only_files(
    tmp_path: Path,
) -> None:
    run = _fixture_run(tmp_path)
    fixtures._write_v1_corpus(run.v1_dir)
    argv = [
        "--provider", "gemini_free",
        "--tools", "none",
        "--grounding", "off",
        "--golden", str(run.golden),
        "--url-map", str(run.url_map),
        "--v1-corpus-dir", str(run.v1_dir),
        "--output-dir", str(run.output_dir),
        "--seed", "0",
    ]
    calls, fetcher_factory, adapter_factory = fixtures._factories()

    code = main(
        argv,
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    filenames = {item.name for item in run.output_dir.iterdir()}
    assert set(FINAL_FILENAMES).issubset(filenames)
    assert filenames.isdisjoint(V2_ONLY_FILENAMES)
    audit = json.loads((run.output_dir / AUDIT_FILENAME).read_text(encoding="utf-8"))
    assert audit["mode"] == V1_DIFFERENTIAL_MODE
    assert isinstance(audit["inputs"]["v1_manifest_sha256"], str)


# --------------------------------------------------------------------------- #
# write_v2_only_differential: pure-function contract
# --------------------------------------------------------------------------- #


def test_write_v2_only_differential_pure_function_contract(tmp_path: Path) -> None:
    golden = tmp_path / "golden.csv"
    fixtures._write_golden(golden)
    target = (fixtures.MUNICIPIOS[0], fixtures.BUCKETS[0])
    url = fixtures._url(*target)
    outcome = LiveABCOutcome(
        municipio=target[0],
        bucket=target[1],
        decision="indice_oficial",
        url=url,
        cause=LiveCause(LiveCauseKind.SUCCESS, "consensus", "offline fixture"),
        layer=None,
    )
    coverage = GoldenTargetCoverage(
        total=1, covered=(target,), target_urls={target: url}, sin_cobertura_v1=(),
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    json_path, csv_path = write_v2_only_differential(
        targets=(target,),
        outcomes=[outcome],
        coverage=coverage,
        golden_path=golden,
        output_dir=output_dir,
    )

    assert json_path == output_dir / V2_ONLY_FILENAMES[0]
    assert csv_path == output_dir / V2_ONLY_FILENAMES[1]
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["rows"] == [{
        "municipio": target[0],
        "bucket": target[1],
        "golden_expectation": "confirm",
        "golden_urls": [url],
        "v2_decision": "indice_oficial",
        "v2_url": url,
        "v2_vs_golden": "match",
        "cause_kind": "success",
        "cause_code": "consensus",
        "revisar_por": "",
    }]
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{
        "municipio": target[0],
        "bucket": target[1],
        "golden_expectation": "confirm",
        "golden_urls": url,
        "v2_decision": "indice_oficial",
        "v2_url": url,
        "v2_vs_golden": "match",
        "cause_kind": "success",
        "cause_code": "consensus",
        "revisar_por": "",
    }]


def test_write_v2_only_differential_requires_an_outcome_for_every_target(
    tmp_path: Path,
) -> None:
    golden = tmp_path / "golden.csv"
    fixtures._write_golden(golden)
    target = (fixtures.MUNICIPIOS[0], fixtures.BUCKETS[0])
    coverage = GoldenTargetCoverage(
        total=1, covered=(target,), target_urls={}, sin_cobertura_v1=(),
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(GoldenLiveInputError, match="missing_outcome_for_target"):
        write_v2_only_differential(
            targets=(target,),
            outcomes=[],
            coverage=coverage,
            golden_path=golden,
            output_dir=output_dir,
        )
