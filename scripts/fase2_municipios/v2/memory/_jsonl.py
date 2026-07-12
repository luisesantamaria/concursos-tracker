"""Locked one-syscall JSONL appends shared by separate event logs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import fcntl


def append_json_line(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    line = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    lock_fd = os.open(
        str(target.with_name(target.name + ".lock")),
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data_fd = os.open(
            str(target),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            written = os.write(data_fd, line)
            if written != len(line):
                raise OSError("partial append")
            os.fsync(data_fd)
        finally:
            os.close(data_fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
