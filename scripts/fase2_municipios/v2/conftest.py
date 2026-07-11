"""Pytest configuration scoped to the parallel Fase 2 V2 suite."""

from __future__ import annotations

import socket
from dataclasses import dataclass

import pytest


BLOCKED_NETWORK_MESSAGE = "RED BLOQUEADA EN SUITE OFFLINE"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class NetworkGuardSpy:
    blocked_attempts: int = 0

    def reset(self) -> None:
        self.blocked_attempts = 0


def _is_allowed_destination(address) -> bool:
    if not isinstance(address, tuple):
        return True
    return bool(address) and address[0] in LOOPBACK_HOSTS


@pytest.fixture(scope="session")
def network_guard_spy() -> NetworkGuardSpy:
    return NetworkGuardSpy()


@pytest.fixture(scope="session", autouse=True)
def block_external_network(network_guard_spy: NetworkGuardSpy):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def guarded_call(original, sock, address, *args, **kwargs):
        if not _is_allowed_destination(address):
            network_guard_spy.blocked_attempts += 1
            raise RuntimeError(BLOCKED_NETWORK_MESSAGE)
        return original(sock, address, *args, **kwargs)

    def guarded_connect(sock, address):
        return guarded_call(original_connect, sock, address)

    def guarded_connect_ex(sock, address):
        return guarded_call(original_connect_ex, sock, address)

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_allowed_destination(address):
            network_guard_spy.blocked_attempts += 1
            raise RuntimeError(BLOCKED_NETWORK_MESSAGE)
        return original_create_connection(address, *args, **kwargs)

    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket.socket, "connect", guarded_connect)
    patcher.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    patcher.setattr(socket, "create_connection", guarded_create_connection)
    try:
        yield
    finally:
        patcher.undo()


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "offline: test performs no network access and uses no real sleep"
    )
