"""Offline contracts for the observable/resumable golden-live runtime."""

from __future__ import annotations

import csv
import io
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval.live_runtime import (
    PROGRESS_COLUMNS,
    EventLogger,
    LiveRunState,
    RunnerLock,
    RunnerLockError,
    canonical_json_bytes,
    normalize_unit,
)
from scripts.fase2_municipios.v2.eval.live_abc_adapter import OrionHTTPFetcher
from scripts.fase2_municipios.v2.eval.run_golden_live import run_golden_live
from scripts.fase2_municipios.v2.eval.tests.test_run_golden_live import (
    BUCKETS,
    FakeLiveAdapter,
    FixtureRun,
    MUNICIPIOS,
    _write_golden,
    _write_url_map,
    _write_v1_corpus,
)


pytestmark = pytest.mark.offline


class FlushSpy(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def _snapshot(municipio: str, bucket: str) -> dict:
    return {
        "schema_version": 1,
        "unit": {"municipio": municipio, "bucket": bucket},
        "sources": [{
            "source_id": "main",
            "url": "https://fixture.invalid/index",
            "retrieved_at": "2026-07-11T12:00:00+00:00",
            "content": "indice oficial",
        }],
    }


def _result(final: str, *, complete: bool = True, error_class: str = "") -> dict:
    return {
        "status": "complete" if complete else "error",
        "stage": "final",
        "model": "",
        "provider": "local",
        "start": "2026-07-11T12:00:00+00:00",
        "end": "2026-07-11T12:00:01+00:00",
        "duration_s": 1.0,
        "attempt": 1,
        "error_class": error_class,
        "error_message": "",
        "A": final,
        "B": final,
        "C": "not_invoked_consensus",
        "final": final,
        "quote": "indice oficial" if final != "revisar" else "",
        "source_id": "main" if final != "revisar" else "",
        "quote_start": 0 if final != "revisar" else "",
        "quote_end": 14 if final != "revisar" else "",
    }


def test_jsonl_is_visible_and_flushed_after_every_stage(tmp_path: Path) -> None:
    console = FlushSpy()
    log = tmp_path / "events.jsonl"
    logger = EventLogger(log, console=console)

    for stage in ("fetch", "A", "B", "juez", "final"):
        logger.emit(
            municipio="sao leopoldo",
            bucket="concurso_publico",
            stage=stage,
            model="fixture" if stage in {"A", "B", "juez"} else "",
            provider="fake",
            status="ok",
        )
        assert len(log.read_text(encoding="utf-8").splitlines()) == (
            ("fetch", "A", "B", "juez", "final").index(stage) + 1
        )

    logger.close()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["stage"] for row in rows] == ["fetch", "A", "B", "juez", "final"]
    assert console.flushes >= 5
    assert all({"ts", "municipio", "bucket", "stage", "model", "provider", "status"} <= set(row) for row in rows)


def test_progress_is_exact_atomic_and_durable_after_each_bucket(
    tmp_path: Path, monkeypatch
) -> None:
    state = LiveRunState(tmp_path)
    fsync_calls = []
    replace_calls = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    def replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    for bucket in ("concurso_publico", "processo_seletivo"):
        state.record_unit(
            municipio="São Leopoldo",
            bucket=bucket,
            url="https://fixture.invalid/index",
            result=_result("indice_oficial"),
            snapshot=_snapshot("sao leopoldo", bucket),
        )
        with state.progress_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == (1 if bucket == "concurso_publico" else 2)
        assert tuple(rows[0]) == PROGRESS_COLUMNS

    assert len(replace_calls) >= 4  # snapshot + checkpoint/progress per unit
    assert len(fsync_calls) >= len(replace_calls) * 2  # file and directory
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "final,error_class,complete",
    [
        ("revisar", "", True),
        ("indice_oficial", "timeout", False),
        ("indice_oficial", "transport_error", False),
        ("indice_oficial", "", False),
    ],
)
def test_resume_retries_review_error_timeout_and_incomplete(
    tmp_path: Path, final: str, error_class: str, complete: bool
) -> None:
    state = LiveRunState(tmp_path)
    state.record_unit(
        municipio="São Leopoldo",
        bucket="concurso_publico",
        url="https://fixture.invalid/index",
        result=_result(final, complete=complete, error_class=error_class),
        snapshot=_snapshot("sao leopoldo", "concurso_publico"),
    )
    resumed = LiveRunState(tmp_path, resume=True)
    assert resumed.should_skip(" São Leopoldo ", "concurso_publico") is False


def test_resume_skips_only_satisfactory_result_with_integral_snapshot(tmp_path: Path) -> None:
    state = LiveRunState(tmp_path)
    state.record_unit(
        municipio="São Leopoldo",
        bucket="concurso_publico",
        url="https://fixture.invalid/index",
        result=_result("indice_oficial"),
        snapshot=_snapshot("sao leopoldo", "concurso_publico"),
    )
    resumed = LiveRunState(tmp_path, resume=True)
    assert resumed.should_skip("SÃO LEOPOLDO", "concurso_publico") is True
    assert resumed.load_satisfactory_result("São Leopoldo", "concurso_publico")["final"] == "indice_oficial"


@pytest.mark.parametrize("corruption", ["truncated", "schema", "hash"])
def test_resume_retries_corrupt_incompatible_or_hash_mismatched_snapshot(
    tmp_path: Path, corruption: str
) -> None:
    state = LiveRunState(tmp_path)
    state.record_unit(
        municipio="São Leopoldo",
        bucket="concurso_publico",
        url="https://fixture.invalid/index",
        result=_result("indice_oficial"),
        snapshot=_snapshot("sao leopoldo", "concurso_publico"),
    )
    checkpoint = json.loads(state.checkpoint_path.read_text(encoding="utf-8"))
    record = next(iter(checkpoint["units"].values()))
    snapshot_path = tmp_path / record["snapshot_path"]
    if corruption == "truncated":
        snapshot_path.write_bytes(b'{"schema_version":1')
    elif corruption == "schema":
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        payload["schema_version"] = 999
        snapshot_path.write_bytes(canonical_json_bytes(payload))
        record["snapshot_hash"] = __import__("hashlib").sha256(snapshot_path.read_bytes()).hexdigest()
        state.checkpoint_path.write_bytes(canonical_json_bytes(checkpoint))
    else:
        record["snapshot_hash"] = "0" * 64
        state.checkpoint_path.write_bytes(canonical_json_bytes(checkpoint))

    assert LiveRunState(tmp_path, resume=True).should_skip(
        "São Leopoldo", "concurso_publico"
    ) is False


def test_normalized_tuple_is_the_single_unit_key_and_deduplicates() -> None:
    assert normalize_unit("  São  Leopoldo ", "concurso_publico") == (
        "saoleopoldo",
        "concurso_publico",
    )
    assert len({normalize_unit("São Leopoldo", "concurso_publico"), normalize_unit(" sao leopoldo ", "concurso_publico")}) == 1


def test_lock_exclusion_stale_same_host_reclaim_and_foreign_host_refusal(tmp_path: Path) -> None:
    path = tmp_path / "runner.lock"
    first = RunnerLock(path)
    first.acquire()
    with pytest.raises(RunnerLockError):
        RunnerLock(path).acquire()
    first.release()

    path.write_text(json.dumps({"pid": 999_999_999, "hostname": socket.gethostname(), "timestamp": "old"}), encoding="utf-8")
    reclaimed = RunnerLock(path, resume=True)
    reclaimed.acquire()
    reclaimed.release()

    path.write_text(json.dumps({"pid": 999_999_999, "hostname": "another-host", "timestamp": "old"}), encoding="utf-8")
    with pytest.raises(RunnerLockError, match="foreign_host"):
        RunnerLock(path, resume=True).acquire()


def test_http_connect_and_read_timeouts_are_distinct(monkeypatch) -> None:
    seen = {}

    class Sock:
        def settimeout(self, value):
            seen["read"] = value

    class Headers:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    class Response:
        status = 200
        headers = Headers()

        @staticmethod
        def read():
            return b"<html><title>fixture</title><body>fixture</body></html>"

        @staticmethod
        def getheader(name, default=None):
            return "text/html; charset=utf-8" if name == "content-type" else default

    class Connection:
        sock = Sock()

        @staticmethod
        def request(*_args, **_kwargs):
            return None

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    def connection(_parsed, timeout_seconds):
        seen["connect"] = timeout_seconds
        return Connection()

    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    fetcher = OrionHTTPFetcher(
        connect_timeout_seconds=1.25,
        read_timeout_seconds=2.5,
        clock=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    fetcher.fetch("https://fixture.invalid/index", timeout_seconds=99)
    assert seen == {"connect": 1.25, "read": 2.5}


def test_crash_mid_run_then_resume_skips_only_completed_integral_unit(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    golden = inputs / "golden.csv"
    url_map = inputs / "urls.csv"
    v1_dir = inputs / "v1"
    staging = tmp_path / "staging"
    output = staging / "run"
    _write_golden(golden)
    _write_url_map(url_map)
    _write_v1_corpus(v1_dir)
    first_adapter = None

    class CrashAdapter(FakeLiveAdapter):
        def request(self, municipio, bucket):
            if len(self.calls) == 1:
                raise KeyboardInterrupt("simulated crash")
            return super().request(municipio, bucket)

    def crash_factory(**kwargs):
        nonlocal first_adapter
        first_adapter = CrashAdapter(kwargs["target_urls"])
        return first_adapter

    with pytest.raises(KeyboardInterrupt):
        run_golden_live(
            golden_path=golden,
            url_map_path=url_map,
            v1_corpus_dir=v1_dir,
            output_dir=output,
            environ={"GEMINI_API_KEY_FREE": "offline"},
            staging_root=staging,
            fetcher_factory=lambda: object(),
            adapter_factory=crash_factory,
        )
    assert first_adapter is not None and first_adapter.calls == [
        (MUNICIPIOS[0], BUCKETS[0])
    ]
    assert not (output / "run_golden_live.lock").exists()
    assert len(list(csv.DictReader((output / "progress.csv").open(encoding="utf-8")))) == 1

    resumed_adapter = None

    def resume_factory(**kwargs):
        nonlocal resumed_adapter
        resumed_adapter = FakeLiveAdapter(kwargs["target_urls"])
        return resumed_adapter

    artifacts = run_golden_live(
        golden_path=golden,
        url_map_path=url_map,
        v1_corpus_dir=v1_dir,
        output_dir=output,
        environ={"GEMINI_API_KEY_FREE": "offline"},
        staging_root=staging,
        fetcher_factory=lambda: object(),
        adapter_factory=resume_factory,
        resume=True,
    )
    assert artifacts.cassette.is_file()
    assert resumed_adapter is not None
    assert (MUNICIPIOS[0], BUCKETS[0]) not in resumed_adapter.calls
    assert len(resumed_adapter.calls) == len(MUNICIPIOS) * len(BUCKETS) - 1
