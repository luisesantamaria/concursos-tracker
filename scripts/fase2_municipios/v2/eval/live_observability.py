"""Bounded, sanitized diagnostic artifacts for live V2 runs.

Non-citation snapshot content is capped per artifact (default 200,000
characters). Truncated sources are represented as offset-bearing segments.
Citation ranges present in the raw stage response are retained verbatim even
when they exceed that cap, so quotes remain reproducible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LOGGER = logging.getLogger(__name__)
PARTIAL_FILENAME = "golden_live.partial.schema1.json"
DEFAULT_MAX_SNAPSHOT_CHARS = 200_000
STAGE_STATES = frozenset({
    "not_started", "request_failed", "raw_received", "validation_failed", "skipped"
})
_MISSING = object()
_SENSITIVE_KEYS = frozenset({
    "authorization", "proxyauthorization", "xapikey", "apikey", "token",
    "accesstoken", "refreshtoken", "password", "passwd", "secret",
    "clientsecret", "cookie", "setcookie", "proxy", "httpproxy", "httpsproxy",
})
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    if port is not None:
        host = f"{host}:{port}"
    query_items = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
        query_items.append((
            key,
            "<redacted>" if normalized in _SENSITIVE_KEYS else item,
        ))
    query = urlencode(query_items, doseq=True)
    return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))


def _redact_text(value: str, redactions: tuple[str, ...]) -> str:
    sanitized = _BEARER_RE.sub("Bearer <redacted>", value)
    sanitized = _URL_RE.sub(lambda match: _safe_url(match.group(0)), sanitized)
    for secret in redactions:
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    return sanitized


def redact_recursive(value: Any, *, redactions: Iterable[str] = ()) -> Any:
    """Best-effort recursive minimization for representative secret patterns."""

    checked = tuple(item for item in redactions if isinstance(item, str) and item)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]+", "", key_text.casefold())
            if normalized_key in _SENSITIVE_KEYS:
                result[key_text] = "<redacted>"
            else:
                result[key_text] = redact_recursive(item, redactions=checked)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_recursive(item, redactions=checked) for item in value]
    if isinstance(value, str):
        return _redact_text(value, checked)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), checked)


def _citations(raw: Any) -> dict[str, list[tuple[int, int]]]:
    found: dict[str, list[tuple[int, int]]] = {}
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            source_id = value.get("source_id")
            start, end = value.get("start"), value.get("end")
            if (
                isinstance(source_id, str) and isinstance(start, int)
                and not isinstance(start, bool) and isinstance(end, int)
                and not isinstance(end, bool) and 0 <= start < end
            ):
                found.setdefault(source_id, []).append((start, end))
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    visit(raw)
    return found


def _bounded_snapshot(snapshot: Mapping[str, Any], raw: Any, limit: int) -> dict[str, Any]:
    result = {key: value for key, value in snapshot.items() if key != "sources"}
    citation_ranges = _citations(raw)
    remaining = limit
    sources = []
    for source in snapshot.get("sources", ()):
        if not isinstance(source, Mapping):
            continue
        copied = {key: value for key, value in source.items() if key != "content"}
        content = source.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        ranges = citation_ranges.get(str(source.get("source_id", "")), [])
        segments: list[dict[str, Any]] = []
        occupied: set[tuple[int, int]] = set()
        for start, end in ranges:
            if start >= len(content):
                continue
            checked_end = min(end, len(content))
            pair = (start, checked_end)
            if pair not in occupied and checked_end > start:
                segments.append({
                    "original_start": start,
                    "original_end": checked_end,
                    "text": content[start:checked_end],
                })
                occupied.add(pair)
                remaining = max(0, remaining - (checked_end - start))
        if remaining > 0:
            head_end = min(len(content), remaining)
            if head_end > 0 and (0, head_end) not in occupied:
                segments.insert(0, {
                    "original_start": 0, "original_end": head_end,
                    "text": content[:head_end],
                })
                remaining -= head_end
        copied["original_length"] = len(content)
        copied["content_truncated"] = sum(len(item["text"]) for item in segments) < len(content)
        copied["content_segments"] = segments
        sources.append(copied)
    result["sources"] = sources
    result["content_limit_chars"] = limit
    return result


def _unit_mapping(unit: tuple[str, str]) -> dict[str, str]:
    return {"municipio": unit[0], "bucket": unit[1]}


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-") or "unit"


class StageArtifactWriter:
    def __init__(
        self, output_dir: Path, *, max_snapshot_chars: int = DEFAULT_MAX_SNAPSHOT_CHARS,
        redactions: Iterable[str] = (),
    ):
        if isinstance(max_snapshot_chars, bool) or max_snapshot_chars <= 0:
            raise ValueError("max_snapshot_chars must be positive")
        self.output_dir = Path(output_dir)
        self.max_snapshot_chars = int(max_snapshot_chars)
        self.redactions = tuple(
            item for item in redactions if isinstance(item, str) and item
        )

    def _path(self, unit: tuple[str, str], attempt: int) -> Path:
        identity = f"{unit[0]}\0{unit[1]}".encode("utf-8")
        suffix = hashlib.sha256(identity).hexdigest()[:12]
        name = f"{_slug(unit[0])}--{_slug(unit[1])}--{suffix}--attempt-{attempt:03d}.json"
        return self.output_dir / "observability" / name

    def reference(self, unit: tuple[str, str], attempt: int) -> dict[str, str]:
        path = self._path(unit, attempt)
        try:
            payload = path.read_bytes()
            relative = path.relative_to(self.output_dir)
        except (OSError, ValueError):
            return {}
        return {
            "observability_path": relative.as_posix(),
            "observability_hash": hashlib.sha256(payload).hexdigest(),
        }

    def record_stage(
        self, *, unit: tuple[str, str], attempt: int, stage: str, state: str,
        snapshot: Mapping[str, Any] | None = None, raw: Any = _MISSING,
        error: BaseException | None = None, redactions: Iterable[str] = (),
    ) -> Path | None:
        if state not in STAGE_STATES:
            raise ValueError("invalid stage state")
        path = self._path(unit, attempt)
        try:
            artifact = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists() else {
                    "schema_version": 1,
                    "unit": _unit_mapping(unit),
                    "attempt": attempt,
                    "stages": {
                        name: {"state": "not_started"}
                        for name in ("fetch", "A", "B", "C")
                    },
                }
            )
            stage_record: dict[str, Any] = {"state": state}
            if raw is not _MISSING:
                stage_record["raw"] = raw
            if error is not None:
                stage_record["error"] = {
                    "code": type(error).__name__,
                    "message": " ".join(str(error).split()),
                }
                status_code = getattr(error, "status_code", None)
                if isinstance(status_code, int) and not isinstance(status_code, bool):
                    stage_record["error"]["status_code"] = status_code
            artifact["stages"][stage] = stage_record
            if snapshot is not None:
                artifact["evidence_snapshot"] = _bounded_snapshot(
                    snapshot, None if raw is _MISSING else raw, self.max_snapshot_chars
                )
            sanitized = redact_recursive(
                artifact, redactions=(*self.redactions, *tuple(redactions))
            )
            _atomic_write(
                path,
                json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
            )
            return path
        except Exception as exc:
            LOGGER.warning("stage_artifact_persist_failed", extra={
                "error_class": type(exc).__name__, "stage": stage,
            })
            return None


class IncompleteRunTracker:
    """Tracks terminal units; only missing terminal results mean incomplete."""

    def __init__(
        self, output_dir: Path, units: Iterable[tuple[str, str]], *,
        redactions: Iterable[str] = (),
    ) -> None:
        self.output_dir = Path(output_dir)
        self.units = tuple(units)
        self._terminal: dict[tuple[str, str], dict[str, str]] = {}
        self.redactions = tuple(
            item for item in redactions if isinstance(item, str) and item
        )

    def record_terminal(
        self, unit: tuple[str, str], *, status: str, decision: str | None = None,
        revisar_por: str = "", stage: str = "", error_class: str = "",
    ) -> None:
        if unit not in self.units or status not in {"complete", "error"}:
            raise ValueError("invalid terminal unit result")
        self._terminal[unit] = {
            "status": status,
            "decision": decision if isinstance(decision, str) else "",
            "revisar_por": revisar_por if isinstance(revisar_por, str) else "",
            "stage": stage if isinstance(stage, str) else "",
            "error_class": error_class if isinstance(error_class, str) else "",
        }

    @property
    def incomplete(self) -> bool:
        return any(unit not in self._terminal for unit in self.units)

    def write_from_finally(self, cause: BaseException | None) -> Path | None:
        if not self.incomplete:
            return None
        completed = [
            unit for unit in self.units
            if self._terminal.get(unit, {}).get("status") == "complete"
        ]
        failed = [
            unit for unit in self.units
            if self._terminal.get(unit, {}).get("status") == "error"
        ]
        pending = [unit for unit in self.units if unit not in self._terminal]
        cause_record = {
            "code": type(cause).__name__ if cause is not None else "runner_incomplete",
            "message": " ".join(str(cause).split()) if cause is not None else "terminal result missing",
        }
        artifact = {
            "schema_version": 1,
            "incomplete": True,
            "completed_units": [_unit_mapping(unit) for unit in completed],
            "failed_units": [_unit_mapping(unit) for unit in failed],
            "pending_units": [_unit_mapping(unit) for unit in pending],
            "completed": [
                {
                    **_unit_mapping(unit),
                    "decision": self._terminal[unit]["decision"],
                    "revisar_por": self._terminal[unit]["revisar_por"],
                }
                for unit in completed
            ],
            "failed": [
                {
                    **_unit_mapping(unit),
                    "stage": self._terminal[unit]["stage"],
                    "error_class": self._terminal[unit]["error_class"],
                    "revisar_por": self._terminal[unit]["revisar_por"],
                }
                for unit in failed
            ],
            "pending": [_unit_mapping(unit) for unit in pending],
            "cause": cause_record,
            "cause_global": cause_record,
        }
        path = self.output_dir / PARTIAL_FILENAME
        try:
            sanitized = redact_recursive(artifact, redactions=self.redactions)
            _atomic_write(
                path,
                json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
            )
        except Exception as exc:
            LOGGER.warning("partial_artifact_persist_failed", extra={
                "error_class": type(exc).__name__,
            })
            return None
        return path


def is_publishable_artifact(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == 1
        and value.get("incomplete") is not True
        and isinstance(value.get("cases"), list)
    )


__all__ = [
    "DEFAULT_MAX_SNAPSHOT_CHARS", "IncompleteRunTracker", "PARTIAL_FILENAME",
    "STAGE_STATES", "StageArtifactWriter", "is_publishable_artifact",
    "redact_recursive",
]
