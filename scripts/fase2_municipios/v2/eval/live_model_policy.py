"""Closed error taxonomy and bounded free-to-paid Gemini call policy.

No grounding or Google tool surface exists here.  Every provider invocation is
bounded by one global deadline and, by default, isolated in a terminable local
subprocess in addition to the SDK's native request timeout.
"""

from __future__ import annotations

import multiprocessing
import os
import random
import socket
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from scripts.fase2_municipios.v2.agents.base import AgentError
from scripts.fase2_municipios.v2.agents.orchestration import ProposalValidationError
from scripts.fase2_municipios.v2.gemini import (
    GeminiClientError,
    RawResponse,
    SchemaValidationError,
    TokenUsage,
)
from scripts.fase2_municipios.v2.gemini.schema_validation import (
    JsonSchemaValidationError,
    UnsupportedJsonSchemaError,
)
from scripts.fase2_municipios.v2.snapshot import CitationVerificationError, SnapshotError


AUTHORIZED_CREDENTIAL_NAMES = (
    "GEMINI_API_KEY_FREE",
    "GEMINI_API_KEY_FREE_2",
    "GEMINI_API_KEY",
)
REQUIRED_CREDENTIAL_NAMES = ("GEMINI_API_KEY_FREE", "GEMINI_API_KEY")
PROVIDER_NAMES = ("gemini_free_1", "gemini_free_2", "gemini_paid")
MIN_PROVIDER_ATTEMPT_SECONDS = 0.1


class ErrorCategory(str, Enum):
    QUOTA_429 = "quota_429"
    TIMEOUT = "timeout"
    TRANSIENT_5XX = "transient_5xx"
    TRANSPORT_ERROR = "transport_error"
    SCHEMA_INVALID = "schema_invalid"
    QUOTE_INVALID = "quote_invalid"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    SEMANTIC_ERROR = "semantic_error"
    CLIENT_4XX_NO_QUOTA = "client_4xx_no_quota"
    LOCAL_BUG = "local_bug"

    @property
    def fallback_eligible(self) -> bool:
        return self in {
            ErrorCategory.QUOTA_429,
            ErrorCategory.TIMEOUT,
            ErrorCategory.TRANSIENT_5XX,
            ErrorCategory.TRANSPORT_ERROR,
        }


class CredentialConfigError(RuntimeError):
    """The explicit two-key credential file is absent or incomplete."""


class QuoteInvalidError(ValueError):
    pass


class EvidenceInsufficientError(ValueError):
    pass


class SemanticModelError(ValueError):
    pass


class LocalBugError(RuntimeError):
    pass


class PolicyCallError(GeminiClientError):
    """Secret-free terminal error from the bounded model policy."""

    def __init__(
        self,
        category: ErrorCategory,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        original_type: str = "",
    ) -> None:
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        self.original_type = original_type
        super().__init__(f"model_call_failed:{category.value}")


@dataclass(frozen=True)
class ClassifiedError:
    category: ErrorCategory
    status_code: int | None = None
    retry_after: float | None = None

    @property
    def fallback_eligible(self) -> bool:
        return self.category.fallback_eligible


def _status_code(exc: BaseException) -> int | None:
    # Only a concrete HTTP response may supply protocol status. Generic
    # exception ``.code``/``.status`` attributes are local and never authorize
    # paid fallback.
    response = getattr(exc, "response", None)
    if response is not None:
        for name in ("status_code", "status"):
            value = getattr(response, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    # google-genai exposes structured ``code`` on its own concrete error
    # classes, often without an HTTP response object. Never trust a generic
    # exception's similarly named attribute (that could rotate on a local bug).
    error_type = type(exc)
    if error_type.__module__.startswith("google.genai.errors") and error_type.__name__ in {
        "ClientError", "ServerError", "APIError"
    }:
        value = getattr(exc, "code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _retry_after(exc: BaseException) -> float | None:
    direct = getattr(exc, "retry_after", None)
    if isinstance(direct, (int, float)) and not isinstance(direct, bool) and direct >= 0:
        return float(direct)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)
    if not isinstance(raw, str):
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_error(exc: BaseException) -> ClassifiedError:
    """Classify only by concrete type and numeric protocol status."""

    if isinstance(exc, PolicyCallError):
        return ClassifiedError(exc.category, exc.status_code, exc.retry_after)
    if isinstance(
        exc,
        (SchemaValidationError, JsonSchemaValidationError, UnsupportedJsonSchemaError, ValidationError),
    ):
        return ClassifiedError(ErrorCategory.SCHEMA_INVALID)
    if isinstance(exc, (QuoteInvalidError, CitationVerificationError)):
        return ClassifiedError(ErrorCategory.QUOTE_INVALID)
    if isinstance(exc, EvidenceInsufficientError):
        return ClassifiedError(ErrorCategory.EVIDENCE_INSUFFICIENT)
    if isinstance(exc, SnapshotError):
        return ClassifiedError(ErrorCategory.EVIDENCE_INSUFFICIENT)
    if isinstance(exc, SemanticModelError):
        return ClassifiedError(ErrorCategory.SEMANTIC_ERROR)
    if isinstance(exc, (AgentError, ProposalValidationError)):
        return ClassifiedError(ErrorCategory.SEMANTIC_ERROR)
    if isinstance(exc, LocalBugError):
        return ClassifiedError(ErrorCategory.LOCAL_BUG)

    status = _status_code(exc)
    retry_after = _retry_after(exc)
    if status == 429:
        return ClassifiedError(ErrorCategory.QUOTA_429, status, retry_after)
    if status == 408:
        return ClassifiedError(ErrorCategory.TIMEOUT, status, retry_after)
    if status is not None and 500 <= status <= 599:
        return ClassifiedError(ErrorCategory.TRANSIENT_5XX, status, retry_after)
    if status is not None and 400 <= status <= 499:
        return ClassifiedError(ErrorCategory.CLIENT_4XX_NO_QUOTA, status, retry_after)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ClassifiedError(ErrorCategory.TIMEOUT, retry_after=retry_after)
    if isinstance(exc, (ConnectionError, socket.gaierror, BrokenPipeError, OSError)):
        return ClassifiedError(ErrorCategory.TRANSPORT_ERROR, retry_after=retry_after)
    return ClassifiedError(ErrorCategory.LOCAL_BUG)


def load_model_credentials(path: Path) -> dict[str, str]:
    """Read the three authorized names; FREE2 is backward-compatible optional."""

    path = Path(path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CredentialConfigError("gemini_credential_file_unreadable") from exc
    loaded: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if name not in AUTHORIZED_CREDENTIAL_NAMES:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            loaded[name] = value
    missing = [name for name in REQUIRED_CREDENTIAL_NAMES if not loaded.get(name)]
    if missing:
        raise CredentialConfigError("gemini_authorized_credentials_missing:" + ",".join(missing))
    return loaded


class ModelPolicyTelemetry:
    """Local approximate quota, fallback and usage telemetry."""

    def __init__(self, observer: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        self.observer = observer
        self.free_calls = 0
        self.paid_calls = 0
        self.quota_429 = 0
        self.tokens = 0
        self.cost = 0.0
        self.cost_reported = False
        self.paid_fallback_reasons: Counter[str] = Counter()
        self.fallback_events: list[dict[str, Any]] = []
        self.backoff_events: list[dict[str, Any]] = []
        self._calls: deque[tuple[float, int]] = deque()
        self._model_usage: dict[str, Counter[str]] = {}
        self._provider_usage: dict[str, Counter[str]] = {
            provider: Counter() for provider in PROVIDER_NAMES
        }

    def set_observer(self, observer: Callable[[Mapping[str, Any]], None] | None) -> None:
        self.observer = observer

    def _emit(self, event: Mapping[str, Any]) -> None:
        if self.observer is not None:
            self.observer(dict(event))

    def record_call(
        self,
        provider: str,
        response: RawResponse | None = None,
        *,
        municipio: str = "",
        bucket: str = "",
        stage: str = "",
        model: str = "",
        attempt: int = 0,
        status: str = "ok",
        error_class: str = "",
    ) -> None:
        if provider == "gemini_free":  # compatibility for injected legacy wrappers
            provider = "gemini_free_1"
        if provider not in PROVIDER_NAMES:
            raise ValueError(f"unknown_model_provider:{provider}")
        if provider in {"gemini_free_1", "gemini_free_2"}:
            self.free_calls += 1
        else:
            self.paid_calls += 1
        tokens = 0
        if response is not None and isinstance(response.usage, TokenUsage):
            tokens = max(0, response.usage.total_tokens)
            self.tokens += tokens
        cost = getattr(response, "cost", None) if response is not None else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            self.cost += float(cost)
            self.cost_reported = True
        self._calls.append((time.monotonic(), tokens))
        provider_usage = self._provider_usage[provider]
        provider_usage["calls"] += 1
        provider_usage["tokens"] += tokens
        if status == "error":
            provider_usage["errors"] += 1
            if error_class == ErrorCategory.QUOTA_429.value:
                provider_usage["quota_rate"] += 1
        if model:
            usage = self._model_usage.setdefault(model, Counter())
            usage[
                "free_calls"
                if provider in {"gemini_free_1", "gemini_free_2"}
                else "paid_calls"
            ] += 1
            usage["tokens"] += tokens
        self._emit({
            "event": "model_call",
            "municipio": municipio,
            "bucket": bucket,
            "stage": stage,
            "model": model,
            "provider": provider,
            "attempt": attempt,
            "status": status,
            "error_class": error_class,
            "tokens": tokens,
        })

    def record_error(self, category: ErrorCategory) -> None:
        if category is ErrorCategory.QUOTA_429:
            self.quota_429 += 1

    def record_backoff(self, *, seconds: float, category: ErrorCategory, **context: Any) -> None:
        event = {"seconds": round(seconds, 6), "cause": category.value, **context}
        self.backoff_events.append(event)
        self._emit({"event": "backoff", **event})

    def record_fallback(self, *, category: ErrorCategory, **context: Any) -> None:
        if context.get("to_provider") == "gemini_paid" or context.get("provider") == "gemini_paid":
            self.paid_fallback_reasons[category.value] += 1
        event = {"cause": category.value, **context}
        self.fallback_events.append(event)
        self._emit({"event": "fallback", **event})

    def summary(self) -> dict[str, Any]:
        now = time.monotonic()
        while self._calls and now - self._calls[0][0] > 60:
            self._calls.popleft()
        summary = {
            "free_calls": self.free_calls,
            "paid_calls": self.paid_calls,
            "paid_fallback_reasons": dict(sorted(self.paid_fallback_reasons.items())),
            "tokens": self.tokens,
            "quota_429": self.quota_429,
            "approx_rpm": len(self._calls),
            "approx_tpm": sum(tokens for _, tokens in self._calls),
            "approx_rpd": self.free_calls + self.paid_calls,
            "providers": {
                provider: {
                    "calls": self._provider_usage[provider]["calls"],
                    "tokens": self._provider_usage[provider]["tokens"],
                    "errors": self._provider_usage[provider]["errors"],
                    "quota_rate": self._provider_usage[provider]["quota_rate"],
                }
                for provider in PROVIDER_NAMES
            },
            "models": {
                model: {
                    "free_calls": usage["free_calls"],
                    "paid_calls": usage["paid_calls"],
                    "tokens": usage["tokens"],
                }
                for model, usage in sorted(self._model_usage.items())
            },
        }
        if self.cost_reported:
            summary["cost"] = self.cost
        return summary


def _isolated_worker(queue, transport, model, contents, config) -> None:
    try:
        queue.put(("ok", transport.generate(model, contents, config)))
    except BaseException as exc:  # child boundary must return a closed descriptor
        classified = classify_error(exc)
        queue.put((
            "error",
            classified.category.value,
            classified.status_code,
            classified.retry_after,
            type(exc).__name__,
        ))


def _invoke_isolated(
    transport: Any,
    model: str,
    contents: Any,
    config: Mapping[str, Any],
    timeout_seconds: float,
) -> RawResponse:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError as exc:
        raise PolicyCallError(ErrorCategory.LOCAL_BUG, original_type=type(exc).__name__) from exc
    queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_isolated_worker,
        args=(queue, transport, model, contents, dict(config)),
        daemon=True,
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join()
        queue.close()
        queue.join_thread()
        raise PolicyCallError(ErrorCategory.TIMEOUT, original_type="DeadlineExceeded")
    try:
        payload = queue.get(timeout=0.2)
    except Exception as exc:
        raise PolicyCallError(ErrorCategory.LOCAL_BUG, original_type="ChildNoResult") from exc
    finally:
        queue.close()
        queue.join_thread()
    if payload[0] == "ok":
        return payload[1]
    raise PolicyCallError(
        ErrorCategory(payload[1]),
        status_code=payload[2],
        retry_after=payload[3],
        original_type=payload[4],
    )


class PolicyTransport:
    """FREE1 -> optional FREE2 -> PAID, only across eligible failures."""

    def __init__(
        self,
        *,
        free_transport: Any,
        paid_transport: Any | None,
        free_2_transport: Any | None = None,
        model: str,
        stage: str,
        telemetry: ModelPolicyTelemetry,
        timeout_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        isolate_calls: bool = True,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("gemini_timeout_must_be_positive")
        self.free_transport = free_transport
        self.free_2_transport = free_2_transport
        self.paid_transport = paid_transport
        self.model = model
        self.stage = stage
        self.telemetry = telemetry
        self.timeout_seconds = float(timeout_seconds)
        self.sleep = sleep
        self.jitter = jitter
        self.isolate_calls = isolate_calls
        self.municipio = "unknown"
        self.bucket = "unknown"

    def set_unit(self, municipio: str, bucket: str) -> None:
        self.municipio = municipio
        self.bucket = bucket

    def _call(self, transport: Any, model: str, contents: Any, config: Mapping[str, Any], remaining: float) -> RawResponse:
        if remaining <= 0:
            raise PolicyCallError(ErrorCategory.LOCAL_BUG, original_type="GlobalDeadline")
        if self.isolate_calls:
            return _invoke_isolated(transport, model, contents, config, remaining)
        return transport.generate(model, contents, config)

    def _context(self, *, provider: str, attempt: int) -> dict[str, Any]:
        return {
            "municipio": self.municipio,
            "bucket": self.bucket,
            "stage": self.stage,
            "model": self.model,
            "provider": provider,
            "attempt": attempt,
        }

    def generate(self, model: str, contents: Any, config: Mapping[str, Any]) -> RawResponse:
        deadline = time.monotonic() + self.timeout_seconds
        last: ClassifiedError | None = None
        # If both free keys belong to the same Google project they probably
        # share quota; FREE2 then improves resilience, not available capacity.
        free_steps = (
            (("gemini_free_1", self.free_transport), ("gemini_free_2", self.free_2_transport))
            if self.free_2_transport is not None
            else (("gemini_free_1", self.free_transport), ("gemini_free_1", self.free_transport))
        )
        for attempt, (provider, selected_transport) in enumerate(free_steps, start=1):
            context = self._context(provider=provider, attempt=attempt)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PolicyCallError(
                    ErrorCategory.LOCAL_BUG, original_type="GlobalDeadline"
                )
            if attempt > 1:
                assert last is not None
                self.telemetry.record_fallback(
                    category=last.category,
                    from_provider=free_steps[attempt - 2][0],
                    to_provider=provider,
                    **{key: value for key, value in context.items() if key != "provider"},
                )
            try:
                response = self._call(
                    selected_transport, model, contents, config, remaining
                )
            except BaseException as exc:
                last = classify_error(exc)
                self.telemetry.record_call(
                    provider,
                    municipio=self.municipio,
                    bucket=self.bucket,
                    stage=self.stage,
                    model=self.model,
                    attempt=attempt,
                    status="error",
                    error_class=last.category.value,
                )
                self.telemetry.record_error(last.category)
                if not last.fallback_eligible:
                    raise PolicyCallError(
                        last.category,
                        status_code=last.status_code,
                        retry_after=last.retry_after,
                        original_type=type(exc).__name__,
                    ) from exc
                if attempt == 1:
                    next_provider = free_steps[1][0]
                if attempt == 1 and next_provider == provider:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PolicyCallError(
                            ErrorCategory.LOCAL_BUG,
                            original_type="GlobalDeadline",
                        ) from exc
                    exponential = 1.0 + max(0.0, min(1.0, float(self.jitter())))
                    delay = max(exponential, last.retry_after or 0.0)
                    delay = min(delay, remaining)
                    self.telemetry.record_backoff(seconds=delay, category=last.category, **context)
                    self.sleep(delay)
                    continue
                if attempt == 1:
                    continue
                break
            else:
                self.telemetry.record_call(
                    provider,
                    response,
                    municipio=self.municipio,
                    bucket=self.bucket,
                    stage=self.stage,
                    model=self.model,
                    attempt=attempt,
                    status="ok",
                )
                return response

        assert last is not None and last.fallback_eligible
        if self.paid_transport is None:
            raise PolicyCallError(
                last.category,
                status_code=last.status_code,
                retry_after=last.retry_after,
                original_type="FreeOnlyProvidersExhausted",
            )
        remaining = deadline - time.monotonic()
        if remaining < MIN_PROVIDER_ATTEMPT_SECONDS:
            raise PolicyCallError(
                ErrorCategory.LOCAL_BUG, original_type="GlobalDeadline"
            )
        paid_context = self._context(provider="gemini_paid", attempt=3)
        self.telemetry.record_fallback(
            category=last.category,
            from_provider=free_steps[-1][0],
            to_provider="gemini_paid",
            **{key: value for key, value in paid_context.items() if key != "provider"},
        )
        try:
            response = self._call(
                self.paid_transport, model, contents, config, remaining
            )
        except BaseException as exc:
            classified = classify_error(exc)
            self.telemetry.record_call(
                "gemini_paid",
                municipio=self.municipio,
                bucket=self.bucket,
                stage=self.stage,
                model=self.model,
                attempt=3,
                status="error",
                error_class=classified.category.value,
            )
            self.telemetry.record_error(classified.category)
            raise PolicyCallError(
                classified.category,
                status_code=classified.status_code,
                retry_after=classified.retry_after,
                original_type=type(exc).__name__,
            ) from exc
        self.telemetry.record_call(
            "gemini_paid",
            response,
            municipio=self.municipio,
            bucket=self.bucket,
            stage=self.stage,
            model=self.model,
            attempt=3,
            status="ok",
        )
        return response


__all__ = [
    "AUTHORIZED_CREDENTIAL_NAMES", "ClassifiedError", "CredentialConfigError",
    "ErrorCategory", "EvidenceInsufficientError", "LocalBugError",
    "ModelPolicyTelemetry", "PolicyCallError", "PolicyTransport",
    "QuoteInvalidError", "SemanticModelError", "classify_error",
    "load_model_credentials", "MIN_PROVIDER_ATTEMPT_SECONDS",
]
