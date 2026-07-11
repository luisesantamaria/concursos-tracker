"""Pytest configuration scoped to the parallel Fase 2 V2 suite."""


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "offline: test performs no network access and uses no real sleep"
    )
