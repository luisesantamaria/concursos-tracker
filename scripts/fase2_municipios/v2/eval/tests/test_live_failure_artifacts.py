"""Offline contracts for stage evidence and incomplete-run diagnostics."""

from __future__ import annotations

import json
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCAdapter,
    LiveABCOutcome,
    LiveCause,
    LiveCauseKind,
)
from scripts.fase2_municipios.v2.agents.base import SnapshotInvalidOutput
from scripts.fase2_municipios.v2.eval.run_golden_live import (
    FINAL_FILENAMES,
    GoldenLiveIncompleteError,
    run_golden_live,
)

try:
    live_observability = importlib.import_module(
        "scripts.fase2_municipios.v2.eval.live_observability"
    )
except ModuleNotFoundError:
    live_observability = SimpleNamespace()


pytestmark = pytest.mark.offline
UNIT = ("Fixture Norte", "concurso_publico")


def _writer(tmp_path: Path):
    writer_type = getattr(live_observability, "StageArtifactWriter", None)
    assert writer_type is not None, "per-stage artifact writer is missing"
    return writer_type(tmp_path, max_snapshot_chars=128)


def _snapshot(secret: str = "") -> dict:
    content = "Official index " + ("x" * 300)
    return {
        "schema_version": 1,
        "unit": {"municipio": UNIT[0], "bucket": UNIT[1]},
        "sources": [{
            "source_id": "main",
            "url": f"https://user:pass@example.invalid/path?token={secret or 'query-secret'}",
            "retrieved_at": "2026-07-11T12:00:00+00:00",
            "content": content,
        }],
    }


def _load_only_artifact(root: Path) -> dict:
    paths = list((root / "observability").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_stage_artifact_distinguishes_no_raw_from_validation_failure(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.record_stage(
        unit=UNIT, attempt=1, stage="A", state="request_failed",
        snapshot=_snapshot(), error=TimeoutError("before response"),
    )
    first = _load_only_artifact(tmp_path)
    assert first["stages"]["A"]["state"] == "request_failed"
    assert "raw" not in first["stages"]["A"]
    assert first["evidence_snapshot"]["sources"]

    raw = {"decision": "indice_oficial", "citations": []}
    writer.record_stage(
        unit=UNIT, attempt=1, stage="B", state="validation_failed",
        snapshot=_snapshot(), raw=raw, error=ValueError("invalid output"),
    )
    second = _load_only_artifact(tmp_path)
    assert second["stages"]["B"]["state"] == "validation_failed"
    assert second["stages"]["B"]["raw"] == raw
    assert second["stages"]["C"]["state"] == "not_started"


def test_recursive_redaction_and_snapshot_bound_are_persisted(tmp_path: Path) -> None:
    secrets = ("header-secret", "proxy-secret", "bearer-secret", "nested-secret")
    writer = _writer(tmp_path)
    raw = {
        "headers": {"Authorization": "Bearer bearer-secret", "X-Api-Key": "header-secret"},
        "proxy": "http://proxy-user:proxy-secret@proxy.invalid:8080",
        "nested": [{"text": "prefix nested-secret suffix"}],
    }
    writer.record_stage(
        unit=UNIT, attempt=2, stage="A", state="raw_received",
        snapshot=_snapshot("query-secret"), raw=raw, redactions=secrets,
    )
    path = next((tmp_path / "observability").glob("*.json"))
    assert "--attempt-002.json" in path.name
    persisted_text = path.read_text(encoding="utf-8")
    for secret in (*secrets, "query-secret", "user:pass"):
        assert secret not in persisted_text
    artifact = json.loads(persisted_text)
    source = artifact["evidence_snapshot"]["sources"][0]
    assert source["content_truncated"] is True
    assert source["original_length"] > len(source["content_segments"][0]["text"])


def test_incomplete_tracker_schema_and_consumer_rejects_partial(tmp_path: Path) -> None:
    tracker_type = getattr(live_observability, "IncompleteRunTracker", None)
    assert tracker_type is not None, "incomplete-run tracker is missing"
    units = (
        ("Complete", "concurso_publico"),
        ("Failed", "processo_seletivo"),
        ("Pending", "concurso_publico"),
    )
    tracker = tracker_type(tmp_path, units)
    tracker.record_terminal(
        units[0], status="complete", decision="indice_oficial",
    )
    tracker.record_terminal(
        units[1], status="error", decision="revisar",
        revisar_por="revisar_por_B", stage="B",
        error_class="SnapshotInvalidOutput",
    )
    try:
        raise KeyboardInterrupt("offline interruption")
    except BaseException as exc:
        tracker.write_from_finally(exc)

    partial_path = tmp_path / live_observability.PARTIAL_FILENAME
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["schema_version"] == 1
    assert partial["incomplete"] is True
    assert partial["completed_units"] == [{"municipio": "Complete", "bucket": "concurso_publico"}]
    assert partial["failed_units"] == [{"municipio": "Failed", "bucket": "processo_seletivo"}]
    assert partial["pending_units"] == [{"municipio": "Pending", "bucket": "concurso_publico"}]
    assert partial["cause"]["code"] == "KeyboardInterrupt"
    assert partial["completed"] == [{
        "municipio": "Complete",
        "bucket": "concurso_publico",
        "decision": "indice_oficial",
        "revisar_por": "",
    }]
    assert partial["failed"] == [{
        "municipio": "Failed",
        "bucket": "processo_seletivo",
        "stage": "B",
        "error_class": "SnapshotInvalidOutput",
        "revisar_por": "revisar_por_B",
    }]
    assert partial["pending"] == [{
        "municipio": "Pending", "bucket": "concurso_publico",
    }]

    assert live_observability.is_publishable_artifact(partial) is False
    assert not any((tmp_path / name).exists() for name in (
        "golden_cassette.schema1.json", "differential.json", "differential.csv", "flips.json",
    ))


def test_terminal_review_does_not_make_run_incomplete(tmp_path: Path) -> None:
    tracker_type = getattr(live_observability, "IncompleteRunTracker", None)
    assert tracker_type is not None, "incomplete-run tracker is missing"
    tracker = tracker_type(tmp_path, (UNIT,))
    tracker.record_terminal(UNIT, status="error", decision="revisar")
    assert tracker.incomplete is False
    assert tracker.write_from_finally(None) is None


def test_real_adapter_persists_snapshot_and_raw_at_each_reached_stage(tmp_path: Path) -> None:
    from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as fixtures

    writer = _writer(tmp_path)
    adapter = LiveABCAdapter(
        fetcher=fixtures.FakeFetcher(),
        target_urls={(fixtures.MUNICIPIO, fixtures.BUCKET): fixtures.URL},
        certifier=fixtures.FakeCertifier(),
        prosecutor=fixtures.FakeProsecutor(),
        judge=fixtures.FakeJudge(),
        artifact_writer=writer,
    )

    outcome = adapter.request(fixtures.MUNICIPIO, fixtures.BUCKET)

    assert outcome.decision == "indice_oficial"
    artifact = _load_only_artifact(tmp_path)
    assert artifact["evidence_snapshot"]["sources"][0]["source_id"] == "main"
    assert artifact["stages"]["A"]["state"] == "raw_received"
    assert artifact["stages"]["B"]["state"] == "raw_received"
    assert artifact["stages"]["C"]["state"] == "skipped"
    assert artifact["stages"]["A"]["raw"]["decision"] == "indice_oficial"


def test_runner_links_failed_model_snapshot_and_raw_artifact_by_hash(
    tmp_path: Path,
    network_guard_spy,
) -> None:
    from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as live_fx
    from scripts.fase2_municipios.v2.eval.tests import test_run_golden_live as run_fx

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    golden = inputs / "golden.csv"
    url_map = inputs / "urls.csv"
    v1_dir = inputs / "v1"
    run_fx._write_golden(golden)
    run_fx._write_url_map(url_map)
    run_fx._write_v1_corpus(v1_dir)
    staging_root = tmp_path / "staging"
    output_dir = staging_root / "failed-model-observability"
    unit = (run_fx.MUNICIPIOS[0], run_fx.BUCKETS[0])

    def adapter_factory(**kwargs):
        return LiveABCAdapter(
            fetcher=live_fx.FakeFetcher(),
            target_urls=kwargs["target_urls"],
            certifier=live_fx.FakeCertifier(outcome=SnapshotInvalidOutput(
                role="certifier",
                code="citation_verification_failed",
                raw={"decision": "indice_oficial", "citations": []},
                original_exception=ValueError("offline invalid citation"),
            )),
            prosecutor=live_fx.FakeProsecutor(),
            judge=live_fx.FakeJudge(),
        )

    network_guard_spy.reset()
    with pytest.raises(GoldenLiveIncompleteError):
        run_golden_live(
            golden_path=golden,
            url_map_path=url_map,
            v1_corpus_dir=v1_dir,
            output_dir=output_dir,
            environ={"GEMINI_API_KEY_FREE": "offline-free"},
            staging_root=staging_root,
            unit_allowlist=(unit,),
            fetcher_factory=lambda: object(),
            adapter_factory=adapter_factory,
        )

    checkpoint = json.loads(
        (output_dir / "checkpoint.json").read_text(encoding="utf-8")
    )
    record = next(iter(checkpoint["units"].values()))
    assert record["snapshot_hash"]
    snapshot_path = output_dir / record["snapshot_path"]
    assert snapshot_path.is_file()
    assert record["result"]["observability_hash"]
    observability_path = output_dir / record["result"]["observability_path"]
    assert observability_path.is_file()
    assert network_guard_spy.blocked_attempts == 0


def test_typed_invocation_error_is_mapped_by_adapter_gate_only() -> None:
    from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as fixtures

    typed_error = SnapshotInvalidOutput(
        role="certifier", code="schema_mismatch", raw={"decision": "bad"}
    )
    outcome = fixtures._adapter(
        certifier=fixtures.FakeCertifier(outcome=typed_error)
    ).request(fixtures.MUNICIPIO, fixtures.BUCKET)

    assert not hasattr(typed_error, "decision")
    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.MODEL_FAILURE


def test_runner_finally_writes_partial_and_never_publishes_it(tmp_path: Path) -> None:
    from scripts.fase2_municipios.v2.eval.tests import test_run_golden_live as fixtures

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    golden = inputs / "golden.csv"
    url_map = inputs / "urls.csv"
    v1_dir = inputs / "v1"
    fixtures._write_golden(golden)
    fixtures._write_url_map(url_map)
    fixtures._write_v1_corpus(v1_dir)
    staging_root = tmp_path / "staging"
    output_dir = staging_root / "run-interrupted"
    targets = [
        (municipio, bucket)
        for municipio in fixtures.MUNICIPIOS for bucket in fixtures.BUCKETS
    ]

    class InterruptingAdapter(fixtures.FakeLiveAdapter):
        def request(self, municipio: str, bucket: str):
            unit = (municipio, bucket)
            if unit == targets[1]:
                outcome = LiveABCOutcome(
                    municipio=municipio,
                    bucket=bucket,
                    decision="revisar",
                    url="",
                    cause=LiveCause(
                        LiveCauseKind.MODEL_FAILURE,
                        "SnapshotInvalidOutput",
                        "fallo de Gemini free-only",
                        "revisar_por_A",
                    ),
                    layer=None,
                    original_exception=RuntimeError("offline invalid output"),
                )
                self.outcomes[unit] = outcome
                return outcome
            if unit == targets[2]:
                raise KeyboardInterrupt("offline interruption")
            return super().request(municipio, bucket)

    def adapter_factory(**kwargs):
        return InterruptingAdapter(kwargs["target_urls"])

    with pytest.raises(KeyboardInterrupt, match="offline interruption"):
        run_golden_live(
            golden_path=golden,
            url_map_path=url_map,
            v1_corpus_dir=v1_dir,
            output_dir=output_dir,
            environ={"GEMINI_API_KEY_FREE": "offline-free"},
            staging_root=staging_root,
            fetcher_factory=lambda: object(),
            adapter_factory=adapter_factory,
        )

    partial = json.loads(
        (output_dir / live_observability.PARTIAL_FILENAME).read_text(encoding="utf-8")
    )
    assert partial["incomplete"] is True
    assert partial["completed_units"] == [
        {"municipio": targets[0][0], "bucket": targets[0][1]}
    ]
    assert partial["failed_units"] == [
        {"municipio": targets[1][0], "bucket": targets[1][1]}
    ]
    assert partial["pending_units"] == [
        {"municipio": unit[0], "bucket": unit[1]} for unit in targets[2:]
    ]
    assert partial["cause"]["code"] == "KeyboardInterrupt"
    assert partial["completed"][0]["decision"] == "indice_oficial"
    assert partial["failed"] == [{
        "municipio": targets[1][0],
        "bucket": targets[1][1],
        "stage": "final",
        "error_class": "RuntimeError",
        "revisar_por": "revisar_por_A",
    }]
    assert not any((output_dir / filename).exists() for filename in FINAL_FILENAMES)
