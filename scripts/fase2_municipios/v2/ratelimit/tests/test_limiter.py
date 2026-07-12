"""Deterministic offline tests: no network and no real sleeping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.fase2_municipios.v2.ratelimit import (
    LimiterConfig,
    ProjectRateLimiter,
    QuotaExhaustedError,
)


pytestmark = pytest.mark.offline


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.base_utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self.seconds

    def utc_now(self) -> datetime:
        return self.base_utc + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.seconds += seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


def make_limiter(clock: FakeClock, config: LimiterConfig | None = None) -> ProjectRateLimiter:
    return ProjectRateLimiter(
        config=config,
        now=clock.now,
        sleep=clock.sleep,
        utc_now=clock.utc_now,
    )


def test_sixteenth_request_waits_for_sliding_rpm_window() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock)

    for _ in range(15):
        limiter.acquire(1)
    limiter.acquire(1)

    assert clock.sleep_calls
    assert clock.sleep_calls == pytest.approx([60.000001])
    assert clock.seconds > 60.0
    assert limiter.snapshot().requests_in_window == 1


def test_tpm_waits_and_reconcile_replaces_estimate_with_actual_usage() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, LimiterConfig(rpm=100, tpm=250_000))

    first = limiter.acquire(200_000)
    first.reconcile(240_000)
    limiter.acquire(10_000)
    limiter.acquire(1)

    assert clock.sleep_calls == pytest.approx([60.000001])
    assert clock.seconds > 60.0
    snapshot = limiter.snapshot()
    assert snapshot.tokens_in_window == 1
    assert snapshot.requests_in_window == 1


def test_rpd_raise_is_typed_and_utc_day_rollover_resets_counter() -> None:
    clock = FakeClock()
    limiter = make_limiter(
        clock,
        LimiterConfig(rpm=100, tpm=250_000, rpd=2, rpd_policy="raise"),
    )

    limiter.acquire(1)
    limiter.acquire(1)
    with pytest.raises(QuotaExhaustedError) as raised:
        limiter.acquire(1)

    assert raised.value.limit_name == "rpd"
    assert raised.value.limit == 2
    assert raised.value.used == 2
    assert raised.value.available == 0
    assert clock.sleep_calls == []

    clock.advance(86_400.0)
    limiter.acquire(1)
    assert limiter.snapshot().requests_today == 1
    assert clock.sleep_calls == []


def test_shared_limiter_is_one_in_process_instance() -> None:
    clock = FakeClock()
    ProjectRateLimiter._reset_shared_for_tests()
    config = LimiterConfig(rpm=20, tpm=100_000)

    first = ProjectRateLimiter.shared(
        config, now=clock.now, sleep=clock.sleep, utc_now=clock.utc_now
    )
    second = ProjectRateLimiter.shared(config)

    assert first is second
    assert clock.sleep_calls == []
    ProjectRateLimiter._reset_shared_for_tests()
