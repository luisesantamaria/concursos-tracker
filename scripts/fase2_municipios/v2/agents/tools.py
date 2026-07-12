"""Bounded, offline, read-only tools over a frozen V2 EvidenceSnapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scripts.fase2_municipios.v2.snapshot import (
    CitationVerificationError,
    EvidenceSnapshot,
)


class ToolError(RuntimeError):
    """Base error for bounded local tool execution."""


class ToolLimitError(ToolError):
    def __init__(self, *, tool: str, limit_name: str, limit: int) -> None:
        self.tool = tool
        self.limit_name = limit_name
        self.limit = limit
        super().__init__(f"tool limit exceeded: tool={tool}, limit={limit_name}, value={limit}")


class ToolExecutionError(ToolError):
    def __init__(self, *, tool: str, error_type: str) -> None:
        self.tool = tool
        self.error_type = error_type
        super().__init__(f"internal tool failure: tool={tool}, error_type={error_type}")


@dataclass(frozen=True)
class ToolLimits:
    get_source_max_length: int = 4_000
    get_source_default_length: int = 2_000
    find_max_needle_length: int = 256
    find_max_matches: int = 20

    def __post_init__(self) -> None:
        for name in (
            "get_source_max_length",
            "get_source_default_length",
            "find_max_needle_length",
            "find_max_matches",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.get_source_default_length > self.get_source_max_length:
            raise ValueError("get_source_default_length cannot exceed get_source_max_length")


def _error_observation(tool: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "error": {"code": code, "detail": detail},
    }


class LocalSnapshotTools:
    """Exact raw-content tools; no normalization, regex, I/O or mutation."""

    def __init__(self, snapshot: EvidenceSnapshot, limits: ToolLimits | None = None) -> None:
        self.snapshot = snapshot
        self.limits = limits or ToolLimits()

    def list_sources(self) -> dict[str, Any]:
        return {
            "ok": True,
            "tool": "list_sources",
            "sources": [
                {
                    "source_id": source.source_id,
                    "url": source.url,
                    "length": len(source.content),
                    "content_sha256": source.content_sha256,
                }
                for source in self.snapshot.sources
            ],
        }

    def get_source(self, source_id: str, *, start: int = 0, length: int | None = None) -> dict[str, Any]:
        requested_length = self.limits.get_source_default_length if length is None else length
        source = self.snapshot.get_source(source_id)
        bounded_length = min(requested_length, self.limits.get_source_max_length)
        content = source.content[start:start + bounded_length]
        returned_length = len(content)
        next_offset = start + returned_length
        has_more = next_offset < len(source.content)
        return {
            "ok": True,
            "tool": "get_source",
            "source_id": source_id,
            "start": start,
            "requested_length": requested_length,
            "returned_length": returned_length,
            "next_start": next_offset if has_more else None,
            "has_more": has_more,
            "content": content,
        }

    def find(self, source_id: str, needle: str) -> dict[str, Any]:
        if not needle:
            return _error_observation("find", "empty_needle", "needle must not be empty")
        if len(needle) > self.limits.find_max_needle_length:
            raise ToolLimitError(
                tool="find",
                limit_name="find_max_needle_length",
                limit=self.limits.find_max_needle_length,
            )
        source = self.snapshot.get_source(source_id)
        offsets: list[dict[str, int]] = []
        cursor = 0
        total_seen = 0
        while True:
            start = source.content.find(needle, cursor)
            if start < 0:
                break
            total_seen += 1
            if len(offsets) < self.limits.find_max_matches:
                offsets.append({"start": start, "end": start + len(needle)})
            cursor = start + max(1, len(needle))
        return {
            "ok": True,
            "tool": "find",
            "source_id": source_id,
            "needle_length": len(needle),
            "matches": offsets,
            "returned_matches": len(offsets),
            "has_more": total_seen > len(offsets),
        }

    def execute(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Return structured observations for model mistakes; raise only limits/internal faults."""
        if not isinstance(tool, str) or not tool:
            return _error_observation(str(tool), "invalid_tool", "tool must be non-empty string")
        if not isinstance(args, Mapping):
            return _error_observation(tool, "invalid_args", "args must be object")
        try:
            if tool == "list_sources":
                if args:
                    return _error_observation(tool, "invalid_args", "list_sources accepts no args")
                return self.list_sources()
            if tool == "get_source":
                allowed = {"source_id", "start", "length"}
                if set(args) - allowed or "source_id" not in args:
                    return _error_observation(tool, "invalid_args", "expected source_id/start/length")
                source_id = args["source_id"]
                start = args.get("start", 0)
                length = args.get("length", self.limits.get_source_default_length)
                if (
                    not isinstance(source_id, str)
                    or not source_id
                    or isinstance(start, bool)
                    or not isinstance(start, int)
                    or start < 0
                    or isinstance(length, bool)
                    or not isinstance(length, int)
                    or length <= 0
                ):
                    return _error_observation(tool, "invalid_args", "invalid source_id/start/length")
                return self.get_source(source_id, start=start, length=length)
            if tool == "find":
                if set(args) != {"source_id", "needle"}:
                    return _error_observation(tool, "invalid_args", "expected source_id and needle")
                source_id = args["source_id"]
                needle = args["needle"]
                if not isinstance(source_id, str) or not source_id or not isinstance(needle, str):
                    return _error_observation(tool, "invalid_args", "invalid source_id/needle")
                return self.find(source_id, needle)
            return _error_observation(tool, "unknown_tool", "tool is not in local allowlist")
        except CitationVerificationError as exc:
            return _error_observation(tool, exc.reason, f"source_id={exc.source_id}")
        except ToolLimitError:
            raise
        except Exception as exc:
            raise ToolExecutionError(tool=tool, error_type=type(exc).__name__) from exc
