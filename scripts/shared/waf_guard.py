"""Per-provider WAF freeze guard for municipal crawls.

The block we saw in production is provider-wide, not URL-wide: several
municipal sites hosted on the same shared infrastructure returned the same
security challenge. This module keeps an in-memory freeze per resolved /24 so
callers can stop issuing more HTTP/browser requests to that group during the
current run.
"""

from __future__ import annotations

import ipaddress
import fnmatch
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse


_HOST_GROUP_CACHE: dict[str, str] = {}
_FREEZES: dict[str, "_Freeze"] = {}
_PREFROZEN_HOST_PATTERNS: set[str] = set()
_NOW = time.time

_HOST_SCOPED_SUFFIXES = (
    ".atende.net",
    ".govbr.cloud",
)
_HOST_SCOPED_CONTAINS = (
    ".elotech.",
)


@dataclass
class _Freeze:
    count: int = 0
    until: float | None = 0.0


def group_for(url: str) -> str:
    """Return a provider-ish group key for ``url``.

    IPv4 hosts are grouped by /24. DNS failures fall back to hostname so the
    guard still prevents repeated requests to the same blocked site.
    """
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return "host:"
    cached = _HOST_GROUP_CACHE.get(host)
    if cached:
        return cached
    if _is_host_scoped_tenant(host):
        group = f"host:{host}"
        _HOST_GROUP_CACHE[host] = group
        return group
    try:
        ip = socket.gethostbyname(host)
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv4Address):
            parts = ip.split(".")
            group = f"ipv4:{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        else:
            group = f"ip:{addr}"
    except Exception:
        group = f"host:{host}"
    _HOST_GROUP_CACHE[host] = group
    return group


def _is_host_scoped_tenant(host: str) -> bool:
    """SaaS portals host many unrelated municipalities on shared IP space."""
    return (
        any(host.endswith(suffix) for suffix in _HOST_SCOPED_SUFFIXES)
        or any(marker in host for marker in _HOST_SCOPED_CONTAINS)
    )


def is_frozen(url: str) -> bool:
    """True when the URL's provider group is currently frozen."""
    if _matches_prefrozen_host(url):
        return True
    freeze_state = _FREEZES.get(group_for(url))
    if not freeze_state:
        return False
    if freeze_state.until is None:
        return True
    if freeze_state.until > _NOW():
        return True
    return False


def freeze(url: str) -> dict[str, object]:
    """Freeze the URL's group: 15 min, then 45 min, then rest of run."""
    group = group_for(url)
    state = _FREEZES.setdefault(group, _Freeze())
    state.count += 1
    if state.count == 1:
        duration = 15 * 60
        state.until = _NOW() + duration
    elif state.count == 2:
        duration = 45 * 60
        state.until = _NOW() + duration
    else:
        duration = None
        state.until = None
    return {
        "group": group,
        "count": state.count,
        "duration_seconds": duration,
        "until": state.until,
    }


def freeze_group(group: str, *, permanent: bool = True) -> dict[str, object]:
    """Freeze a precomputed group key, usually from a prefreeze list."""
    state = _FREEZES.setdefault(group, _Freeze())
    state.count += 1
    state.until = None if permanent else _NOW() + 15 * 60
    return {
        "group": group,
        "count": state.count,
        "duration_seconds": None if permanent else 15 * 60,
        "until": state.until,
    }


def prefreeze(pattern: str) -> dict[str, object]:
    """Permanently freeze a host/url/group pattern for the current run."""
    raw = (pattern or "").strip().lower()
    if not raw:
        return {"pattern": "", "kind": "empty"}
    if raw.startswith(("host:", "ipv4:", "ip:")):
        return {"pattern": raw, "kind": "group", **freeze_group(raw)}

    host = (urlparse(raw).hostname or "").lower() if "://" in raw else raw
    host = host.strip("/")
    if not host:
        return {"pattern": raw, "kind": "empty"}
    _PREFROZEN_HOST_PATTERNS.add(host)

    # Exact hosts can also freeze their provider /24. Globs stay host-pattern
    # only because they are not resolvable.
    if "*" not in host and "?" not in host:
        url = raw if "://" in raw else f"https://{host}/"
        group = group_for(url)
        freeze_group(group, permanent=True)
        return {"pattern": host, "kind": "host+group", "group": group}
    return {"pattern": host, "kind": "host_pattern"}


def _matches_prefrozen_host(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return False
    for pattern in _PREFROZEN_HOST_PATTERNS:
        if fnmatch.fnmatch(host, pattern) or host == pattern:
            return True
    return False


def snapshot() -> dict[str, dict[str, object]]:
    """Expose freeze state for diagnostics/tests."""
    out = {
        group: {"count": state.count, "until": state.until}
        for group, state in _FREEZES.items()
    }
    if _PREFROZEN_HOST_PATTERNS:
        out["_prefrozen_host_patterns"] = {
            "count": len(_PREFROZEN_HOST_PATTERNS),
            "until": None,
        }
    return out


def reset_for_tests() -> None:
    """Clear in-memory state. Intended for unit tests only."""
    _HOST_GROUP_CACHE.clear()
    _FREEZES.clear()
    _PREFROZEN_HOST_PATTERNS.clear()


def set_now_for_tests(now_fn) -> None:
    """Inject a deterministic clock in tests."""
    global _NOW
    _NOW = now_fn


def reset_clock_for_tests() -> None:
    global _NOW
    _NOW = time.time
