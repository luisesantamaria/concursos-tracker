"""Durable local observability and resume state for ``run_golden_live``.

The stable work-unit key is ``(muni_key(municipio), bucket)``.  ``muni_key`` is
the evaluator's existing lower-case, accent-insensitive, non-alphanumeric
normalization.  Repeated input rows with the same tuple represent one unit and
must not create a second progress row.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from scripts.eval import medir_golden_set as golden_evaluator


SNAPSHOT_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1
PROGRESS_COLUMNS = (
    "municipio", "bucket", "url", "status", "stage", "model", "provider",
    "start", "end", "duration_s", "attempt", "error_class", "error_message",
    "snapshot_hash", "A", "B", "C", "final", "quote", "source_id",
    "quote_start", "quote_end",
)
CONFIRMING_DECISIONS = frozenset({
    "indice_oficial", "indice_oficial_combinado", "portal_externo_oficial",
})


class LiveRuntimeError(RuntimeError):
    """Secret-free local runtime failure."""


class RunnerLockError(LiveRuntimeError):
    """Exclusive runner lock could not be acquired safely."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_unit(municipio: str, bucket: str) -> tuple[str, str]:
    """Return the one canonical unit tuple used by logs, CSV and checkpoint."""

    if not isinstance(municipio, str) or not municipio.strip():
        raise ValueError("municipio_must_be_nonempty")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("bucket_must_be_nonempty")
    return golden_evaluator.muni_key(municipio.strip()), bucket.strip()


def unit_storage_key(municipio: str, bucket: str) -> str:
    return json.dumps(normalize_unit(municipio, bucket), ensure_ascii=False, separators=(",", ":"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_durable_write(path: Path, payload: bytes) -> None:
    """Unique same-filesystem temp, file fsync, replace, then directory fsync."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


class EventLogger:
    """Append-only JSONL plus immediately flushed local console output."""

    def __init__(
        self,
        path: Path,
        *,
        console: TextIO | None = None,
        redactions: tuple[str, ...] = (),
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self.console = sys.stdout if console is None else console
        self.redactions = tuple(value for value in redactions if value)

    def _safe(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        safe = value.replace("\r", " ").replace("\n", " ")
        for secret in self.redactions:
            safe = safe.replace(secret, "[REDACTED]")
        return safe

    def emit(
        self,
        *,
        municipio: str,
        bucket: str,
        stage: str,
        model: str,
        provider: str,
        status: str,
        error_class: str = "",
        error_message: str = "",
        **extra: Any,
    ) -> None:
        normalized_municipio, normalized_bucket = normalize_unit(municipio, bucket)
        event = {
            "ts": utc_now_iso(),
            "municipio": normalized_municipio,
            "bucket": normalized_bucket,
            "stage": stage,
            "model": model,
            "provider": provider,
            "status": status,
        }
        if status == "error" or error_class or error_message:
            event["error_class"] = error_class
            event["error_message"] = self._safe(error_message)
        event.update({key: self._safe(value) for key, value in extra.items()})
        self._handle.write(canonical_json_bytes(event).decode("utf-8") + "\n")
        self._handle.flush()
        line = (
            f"[{event['ts']}] unidad={normalized_municipio}/{normalized_bucket} "
            f"stage={stage} provider={provider} model={model or '-'} status={status}"
        )
        if error_class:
            line += f" error_class={error_class}"
        self.console.write(line + "\n")
        self.console.flush()

    def heartbeat(
        self,
        *,
        municipio: str,
        bucket: str,
        completed: int,
        total: int,
        last_stage: str,
        elapsed_s: float,
    ) -> None:
        self.emit(
            municipio=municipio,
            bucket=bucket,
            stage="heartbeat",
            model="",
            provider="local",
            status="ok",
            completed=completed,
            total=total,
            last_stage=last_stage,
            elapsed_s=round(elapsed_s, 3),
            message=f"unidad {completed}/{total}, ultima etapa {last_stage}, elapsed {elapsed_s:.1f}s",
        )

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def _snapshot_filename(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"


def _is_negative_decision(value: str) -> bool:
    return value == "nao_encontrado" or value.endswith(("_rechazado", "_rechazada"))


class LiveRunState:
    """Atomic progress/checkpoint/snapshot persistence and strict resume checks."""

    def __init__(self, output_dir: Path, *, resume: bool = False) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.progress_path = self.output_dir / "progress.csv"
        self.checkpoint_path = self.output_dir / "checkpoint.json"
        self.snapshots_dir = self.output_dir / "snapshots"
        self.resume = resume
        self._checkpoint: dict[str, Any] = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "units": {},
        }
        if resume and self.checkpoint_path.is_file():
            try:
                loaded = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                loaded = None
            if (
                isinstance(loaded, dict)
                and loaded.get("schema_version") == CHECKPOINT_SCHEMA_VERSION
                and isinstance(loaded.get("units"), dict)
            ):
                self._checkpoint = loaded
        self._progress_by_key = self._read_progress()

    def _read_progress(self) -> dict[tuple[str, str], dict[str, str]]:
        if not self.progress_path.is_file():
            return {}
        try:
            with self.progress_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if tuple(reader.fieldnames or ()) != PROGRESS_COLUMNS:
                    return {}
                return {
                    normalize_unit(row["municipio"], row["bucket"]): row
                    for row in reader
                }
        except (OSError, UnicodeError, csv.Error, KeyError, ValueError):
            return {}

    def _write_checkpoint(self) -> None:
        atomic_durable_write(self.checkpoint_path, canonical_json_bytes(self._checkpoint))

    def _progress_payload(self) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=PROGRESS_COLUMNS, lineterminator="\n")
        writer.writeheader()
        records = sorted(
            self._checkpoint["units"].values(),
            key=lambda item: (item["municipio"], item["bucket"]),
        )
        for record in records:
            result = record.get("result", {})
            row = {
                "municipio": record["municipio"],
                "bucket": record["bucket"],
                "url": record.get("url", ""),
                "snapshot_hash": record.get("snapshot_hash", ""),
            }
            row.update({column: result.get(column, "") for column in PROGRESS_COLUMNS if column not in row})
            writer.writerow({column: row.get(column, "") for column in PROGRESS_COLUMNS})
        return stream.getvalue().encode("utf-8")

    def _write_progress(self) -> None:
        atomic_durable_write(self.progress_path, self._progress_payload())
        self._progress_by_key = self._read_progress()

    def record_unit(
        self,
        *,
        municipio: str,
        bucket: str,
        url: str,
        result: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_municipio, normalized_bucket = normalize_unit(municipio, bucket)
        key = unit_storage_key(normalized_municipio, normalized_bucket)
        snapshot_path = ""
        snapshot_hash = ""
        if snapshot is not None:
            materialized = dict(snapshot)
            if materialized.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                raise LiveRuntimeError("snapshot_schema_version_invalid")
            unit = materialized.get("unit")
            if not isinstance(unit, Mapping) or normalize_unit(
                str(unit.get("municipio", "")), str(unit.get("bucket", ""))
            ) != (normalized_municipio, normalized_bucket):
                raise LiveRuntimeError("snapshot_unit_mismatch")
            payload = canonical_json_bytes(materialized)
            snapshot_hash = hashlib.sha256(payload).hexdigest()
            relative = Path("snapshots") / _snapshot_filename(key)
            atomic_durable_write(self.output_dir / relative, payload)
            snapshot_path = relative.as_posix()

        result_record = dict(result)
        result_record["snapshot_hash"] = snapshot_hash
        result_record["snapshot_path"] = snapshot_path
        record = {
            "municipio": normalized_municipio,
            "bucket": normalized_bucket,
            "url": url,
            "snapshot_hash": snapshot_hash,
            "snapshot_path": snapshot_path,
            "result": result_record,
        }
        self._checkpoint["units"][key] = record
        self._write_checkpoint()
        self._write_progress()
        return record

    def _valid_snapshot(self, record: Mapping[str, Any]) -> bool:
        relative = record.get("snapshot_path")
        expected_hash = record.get("snapshot_hash")
        result = record.get("result")
        if not isinstance(relative, str) or not relative or not isinstance(expected_hash, str):
            return False
        if not isinstance(result, Mapping):
            return False
        if result.get("snapshot_path") != relative or result.get("snapshot_hash") != expected_hash:
            return False
        path = (self.output_dir / relative).resolve()
        try:
            path.relative_to(self.output_dir.resolve())
            raw = path.read_bytes()
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return False
        if not isinstance(parsed, dict) or parsed.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            return False
        canonical = canonical_json_bytes(parsed)
        if raw != canonical or hashlib.sha256(canonical).hexdigest() != expected_hash:
            return False
        try:
            unit = parsed["unit"]
            if normalize_unit(unit["municipio"], unit["bucket"]) != (
                record.get("municipio"), record.get("bucket")
            ):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        progress = self._progress_by_key.get((record.get("municipio"), record.get("bucket")))
        return progress is not None and progress.get("snapshot_hash") == expected_hash

    @staticmethod
    def _satisfactory(record: Mapping[str, Any]) -> bool:
        result = record.get("result")
        if not isinstance(result, Mapping):
            return False
        final = result.get("final")
        if (
            result.get("status") != "complete"
            or result.get("error_class")
            or not isinstance(final, str)
            or final == "revisar"
        ):
            return False
        if final in CONFIRMING_DECISIONS:
            return True
        return _is_negative_decision(final) and result.get("evidence_complete") is True

    def should_skip(self, municipio: str, bucket: str) -> bool:
        key = unit_storage_key(municipio, bucket)
        record = self._checkpoint["units"].get(key)
        return bool(
            self.resume
            and isinstance(record, Mapping)
            and self._satisfactory(record)
            and self._valid_snapshot(record)
        )

    def load_satisfactory_result(self, municipio: str, bucket: str) -> Mapping[str, Any]:
        if not self.should_skip(municipio, bucket):
            raise LiveRuntimeError("unit_not_safely_resumable")
        return dict(self._checkpoint["units"][unit_storage_key(municipio, bucket)]["result"])


def _process_identity(pid: int) -> str:
    boot = ""
    start = ""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        pass
    try:
        start = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[21]
    except (OSError, IndexError):
        pass
    return f"{boot}:{start}" if boot or start else "unavailable"


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunnerLock:
    """O_EXCL lock; ``--resume`` reclaims only dead PIDs on this host."""

    def __init__(self, path: Path, *, resume: bool = False) -> None:
        self.path = Path(path)
        self.resume = resume
        self.pid = os.getpid()
        self.hostname = socket.gethostname()
        self.process_identity = _process_identity(self.pid)
        self.acquired = False

    def _payload(self) -> bytes:
        return canonical_json_bytes({
            "pid": self.pid,
            "hostname": self.hostname,
            "timestamp": utc_now_iso(),
            "process_identity": self.process_identity,
        })

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                if not self.resume:
                    raise RunnerLockError("runner_lock_exists")
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise RunnerLockError("runner_lock_unreadable") from exc
                if existing.get("hostname") != self.hostname:
                    raise RunnerLockError("runner_lock_foreign_host")
                if _pid_alive(existing.get("pid")):
                    raise RunnerLockError("runner_lock_pid_alive")
                try:
                    self.path.unlink()
                    _fsync_directory(self.path.parent)
                except OSError as exc:
                    raise RunnerLockError("runner_lock_stale_reclaim_failed") from exc
                continue
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(self._payload())
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory(self.path.parent)
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
            self.acquired = True
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            owned = (
                existing.get("pid") == self.pid
                and existing.get("hostname") == self.hostname
                and existing.get("process_identity") == self.process_identity
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            owned = False
        if owned:
            self.path.unlink(missing_ok=True)
            _fsync_directory(self.path.parent)
        self.acquired = False

    def __enter__(self) -> "RunnerLock":
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION", "CONFIRMING_DECISIONS", "EventLogger",
    "LiveRunState", "LiveRuntimeError", "PROGRESS_COLUMNS", "RunnerLock",
    "RunnerLockError", "SNAPSHOT_SCHEMA_VERSION", "atomic_durable_write",
    "canonical_json_bytes", "normalize_unit", "unit_storage_key",
]
