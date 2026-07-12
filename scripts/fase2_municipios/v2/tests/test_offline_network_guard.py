"""Contract tests for the session-wide offline network guard."""

from __future__ import annotations

import socket

import pytest


pytestmark = pytest.mark.offline
MESSAGE = "RED BLOQUEADA EN SUITE OFFLINE"


@pytest.mark.parametrize(
    ("family", "address"),
    [
        (socket.AF_INET, ("198.51.100.7", 443)),
        (socket.AF_INET6, ("2001:db8::7", 443, 0, 0)),
    ],
)
def test_external_ip_destinations_are_blocked(network_guard_spy, family, address) -> None:
    network_guard_spy.reset()
    with pytest.raises(RuntimeError, match=MESSAGE):
        socket.socket.connect_ex(object(), address)
    assert network_guard_spy.blocked_attempts == 1


def test_external_hostname_is_blocked_before_dns(network_guard_spy) -> None:
    network_guard_spy.reset()
    with pytest.raises(RuntimeError, match=MESSAGE):
        socket.create_connection(("example.com", 443), timeout=0.01)
    assert network_guard_spy.blocked_attempts == 1


def test_socket_connect_entrypoint_is_blocked(network_guard_spy) -> None:
    network_guard_spy.reset()
    with pytest.raises(RuntimeError, match=MESSAGE):
        socket.socket.connect(object(), ("203.0.113.9", 443))
    assert network_guard_spy.blocked_attempts == 1


@pytest.mark.parametrize(
    ("family", "address"),
    [
        (socket.AF_INET, ("127.0.0.1", 9)),
        (socket.AF_INET6, ("::1", 9, 0, 0)),
    ],
)
def test_loopback_is_not_blocked(network_guard_spy, family, address) -> None:
    network_guard_spy.reset()
    with pytest.raises(TypeError):
        socket.socket.connect_ex(object(), address)
    assert network_guard_spy.blocked_attempts == 0


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX unavailable")
def test_af_unix_is_not_blocked(network_guard_spy, tmp_path) -> None:
    network_guard_spy.reset()
    with pytest.raises(TypeError):
        socket.socket.connect_ex(object(), str(tmp_path / "missing.sock"))
    assert network_guard_spy.blocked_attempts == 0
