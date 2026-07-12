"""Offline end-to-end tests for the turnkey golden live CLI."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval.cassette_producer import (
    ABCLayer,
    CandidateLayer,
    CitationLayer,
    EvidenceLayer,
    ProposalLayer,
    SourceLayer,
)
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCOutcome,
    LiveCause,
    LiveCauseKind,
)
from scripts.fase2_municipios.v2.eval.run_golden_live import (
    AUDIT_FILENAME,
    FINAL_FILENAMES,
    GoldenLiveInputError,
    golden_targets,
    load_url_map,
    main,
)
from scripts.fase2_municipios.v2.eval.golden_runner import GoldenDifferentialRunner


pytestmark = pytest.mark.offline
MUNICIPIOS = ("Fixture Norte", "Fixture Sul")
BUCKETS = ("concurso_publico", "processo_seletivo")


def _url(municipio: str, bucket: str) -> str:
    slug = municipio.lower().replace(" ", "-")
    return f"https://{slug}.rs.gov.br/{bucket}"


def _write_golden(path: Path) -> bytes:
    fieldnames = [
        "municipio",
        "tipo",
        "site_base",
        "url_concursos",
        "url_processos_seletivos",
        "urls_concursos_extra",
        "urls_processos_extra",
        "requiere_revision_humana",
        "notas",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for municipio in MUNICIPIOS:
            writer.writerow({
                "municipio": municipio,
                "tipo": "fixture",
                "site_base": _url(municipio, "site"),
                "url_concursos": _url(municipio, "concurso_publico"),
                "url_processos_seletivos": _url(municipio, "processo_seletivo"),
                "requiere_revision_humana": "no",
                "notas": "offline",
            })
    return path.read_bytes()


def _write_url_map(path: Path) -> bytes:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("municipio", "bucket", "url"),
            lineterminator="\n",
        )
        writer.writeheader()
        for municipio in MUNICIPIOS:
            for bucket in BUCKETS:
                writer.writerow({
                    "municipio": municipio,
                    "bucket": bucket,
                    "url": _url(municipio, bucket),
                })
    return path.read_bytes()


def _write_v1_corpus(path: Path) -> dict[str, bytes]:
    path.mkdir()
    original = {}
    for index, municipio in enumerate(MUNICIPIOS):
        for bucket in BUCKETS:
            target = path / f"{index}-{bucket}.json"
            target.write_text(
                json.dumps({
                    "municipio": municipio,
                    "bucket": bucket,
                    "decision": "indice_oficial",
                    "url": _url(municipio, bucket),
                    "evidence": {
                        "snapshot_ref": f"v1:{index}:{bucket}",
                        "authority": "confirmada",
                        "identity": "confirmada",
                        "reason": "offline fixture",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            original[target.name] = target.read_bytes()
    return original


def _layer(municipio: str, bucket: str) -> ABCLayer:
    url = _url(municipio, bucket)
    content = f"Official index for {municipio} {bucket}"
    citation = CitationLayer("main", 0, len(content), content)
    candidate_id = f"candidate-{municipio}-{bucket}"
    proposal = ProposalLayer(
        decision="indice_oficial",
        bucket=bucket,
        candidate_id=candidate_id,
        resource_url=url,
        citations=(citation,),
        reason="offline fixture",
    )
    return ABCLayer(
        evidence=EvidenceLayer(
            snapshot_ref=f"v2:{municipio}:{bucket}",
            authority="confirmada",
            identity="confirmada",
            reason="offline fixture",
        ),
        sources=(SourceLayer(
            source_id="main",
            url=url,
            retrieved_at="2026-07-11T12:00:00+00:00",
            content=content,
        ),),
        citations=(citation,),
        candidate=CandidateLayer(
            candidate_id=candidate_id,
            url=url,
            decision="indice_oficial",
            bucket=bucket,
            authority="confirmada",
            identity="confirmada",
            evidence_state="completa",
            source_kind="fixture_official",
        ),
        proposal_a=proposal,
        proposal_b=proposal,
        judge_response={"decision": "revisar", "reason": "not_invoked_consensus"},
    )


class FakeLiveAdapter:
    def __init__(self, target_urls, *, blocked_unit=None) -> None:
        self.target_urls = dict(target_urls)
        self.blocked_unit = blocked_unit
        self.calls = []
        self.outcomes = {}

    def request(self, municipio: str, bucket: str) -> LiveABCOutcome:
        unit = (municipio, bucket)
        if unit in self.outcomes:
            return self.outcomes[unit]
        self.calls.append(unit)
        if unit == self.blocked_unit:
            outcome = LiveABCOutcome(
                municipio=municipio,
                bucket=bucket,
                decision="revisar",
                url="",
                cause=LiveCause(
                    LiveCauseKind.ACCESS_FAILURE,
                    "ExternalAccessBlocked",
                    "no se pudo acceder",
                ),
                layer=None,
                original_exception=RuntimeError("blocked fixture"),
            )
        else:
            layer = _layer(municipio, bucket)
            outcome = LiveABCOutcome(
                municipio=municipio,
                bucket=bucket,
                decision="indice_oficial",
                url=self.target_urls[unit],
                cause=LiveCause(
                    LiveCauseKind.SUCCESS,
                    "consensus",
                    "offline fixture",
                ),
                layer=layer,
            )
        self.outcomes[unit] = outcome
        return outcome

    def get(self, municipio: str, bucket: str) -> ABCLayer | None:
        return self.request(municipio, bucket).layer


@dataclass
class FixtureRun:
    golden: Path
    url_map: Path
    v1_dir: Path
    staging_root: Path
    output_dir: Path
    golden_bytes: bytes
    url_map_bytes: bytes
    v1_bytes: dict[str, bytes]


@pytest.fixture
def fixture_run(tmp_path: Path) -> FixtureRun:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    golden = inputs / "golden.csv"
    url_map = inputs / "urls.csv"
    v1_dir = inputs / "v1"
    staging_root = tmp_path / "staging"
    return FixtureRun(
        golden=golden,
        url_map=url_map,
        v1_dir=v1_dir,
        staging_root=staging_root,
        output_dir=staging_root / "run-001",
        golden_bytes=_write_golden(golden),
        url_map_bytes=_write_url_map(url_map),
        v1_bytes=_write_v1_corpus(v1_dir),
    )


def _argv(run: FixtureRun) -> list[str]:
    return [
        "--provider", "gemini_free",
        "--tools", "none",
        "--grounding", "off",
        "--golden", str(run.golden),
        "--url-map", str(run.url_map),
        "--v1-corpus-dir", str(run.v1_dir),
        "--output-dir", str(run.output_dir),
        "--seed", "0",
    ]


def _factories(*, blocked_unit=None):
    calls = {"fetcher": 0, "adapter_kwargs": [], "adapters": []}

    class FakeFetcher:
        pass

    def fetcher_factory():
        calls["fetcher"] += 1
        return FakeFetcher()

    def adapter_factory(**kwargs):
        calls["adapter_kwargs"].append(kwargs)
        adapter = FakeLiveAdapter(
            kwargs["target_urls"],
            blocked_unit=blocked_unit,
        )
        calls["adapters"].append(adapter)
        return adapter

    return calls, fetcher_factory, adapter_factory


def _rewrite_url_map(
    path: Path,
    *,
    rows: list[dict[str, str]],
    fieldnames=("municipio", "bucket", "url"),
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _url_map_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_turnkey_complete_run_writes_cassette_differential_and_flips_only_to_staging(
    fixture_run: FixtureRun,
    network_guard_spy,
) -> None:
    network_guard_spy.reset()
    calls, fetcher_factory, adapter_factory = _factories()

    code = main(
        _argv(fixture_run),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=fixture_run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    assert network_guard_spy.blocked_attempts == 0
    assert calls["fetcher"] == 1
    assert len(calls["adapters"][0].calls) == len(MUNICIPIOS) * len(BUCKETS)
    assert {item.name for item in fixture_run.output_dir.iterdir()} == {
        *FINAL_FILENAMES,
        AUDIT_FILENAME,
        "events.jsonl",
        "progress.csv",
        "checkpoint.json",
        "snapshots",
    }
    cassette = json.loads(
        (fixture_run.output_dir / FINAL_FILENAMES[0]).read_text(encoding="utf-8")
    )
    differential = json.loads(
        (fixture_run.output_dir / FINAL_FILENAMES[1]).read_text(encoding="utf-8")
    )
    flips = json.loads(
        (fixture_run.output_dir / FINAL_FILENAMES[3]).read_text(encoding="utf-8")
    )
    assert len(cassette["cases"]) == 4
    assert len(differential["rows"]) == 4
    assert len(flips["flips"]) == 4
    assert fixture_run.golden.read_bytes() == fixture_run.golden_bytes
    assert fixture_run.url_map.read_bytes() == fixture_run.url_map_bytes
    assert {
        path.name: path.read_bytes() for path in fixture_run.v1_dir.glob("*.json")
    } == fixture_run.v1_bytes


@pytest.mark.parametrize(
    "environment",
    [{}, {"GEMINI_API_KEY": "paid-must-not-be-read"}],
    ids=("free_absent", "paid_only"),
)
def test_credentials_fail_fast_before_fetch_adapter_or_write(
    fixture_run: FixtureRun,
    environment,
) -> None:
    calls, fetcher_factory, adapter_factory = _factories()

    code = main(
        _argv(fixture_run),
        environ=environment,
        staging_root=fixture_run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 2
    assert calls["fetcher"] == 0
    assert calls["adapter_kwargs"] == []
    assert not fixture_run.output_dir.exists()


def test_blocked_unit_is_audited_as_access_failure_and_other_units_continue(
    fixture_run: FixtureRun,
) -> None:
    blocked = (MUNICIPIOS[0], BUCKETS[0])
    calls, fetcher_factory, adapter_factory = _factories(blocked_unit=blocked)

    code = main(
        _argv(fixture_run),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=fixture_run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 2
    assert len(calls["adapters"][0].calls) == 4
    audit = json.loads(
        (fixture_run.output_dir / AUDIT_FILENAME).read_text(encoding="utf-8")
    )
    failed = next(
        unit for unit in audit["units"]
        if (unit["municipio"], unit["bucket"]) == blocked
    )
    assert failed["decision"] == "revisar"
    assert failed["cause"]["kind"] == "access_failure"
    assert failed["cause"]["comment"] == "no se pudo acceder"
    assert not any(
        (fixture_run.output_dir / filename).exists() for filename in FINAL_FILENAMES
    )


def test_adapter_invocation_has_no_key_grounding_or_tools_kwargs(
    fixture_run: FixtureRun,
) -> None:
    calls, fetcher_factory, adapter_factory = _factories()

    code = main(
        _argv(fixture_run),
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=fixture_run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 0
    assert len(calls["adapter_kwargs"]) == 1
    kwargs = calls["adapter_kwargs"][0]
    assert {"api_key", "tools", "grounding"}.isdisjoint(kwargs)
    assert set(kwargs) == {"fetcher", "target_urls", "environ", "timeout_seconds"}


def test_output_outside_injected_staging_root_fails_before_adapter(
    fixture_run: FixtureRun,
    tmp_path: Path,
) -> None:
    calls, fetcher_factory, adapter_factory = _factories()
    argv = _argv(fixture_run)
    argv[argv.index("--output-dir") + 1] = str(tmp_path / "outside")

    code = main(
        argv,
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=fixture_run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
    )

    assert code == 2
    assert calls["fetcher"] == 0
    assert calls["adapter_kwargs"] == []
    assert not (tmp_path / "outside").exists()


def test_missing_url_map_unit_default_off_stays_fail_closed(
    fixture_run: FixtureRun,
) -> None:
    rows = _url_map_rows(fixture_run.url_map)
    _rewrite_url_map(fixture_run.url_map, rows=rows[1:])

    with pytest.raises(GoldenLiveInputError, match="url_map_missing_units:1"):
        load_url_map(
            fixture_run.url_map,
            golden_targets(fixture_run.golden),
        )


def test_allow_sin_cobertura_v1_executes_only_covered_and_traces_exclusions(
    fixture_run: FixtureRun,
) -> None:
    excluded = {
        (MUNICIPIOS[1], BUCKETS[1]),
        (MUNICIPIOS[0], BUCKETS[0]),
    }
    rows = [
        row for row in _url_map_rows(fixture_run.url_map)
        if (row["municipio"], row["bucket"]) not in excluded
    ]
    _rewrite_url_map(fixture_run.url_map, rows=rows)
    calls, fetcher_factory, adapter_factory = _factories()
    judge_calls = []

    class RecordingModelAdapter:
        def response_for(self, case):
            judge_calls.append((case["municipio"], case["bucket"]))
            return dict(case["v2"]["judge_response"])

    def runner_factory(*, seed):
        return GoldenDifferentialRunner(seed=seed, model_adapter=RecordingModelAdapter())

    code = main(
        [*_argv(fixture_run), "--allow-sin-cobertura-v1"],
        environ={"GEMINI_API_KEY_FREE": "offline-free"},
        staging_root=fixture_run.staging_root,
        fetcher_factory=fetcher_factory,
        adapter_factory=adapter_factory,
        differential_runner_factory=runner_factory,
    )

    assert code == 0
    expected_covered = {
        (municipio, bucket)
        for municipio in MUNICIPIOS for bucket in BUCKETS
    } - excluded
    assert set(calls["adapters"][0].calls) == expected_covered
    assert set(judge_calls) == expected_covered
    assert excluded.isdisjoint(calls["adapters"][0].calls)
    assert excluded.isdisjoint(judge_calls)

    cassette = json.loads(
        (fixture_run.output_dir / FINAL_FILENAMES[0]).read_text(encoding="utf-8")
    )
    differential = json.loads(
        (fixture_run.output_dir / FINAL_FILENAMES[1]).read_text(encoding="utf-8")
    )
    flips = json.loads(
        (fixture_run.output_dir / FINAL_FILENAMES[3]).read_text(encoding="utf-8")
    )
    audit = json.loads(
        (fixture_run.output_dir / AUDIT_FILENAME).read_text(encoding="utf-8")
    )
    canonical_exclusions = [
        {
            "municipio": MUNICIPIOS[0],
            "bucket": BUCKETS[0],
            "executed": False,
            "motivo": "sin_cobertura_v1",
        },
        {
            "municipio": MUNICIPIOS[1],
            "bucket": BUCKETS[1],
            "executed": False,
            "motivo": "sin_cobertura_v1",
        },
    ]
    summary = {"total": 4, "covered": 2, "sin_cobertura_v1": 2}
    for artifact in (cassette, differential, flips, audit):
        assert artifact["coverage"] == summary
        assert artifact["sin_cobertura_v1"] == canonical_exclusions
        assert len({(row["municipio"], row["bucket"]) for row in artifact["sin_cobertura_v1"]}) == 2
    assert len(cassette["cases"]) == 2
    assert len(differential["rows"]) == 2
    assert len(flips["flips"]) == 2
    assert excluded.isdisjoint(
        (row["municipio"], row["bucket"]) for row in differential["rows"]
    )
    assert excluded.isdisjoint(
        (row["municipio"], row["bucket"]) for row in flips["flips"]
    )
    assert all(
        metrics["fpos"] == 0
        for system in differential["golden_metrics"].values()
        for metrics in system.values()
    )


@pytest.mark.parametrize("allow_partial", (False, True), ids=("flag_off", "flag_on"))
@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("extra", "unexpected_url_map_unit_at_row"),
        ("duplicate", "duplicate_url_map_unit_at_row"),
        ("invalid_url", "invalid_url_map_url_at_row"),
        ("columns", "url_map_columns_must_be"),
    ],
)
def test_map_corruption_is_always_hard_error(
    fixture_run: FixtureRun,
    allow_partial: bool,
    corruption: str,
    message: str,
) -> None:
    rows = _url_map_rows(fixture_run.url_map)
    fieldnames = ("municipio", "bucket", "url")
    if corruption == "extra":
        rows.append({
            "municipio": "Fuera Golden",
            "bucket": BUCKETS[0],
            "url": "https://fuera-golden.rs.gov.br/concursos",
        })
    elif corruption == "duplicate":
        rows.append(dict(rows[0]))
    elif corruption == "invalid_url":
        rows[0]["url"] = "not-a-url"
    elif corruption == "columns":
        fieldnames = ("municipio", "bucket", "wrong_url")
        rows = [
            {"municipio": row["municipio"], "bucket": row["bucket"], "wrong_url": row["url"]}
            for row in rows
        ]
    _rewrite_url_map(fixture_run.url_map, rows=rows, fieldnames=fieldnames)

    with pytest.raises(GoldenLiveInputError, match=message):
        load_url_map(
            fixture_run.url_map,
            golden_targets(fixture_run.golden),
            allow_sin_cobertura_v1=allow_partial,
        )


def test_allow_sin_cobertura_v1_rejects_zero_covered_units(
    fixture_run: FixtureRun,
) -> None:
    _rewrite_url_map(fixture_run.url_map, rows=[])

    with pytest.raises(GoldenLiveInputError, match="no_covered_units"):
        load_url_map(
            fixture_run.url_map,
            golden_targets(fixture_run.golden),
            allow_sin_cobertura_v1=True,
        )
