"""Thread-safe, in-process project quota limiter for Fase 2 V2.

The certifier, false-positive prosecutor and conflict judge must reuse one
``ProjectRateLimiter`` because they consume the same free project quota. The
singleton returned by :func:`get_shared_limiter` is deliberately in-process:
cross-process coordination is not implemented in this round. Subclasses can
later implement ``_synchronize_cross_process_locked`` and
``_publish_cross_process_locked`` with a file lock without changing callers.

``acquire`` reserves a request and estimated tokens before an external call;
the returned handle reconciles actual token use afterwards. RPM and TPM use
60-second sliding windows driven by an injectable monotonic clock. RPD rolls
over at midnight UTC using an injectable UTC clock. The default RPD policy is
``raise`` because silently blocking for almost 24 hours is not operationally
viable; ``block`` remains available for explicitly supervised processes. No
paid fallback, network call, credential access or provider SDK exists here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from enum import Enum
from itertools import count
from typing import Callable, ClassVar


LOGGER = logging.getLogger(__name__)
WINDOW_SECONDS = 60.0
WAIT_EPSILON_SECONDS = 1e-6
RPD_BLOCK_SLICE_SECONDS = 60.0

MonotonicClock = Callable[[], float]
SleepFunction = Callable[[float], None]
UtcClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RPDPolicy(str, Enum):
    RAISE = "raise"
    BLOCK = "block"


@dataclass(frozen=True)
class LimiterConfig:
    """Immutable free-project quota configuration."""

    rpm: int = 15
    tpm: int = 250_000
    rpd: int | None = None
    rpd_policy: RPDPolicy | str = RPDPolicy.RAISE

    def __post_init__(self) -> None:
        if isinstance(self.rpm, bool) or not isinstance(self.rpm, int) or self.rpm <= 0:
            raise ValueError("rpm must be a positive integer")
        if isinstance(self.tpm, bool) or not isinstance(self.tpm, int) or self.tpm <= 0:
            raise ValueError("tpm must be a positive integer")
        if (
            self.rpd is not None
            and (isinstance(self.rpd, bool) or not isinstance(self.rpd, int) or self.rpd < 0)
        ):
            raise ValueError("rpd must be a non-negative integer or None")
        try:
            policy = RPDPolicy(self.rpd_policy)
        except ValueError as exc:
            raise ValueError("rpd_policy must be 'raise' or 'block'") from exc
        object.__setattr__(self, "rpd_policy", policy)


class QuotaExhaustedError(RuntimeError):
    """Auditable quota failure containing limits and capacity, never content."""

    def __init__(
        self,
        *,
        limit_name: str,
        limit: int,
        window_seconds: float | None,
        used: int,
        requested: int,
        available: int,
        retry_after: float | None,
    ) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.window_seconds = window_seconds
        self.used = used
        self.requested = requested
        self.available = available
        self.retry_after = retry_after
        super().__init__(
            f"quota exhausted: limit={limit_name}, configured={limit}, used={used}, "
            f"requested={requested}, available={available}, "
            f"window_seconds={window_seconds}, retry_after={retry_after}"
        )


class ReservationError(RuntimeError):
    """Raised for an invalid, foreign or repeated reconciliation."""


@dataclass(frozen=True)
class UsageSnapshot:
    """Secret-free state published through the future cross-process seam."""

    requests_in_window: int
    tokens_in_window: int
    requests_today: int
    utc_day: date


@dataclass
class _TokenCharge:
    reservation_id: int
    timestamp: float
    tokens: int
    reconciled: bool = False


class Reservation:
    """Handle for reconciling one successful pre-call reservation."""

    __slots__ = ("_limiter", "reservation_id", "estimated_tokens")

    def __init__(
        self, limiter: ProjectRateLimiter, reservation_id: int, estimated_tokens: int
    ) -> None:
        self._limiter = limiter
        self.reservation_id = reservation_id
        self.estimated_tokens = estimated_tokens

    def reconcile(self, actual_tokens: int) -> None:
        self._limiter.reconcile(self, actual_tokens)


class ProjectRateLimiter:
    """FIFO limiter shared by all V2 roles in one Python process."""

    _shared_instance: ClassVar[ProjectRateLimiter | None] = None
    _shared_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        config: LimiterConfig | None = None,
        *,
        now: MonotonicClock = time.monotonic,
        sleep: SleepFunction = time.sleep,
        utc_now: UtcClock = _utc_now,
    ) -> None:
        self.config = config or LimiterConfig()
        self._now = now
        self._sleep = sleep
        self._utc_now = utc_now
        self._condition = threading.Condition(threading.RLock())
        self._request_times: deque[float] = deque()
        self._token_charges: deque[_TokenCharge] = deque()
        self._charges_by_id: dict[int, _TokenCharge] = {}
        self._waiters: deque[int] = deque()
        self._waiter_ids = count(1)
        self._reservation_ids = count(1)
        self._rpd_day = self._current_utc_day()
        self._requests_today = 0

    @classmethod
    def shared(
        cls,
        config: LimiterConfig | None = None,
        *,
        now: MonotonicClock = time.monotonic,
        sleep: SleepFunction = time.sleep,
        utc_now: UtcClock = _utc_now,
    ) -> ProjectRateLimiter:
        """Return the unique reusable limiter for this process."""
        with cls._shared_lock:
            if cls._shared_instance is None:
                cls._shared_instance = cls(
                    config=config, now=now, sleep=sleep, utc_now=utc_now
                )
            elif config is not None and config != cls._shared_instance.config:
                raise RuntimeError("shared limiter already exists with different configuration")
            return cls._shared_instance

    @classmethod
    def _reset_shared_for_tests(cls) -> None:
        """Reset singleton isolation; intended only for deterministic unit tests."""
        with cls._shared_lock:
            cls._shared_instance = None

    def _current_utc(self) -> datetime:
        current = self._utc_now()
        if current.tzinfo is None:
            raise ValueError("utc_now must return a timezone-aware datetime")
        return current.astimezone(timezone.utc)

    def _current_utc_day(self) -> date:
        return self._current_utc().date()

    def _synchronize_cross_process_locked(self) -> None:
        """Future hook to merge file-locked state before local decisions."""

    def _publish_cross_process_locked(self, snapshot: UsageSnapshot) -> None:
        """Future hook to persist file-locked state after local mutations."""

    def _rollover_rpd_locked(self) -> None:
        current_day = self._current_utc_day()
        if current_day != self._rpd_day:
            LOGGER.info(
                "ratelimit_rpd_rollover",
                extra={
                    "ratelimit_event": "rpd_rollover",
                    "previous_utc_day": self._rpd_day.isoformat(),
                    "utc_day": current_day.isoformat(),
                    "previous_requests": self._requests_today,
                },
            )
            self._rpd_day = current_day
            self._requests_today = 0

    def _prune_locked(self, now_value: float) -> None:
        cutoff = now_value - WINDOW_SECONDS
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()
        while self._token_charges and self._token_charges[0].timestamp <= cutoff:
            charge = self._token_charges.popleft()
            self._charges_by_id.pop(charge.reservation_id, None)

    def _tokens_used_locked(self) -> int:
        return sum(charge.tokens for charge in self._token_charges)

    def _usage_snapshot_locked(self) -> UsageSnapshot:
        return UsageSnapshot(
            requests_in_window=len(self._request_times),
            tokens_in_window=self._tokens_used_locked(),
            requests_today=self._requests_today,
            utc_day=self._rpd_day,
        )

    def snapshot(self) -> UsageSnapshot:
        """Return a secret-free instantaneous usage snapshot."""
        with self._condition:
            self._synchronize_cross_process_locked()
            self._rollover_rpd_locked()
            self._prune_locked(self._now())
            return self._usage_snapshot_locked()

    def _window_wait_locked(self, now_value: float, estimated_tokens: int) -> float:
        waits: list[float] = []
        if len(self._request_times) >= self.config.rpm:
            waits.append(self._request_times[0] + WINDOW_SECONDS - now_value)

        used = self._tokens_used_locked()
        if used + estimated_tokens > self.config.tpm:
            remaining = used
            for charge in self._token_charges:
                remaining -= charge.tokens
                if remaining + estimated_tokens <= self.config.tpm:
                    waits.append(charge.timestamp + WINDOW_SECONDS - now_value)
                    break
        return max(0.0, max(waits, default=0.0))

    def _seconds_until_utc_rollover_locked(self) -> float:
        current = self._current_utc()
        tomorrow = datetime.combine(
            current.date() + timedelta(days=1), datetime_time.min, tzinfo=timezone.utc
        )
        return max(0.0, (tomorrow - current).total_seconds())

    def acquire(self, estimated_tokens: int) -> Reservation:
        """Block in a fair FIFO queue and reserve request/token capacity."""
        if (
            isinstance(estimated_tokens, bool)
            or not isinstance(estimated_tokens, int)
            or estimated_tokens < 0
        ):
            raise ValueError("estimated_tokens must be a non-negative integer")
        if estimated_tokens > self.config.tpm:
            raise QuotaExhaustedError(
                limit_name="tpm",
                limit=self.config.tpm,
                window_seconds=WINDOW_SECONDS,
                used=0,
                requested=estimated_tokens,
                available=self.config.tpm,
                retry_after=None,
            )

        waiter_id = next(self._waiter_ids)
        with self._condition:
            self._waiters.append(waiter_id)

        try:
            while True:
                wait_seconds = 0.0
                with self._condition:
                    while self._waiters[0] != waiter_id:
                        self._condition.wait()

                    self._synchronize_cross_process_locked()
                    self._rollover_rpd_locked()
                    now_value = self._now()
                    self._prune_locked(now_value)

                    if (
                        self.config.rpd is not None
                        and self._requests_today >= self.config.rpd
                    ):
                        retry_after = self._seconds_until_utc_rollover_locked()
                        if self.config.rpd_policy is RPDPolicy.RAISE:
                            self._waiters.popleft()
                            self._condition.notify_all()
                            raise QuotaExhaustedError(
                                limit_name="rpd",
                                limit=self.config.rpd,
                                window_seconds=86_400.0,
                                used=self._requests_today,
                                requested=1,
                                available=0,
                                retry_after=retry_after,
                            )
                        wait_seconds = min(retry_after, RPD_BLOCK_SLICE_SECONDS)
                    else:
                        wait_seconds = self._window_wait_locked(now_value, estimated_tokens)

                    if wait_seconds <= 0.0:
                        reservation_id = next(self._reservation_ids)
                        charge = _TokenCharge(
                            reservation_id=reservation_id,
                            timestamp=now_value,
                            tokens=estimated_tokens,
                        )
                        self._request_times.append(now_value)
                        self._token_charges.append(charge)
                        self._charges_by_id[reservation_id] = charge
                        self._requests_today += 1
                        self._waiters.popleft()
                        snapshot = self._usage_snapshot_locked()
                        self._publish_cross_process_locked(snapshot)
                        self._condition.notify_all()
                        LOGGER.info(
                            "ratelimit_acquire",
                            extra={
                                "ratelimit_event": "acquire",
                                "reservation_id": reservation_id,
                                "estimated_tokens": estimated_tokens,
                                "requests_in_window": snapshot.requests_in_window,
                                "tokens_in_window": snapshot.tokens_in_window,
                                "requests_today": snapshot.requests_today,
                                "utc_day": snapshot.utc_day.isoformat(),
                            },
                        )
                        return Reservation(self, reservation_id, estimated_tokens)

                    LOGGER.info(
                        "ratelimit_wait",
                        extra={
                            "ratelimit_event": "wait",
                            "waiter_id": waiter_id,
                            "wait_seconds": wait_seconds,
                            "requests_in_window": len(self._request_times),
                            "tokens_in_window": self._tokens_used_locked(),
                            "requests_today": self._requests_today,
                        },
                    )
                self._sleep(wait_seconds + WAIT_EPSILON_SECONDS)
        except BaseException:
            with self._condition:
                try:
                    self._waiters.remove(waiter_id)
                except ValueError:
                    pass
                self._condition.notify_all()
            raise

    def reconcile(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace a live reservation's estimate with actual token usage once."""
        if reservation._limiter is not self:
            raise ReservationError("reservation belongs to a different limiter")
        if (
            isinstance(actual_tokens, bool)
            or not isinstance(actual_tokens, int)
            or actual_tokens < 0
        ):
            raise ValueError("actual_tokens must be a non-negative integer")

        with self._condition:
            self._synchronize_cross_process_locked()
            self._rollover_rpd_locked()
            self._prune_locked(self._now())
            charge = self._charges_by_id.get(reservation.reservation_id)
            if charge is None:
                raise ReservationError("reservation is unknown or outside the active window")
            if charge.reconciled:
                raise ReservationError("reservation has already been reconciled")
            previous = charge.tokens
            charge.tokens = actual_tokens
            charge.reconciled = True
            snapshot = self._usage_snapshot_locked()
            self._publish_cross_process_locked(snapshot)
            self._condition.notify_all()
            LOGGER.info(
                "ratelimit_reconcile",
                extra={
                    "ratelimit_event": "reconcile",
                    "reservation_id": reservation.reservation_id,
                    "estimated_tokens": previous,
                    "actual_tokens": actual_tokens,
                    "tokens_in_window": snapshot.tokens_in_window,
                },
            )


def get_shared_limiter(
    config: LimiterConfig | None = None,
    *,
    now: MonotonicClock = time.monotonic,
    sleep: SleepFunction = time.sleep,
    utc_now: UtcClock = _utc_now,
) -> ProjectRateLimiter:
    """Return the process-wide limiter shared by all three V2 roles."""
    return ProjectRateLimiter.shared(
        config=config, now=now, sleep=sleep, utc_now=utc_now
    )
