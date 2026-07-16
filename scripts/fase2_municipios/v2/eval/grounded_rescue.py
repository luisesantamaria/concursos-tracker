"""Grounded URL-candidate rescue runner for the F2 V2 adjudication gate.

POLITICA DE LUIS (obligatoria, 2026-07-13):
- El unico modelo autorizado para este rescate es exactamente
  ``gemini-3.1-flash-lite``. No se permite 2.5 Flash, Pro ni variantes
  ``-preview``; REQUIRED_MODEL y FALLBACK_MODEL quedan fijados al mismo ID
  para que no exista rotacion de modelo.
- En ``--free-only`` la topologia es FREE1 -> FREE2 -> STOP: el proveedor
  paid no se agrega a la secuencia ni se construye. Produccion solo puede usar
  FREE1 -> FREE2 -> PAID con permiso explicito y configuracion separada.
- Herramienta google_search (grounding de busqueda). NO usar retrieval
  Default. NO usar Map Grounding.
- Maximo 5 busquedas grounded por unidad.
- El grounding PROPONE URLs y evidencia pero NUNCA confirma; solo se
  registran candidatas. La confirmacion siempre es del gate
  deterministico/adjudicacion V2 despues.

This module is deliberately separate from the normal structured Gemini
client: that client correctly forbids every grounding tool.  Importing this
module performs no credential lookup, SDK construction, HTTP request, or
model call.  All side effects happen from ``main`` or injected functions.
"""

from __future__ import annotations

import argparse
import csv
import html
import ipaddress
import json
import logging
import os
import random
import re
import signal
import socket
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from scripts.fase2_municipios.v2 import authority
from scripts.fase2_municipios.v2.agents import certifier
from scripts.fase2_municipios.v2.eval.live_abc_adapter import render_page_networkidle
from scripts.fase2_municipios.v2.eval import f3_multi24_adapter
from scripts.fase2_municipios.v2.eval.f3_adapters_dispatch import (
    detect_platform,
    dispatch_f3_adapter,
)
from scripts.fase2_municipios.v2.eval.live_model_policy import (
    ErrorCategory,
    classify_error,
)
from scripts.fase2_municipios.v2.eval.platform_probe_runner import (
    Fetcher,
    RequestsFetcher,
    _count_item_markers,
    _norm,
    extract_title_and_text,
)


LOGGER = logging.getLogger(__name__)
REQUIRED_MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODEL = REQUIRED_MODEL
MAX_POLICY_SEARCHES = 5
MAX_OPERATIONAL_RPM = 12
MIN_MODEL_INTERVAL_SECONDS = 60.0 / MAX_OPERATIONAL_RPM
DEFAULT_DAILY_MODEL_LIMIT = 500
DEFAULT_DAILY_SEARCH_LIMIT = 500
QUOTA_STOP_FRACTION = 0.90
SNIPPET_LIMIT = 500
SNAPSHOT_LIMIT = 8000
MULTI24_CHILD_LIMIT = 10
MULTI24_FETCH_TIMEOUT = 10
MULTI24_OFFICIAL_SUBPAGE_LIMIT = 5
MULTI24_OFFICIAL_NAV_HINT_RE = re.compile(
    r"\b(?:transparencia|portal|acesso\s+a\s+informacao|servicos?)\b"
)
URL_PATTERN = re.compile(r"https?://[^\s<>\]\[{}\"']+", re.IGNORECASE)
SUB_CAUSAS = {"url_mala", "render_incierto", "dificil_rederivado"}
OUTPUT_COLUMNS = (
    "municipio",
    "bucket",
    "url_candidata",
    "query_usada",
    "snippet_grounding",
    "host_oficial_check",
    "item_markers",
    "http_status",
    "fuente",
    "redirector_original",
    "disposition",
    "confirmed",
    "provenance",
)
PROVIDERS = ("gemini_free_1", "gemini_free_2", "gemini_paid")
PAID_AUTHORIZATION = "explicita_luis_20260714"
TELEMETRY_COUNTERS = (
    "model_requests", "successful_model_responses", "google_search_queries",
    "query_count_unknown", "grounded_responses", "quota_429", "paid_calls",
)
UNIT_SCHEMA_VERSION = 1
MAX_GROUNDING_REDIRECTS = 3  # also the hard maximum HTTP requests per wrapper URL
REDIRECT_RESOLUTION_TIMEOUT = 10
# Add a host here only after confirming that it is an official Google
# grounding redirector. Redirect recognition is intentionally fail-closed.
GOOGLE_GROUNDING_REDIRECT_HOSTS = frozenset({
    "vertexaisearch.cloud.google.com",
})
GOOGLE_QUERY_REDIRECT_HOSTS = frozenset({"google.com", "www.google.com"})
_BLOCKED_REDIRECT_HOSTS = frozenset({
    "localhost",
    "metadata.google.internal",
})


@dataclass(frozen=True)
class Target:
    municipio: str
    bucket: str
    pista: str
    sub_causa: str = "url_mala"


@dataclass(frozen=True)
class GroundedAnswer:
    text: str
    grounding_urls: tuple[str, ...] = ()
    grounding_snippets: tuple[str, ...] = ()
    model: str = REQUIRED_MODEL
    provider: str = ""
    fallbacks: tuple[dict[str, str], ...] = ()
    google_search_query_count: int | None = None
    grounded: bool = False


@dataclass(frozen=True)
class CandidateRow:
    municipio: str
    bucket: str
    url_candidata: str
    query_usada: str
    snippet_grounding: str
    host_oficial_check: str
    item_markers: int
    http_status: str
    fuente: str = "grounding"
    redirector_original: str = ""
    disposition: str = "propose"
    confirmed: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.confirmed:
            raise ValueError("candidate_rows_never_confirm")
        if self.disposition not in {"propose", "revisar"}:
            raise ValueError("candidate_disposition_must_be_propose_or_revisar")

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in OUTPUT_COLUMNS}


class GroundedClient(Protocol):
    telemetry: Mapping[str, Any]

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        """Perform one grounded search intent and return candidate evidence."""


class _AdaptersOnlyModelGuard:
    """Structural bomb: adapters-only must never reach a model client."""

    telemetry = {"providers": {provider: {} for provider in PROVIDERS}}

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        raise AssertionError("adapters_only_model_client_invoked")


class ExplicitModelRejection(RuntimeError):
    """An API provider explicitly rejected the exact required model."""

    def __init__(self, exact_error: str, provider: str = "") -> None:
        self.exact_error = exact_error
        self.provider = provider
        super().__init__(exact_error)


class RescueInterrupted(RuntimeError):
    """Cooperative stop used by SIGINT/SIGTERM handling."""


class DailyQuotaExhausted(RuntimeError):
    """FREE2 reported daily exhaustion; the run must checkpoint and stop."""


class PreventiveQuotaStop(RuntimeError):
    """The 90% model/search quota brake or global call budget fired."""


class PaidCallCapReached(RuntimeError):
    """The authorized run-wide paid-call cap blocked the next paid request."""


class PolicyFailure(RuntimeError):
    """A free-only invariant was violated."""


@dataclass
class QuotaGovernor:
    """Run-wide 12 RPM limiter and preventive daily/budget brake."""

    global_call_budget: int
    daily_model_limit: int = DEFAULT_DAILY_MODEL_LIMIT
    daily_search_limit: int = DEFAULT_DAILY_SEARCH_LIMIT
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = random.random
    min_interval_seconds: float = MIN_MODEL_INTERVAL_SECONDS
    last_request_at: float | None = None

    def before_request(self, *, model_requests: int, google_search_queries: int) -> None:
        if self.global_call_budget < 1 or model_requests >= self.global_call_budget:
            raise PreventiveQuotaStop("global_call_budget_exhausted")
        model_stop = int(self.daily_model_limit * QUOTA_STOP_FRACTION)
        search_stop = int(self.daily_search_limit * QUOTA_STOP_FRACTION)
        # Stop before request/search number 450 when the active limit is 500.
        if model_requests + 1 >= model_stop:
            raise PreventiveQuotaStop("preventive_90pct_model_requests")
        # A grounded response may report several real queries; reserve the
        # per-intent policy maximum so a single response cannot cross 90%.
        if google_search_queries + MAX_POLICY_SEARCHES >= search_stop:
            raise PreventiveQuotaStop("preventive_90pct_google_search_queries")
        now = self.clock()
        if self.last_request_at is not None:
            remaining = self.min_interval_seconds - (now - self.last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now = max(self.clock(), self.last_request_at + self.min_interval_seconds)
        self.last_request_at = now

    def backoff_seconds(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return max(0.0, retry_after)
        return (2.0 ** max(0, attempt - 1)) + max(0.0, min(1.0, self.jitter()))


@dataclass
class InterruptionState:
    requested: bool = False
    signal_name: str = ""

    def handle(self, signum: int, _frame: Any) -> None:
        self.requested = True
        try:
            self.signal_name = signal.Signals(signum).name
        except ValueError:
            self.signal_name = str(signum)
        raise RescueInterrupted("interrupted")

    def raise_if_requested(self) -> None:
        if self.requested:
            raise RescueInterrupted("interrupted")


def _safe_error(exc: BaseException, secret_values: Sequence[str]) -> str:
    """Preserve the SDK error text while redacting any credential occurrence."""
    text = str(exc) or type(exc).__name__
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:2000]


def _is_explicit_model_rejection(exc: BaseException, model: str) -> bool:
    if bool(getattr(exc, "pro_rejected", False)):
        return True
    classified = classify_error(exc)
    sdk_code = getattr(exc, "code", None)
    is_explicit_4xx = (
        classified.category is ErrorCategory.CLIENT_4XX_NO_QUOTA
        or (isinstance(sdk_code, int) and not isinstance(sdk_code, bool) and 400 <= sdk_code <= 499 and sdk_code != 429)
    )
    if not is_explicit_4xx:
        return False
    text = str(exc).casefold()
    model_tokens = (model.casefold(), model.removeprefix("models/").casefold())
    rejection = any(
        marker in text
        for marker in (
            "not found",
            "unsupported",
            "not supported",
            "not available",
            "no longer available",
            "invalid model",
        )
    )
    return rejection and any(token in text for token in model_tokens)


class GeminiGroundedClient:
    """google-genai adapter with structurally separate free-only routing."""

    def __init__(
        self,
        credentials: Mapping[str, str],
        *,
        client_factory: Callable[..., Any] | None = None,
        free_only: bool = False,
        max_paid_calls: int | None = None,
        global_call_budget: int = 100,
        daily_model_limit: int = DEFAULT_DAILY_MODEL_LIMIT,
        daily_search_limit: int = DEFAULT_DAILY_SEARCH_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        max_free2_attempts: int = 3,
    ) -> None:
        if not credentials.get("GEMINI_API_KEY_FREE"):
            raise ValueError("gemini_free1_credential_missing")
        if not free_only and not credentials.get("GEMINI_API_KEY"):
            raise ValueError("gemini_paid_credential_missing_for_production")
        if max_paid_calls is not None and max_paid_calls < 1:
            raise ValueError("max_paid_calls_debe_ser_positivo")
        if client_factory is None:
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("google-genai no esta instalado") from exc
            client_factory = genai.Client
        allowed_names = ("GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2") if free_only else (
            "GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2", "GEMINI_API_KEY"
        )
        self._credentials = {name: credentials[name] for name in allowed_names if credentials.get(name)}
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}
        self.free_only = free_only
        self.max_paid_calls = max_paid_calls
        self._sleep = sleep
        self._max_free2_attempts = max(1, max_free2_attempts)
        self._governor = QuotaGovernor(
            global_call_budget=global_call_budget,
            daily_model_limit=daily_model_limit,
            daily_search_limit=daily_search_limit,
            clock=clock,
            sleep=sleep,
            jitter=jitter,
        )
        self._calls: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._responses: Counter[str] = Counter()
        self._tokens: Counter[str] = Counter()
        self._quota_rate: Counter[str] = Counter()
        self._fallback_events: list[dict[str, str]] = []
        self._capacity_veto: dict[tuple[str, str], str] = {}
        self._capacity_errors: dict[tuple[str, str], str] = {}
        self._model_requests = 0
        self._successful_model_responses = 0
        self._google_search_queries = 0
        self._query_count_unknown = 0
        self._grounded_responses = 0
        self._quota_429 = 0

    @property
    def telemetry(self) -> dict[str, Any]:
        providers = {}
        for provider in ("gemini_free_1", "gemini_free_2", "gemini_paid"):
            providers[provider] = {
                "calls": self._calls[provider],
                "errors": self._errors[provider],
                "responses": self._responses[provider],
                "tokens": self._tokens[provider],
                "quota_rate": self._quota_rate[provider],
            }
        return {
            "providers": providers,
            "fallback_events": list(self._fallback_events),
            "capacidad_vetada": [
                (provider, model, cause)
                for (provider, model), cause in self._capacity_veto.items()
            ],
            "paid_calls": self._calls["gemini_paid"],
            "model_requests": self._model_requests,
            "successful_model_responses": self._successful_model_responses,
            "google_search_queries": self._google_search_queries,
            "query_count_unknown": self._query_count_unknown,
            "grounded_responses": self._grounded_responses,
            "calls_by_provider": {name: self._calls[name] for name in PROVIDERS},
            "responses_by_provider": {name: self._responses[name] for name in PROVIDERS},
            "errors_by_provider": {name: self._errors[name] for name in PROVIDERS},
            "quota_429": self._quota_429,
        }

    @staticmethod
    def _config() -> dict[str, Any]:
        # Exactly Google Search grounding. No retrieval and no map grounding.
        return {
            "tools": [{"google_search": {}}],
            "temperature": 0.0,
            "max_output_tokens": 2048,
        }

    def _invoke(self, provider: str, model: str, prompt: str) -> Any:
        if model != REQUIRED_MODEL:
            raise ValueError(f"model_debe_ser_exactamente_{REQUIRED_MODEL}")
        if self.free_only and provider == "gemini_paid":
            raise PolicyFailure("FALLO_DE_POLITICA:paid_provider_reachable")
        if (
            provider == "gemini_paid"
            and self.max_paid_calls is not None
            and self._calls[provider] >= self.max_paid_calls
        ):
            raise PaidCallCapReached("paid_cap_alcanzado")
        self._governor.before_request(
            model_requests=self._model_requests,
            google_search_queries=self._google_search_queries,
        )
        self._model_requests += 1
        self._calls[provider] += 1
        try:
            if provider not in self._clients:
                credential_name = {
                    "gemini_free_1": "GEMINI_API_KEY_FREE",
                    "gemini_free_2": "GEMINI_API_KEY_FREE_2",
                    "gemini_paid": "GEMINI_API_KEY",
                }[provider]
                key = self._credentials.get(credential_name, "")
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("gemini_authorized_credential_missing")
                self._clients[provider] = self._client_factory(
                    api_key=key, vertexai=False
                )
            response = self._clients[provider].models.generate_content(
                model=model,
                contents=prompt,
                config=self._config(),
            )
        except RescueInterrupted:
            raise
        except BaseException as exc:
            self._errors[provider] += 1
            if classify_error(exc).category is ErrorCategory.QUOTA_429:
                self._quota_rate[provider] += 1
                self._quota_429 += 1
            raise
        self._responses[provider] += 1
        self._successful_model_responses += 1
        usage = getattr(response, "usage_metadata", None)
        total = getattr(usage, "total_token_count", 0) if usage is not None else 0
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self._tokens[provider] += total
        query_count, grounded = extract_grounding_usage(response)
        if query_count is None:
            self._query_count_unknown += 1
        else:
            self._google_search_queries += query_count
        if grounded:
            self._grounded_responses += 1
        return response

    def _veto_capacity(self, provider: str, model: str, exc: BaseException) -> None:
        key = (provider, model)
        if key in self._capacity_veto:
            return
        self._capacity_veto[key] = "model_unavailable_for_provider"
        self._capacity_errors[key] = _safe_error(exc, self._secret_values())

    def _veto_free_quota_for_run(
        self, provider: str, model: str, exc: BaseException
    ) -> None:
        """Veto one exhausted FREE key until this client/run ends."""

        if provider not in {"gemini_free_1", "gemini_free_2"}:
            return
        key = (provider, model)
        if key in self._capacity_veto:
            return
        self._capacity_veto[key] = "quota_429_run_veto"
        self._capacity_errors[key] = _safe_error(exc, self._secret_values())

    def _next_available_provider(
        self,
        steps: Sequence[str],
        start: int,
        model: str,
    ) -> str | None:
        for provider in steps[start:]:
            if (provider, model) not in self._capacity_veto:
                return provider
        return None

    def _key_policy_call(self, model: str, prompt: str) -> tuple[Any, str, list[dict[str, str]]]:
        last: BaseException | None = None
        last_provider = ""
        last_cause = ""
        events: list[dict[str, str]] = []
        free_steps = (
            ("gemini_free_1", "gemini_free_2")
            if "GEMINI_API_KEY_FREE_2" in self._credentials
            else ("gemini_free_1", "gemini_free_1")
        )
        steps = (*free_steps, "gemini_paid")
        for index, provider in enumerate(steps):
            if (provider, model) in self._capacity_veto:
                continue
            try:
                return self._invoke(provider, model, prompt), provider, events
            except RescueInterrupted:
                raise
            except BaseException as exc:
                if _is_explicit_model_rejection(exc, model):
                    self._veto_capacity(provider, model, exc)
                    # El modelo autorizado no existe en el proyecto de ESTA
                    # clave (p.ej. "no longer available to new users"); la
                    # siguiente clave puede pertenecer a otro proyecto con
                    # acceso: rotación de CLAVE, nunca de modelo.
                    last = exc
                    last_provider = provider
                    rotation_cause = "model_unavailable_for_provider"
                    rotation_retry_after = 0.0
                else:
                    classified = classify_error(exc)
                    if not classified.fallback_eligible:
                        raise
                    last = exc
                    last_provider = provider
                    rotation_cause = classified.category.value
                    rotation_retry_after = classified.retry_after or 0.0
                last_cause = rotation_cause
                next_provider = self._next_available_provider(steps, index + 1, model)
                if next_provider is not None:
                    event = {
                        "from_provider": provider,
                        "to_provider": next_provider,
                        "cause": rotation_cause,
                    }
                    events.append(event)
                    self._fallback_events.append(dict(event))
                    if next_provider == provider:
                        self._sleep(max(1.0, rotation_retry_after))
        if last is None:
            for provider in reversed(steps):
                if (provider, model) in self._capacity_veto:
                    raise ExplicitModelRejection(
                        self._capacity_errors.get(
                            (provider, model), "model_unavailable_for_provider"
                        ),
                        provider,
                    )
            raise RuntimeError("provider_policy_without_attempt")
        if last_cause == "model_unavailable_for_provider":
            raise ExplicitModelRejection(
                self._capacity_errors.get(
                    (last_provider, model),
                    _safe_error(last, self._secret_values()),
                ),
                last_provider,
            ) from last
        raise last

    def _record_free_event(
        self, events: list[dict[str, str]], provider: str, next_provider: str, cause: str
    ) -> None:
        event = {"from_provider": provider, "to_provider": next_provider, "cause": cause}
        events.append(event)
        self._fallback_events.append(dict(event))

    @staticmethod
    def _daily_quota_exhausted(exc: BaseException) -> bool:
        if classify_error(exc).category is not ErrorCategory.QUOTA_429:
            return False
        text = str(exc).casefold()
        return any(marker in text for marker in (
            "per day", "daily", "requests per day", "rpd",
            "generate_content_free_tier_requests", "quota metric: generatecontent",
        ))

    def _free_steps(self) -> tuple[str, ...]:
        if self._credentials.get("GEMINI_API_KEY_FREE_2"):
            return ("gemini_free_1", "gemini_free_2")
        return ("gemini_free_1",)

    def _free_only_call(self, model: str, prompt: str) -> tuple[Any, str, list[dict[str, str]]]:
        """FREE1 -> FREE2 -> STOP; paid is structurally absent."""
        last: BaseException | None = None
        events: list[dict[str, str]] = []
        steps = self._free_steps()
        for index, provider in enumerate(steps):
            if (provider, model) in self._capacity_veto:
                continue
            attempts = self._max_free2_attempts if provider == "gemini_free_2" else 1
            for attempt in range(1, attempts + 1):
                try:
                    return self._invoke(provider, model, prompt), provider, events
                except (RescueInterrupted, PreventiveQuotaStop, PolicyFailure):
                    raise
                except BaseException as exc:
                    last = exc
                    next_provider = steps[index + 1] if index + 1 < len(steps) else "STOP"
                    if _is_explicit_model_rejection(exc, model):
                        self._veto_capacity(provider, model, exc)
                        self._record_free_event(
                            events, provider, next_provider, "model_unavailable_for_provider"
                        )
                        break
                    classified = classify_error(exc)
                    if not classified.fallback_eligible:
                        raise
                    if classified.category is ErrorCategory.QUOTA_429:
                        self._veto_free_quota_for_run(provider, model, exc)
                    if provider == "gemini_free_2" and classified.category is ErrorCategory.QUOTA_429:
                        if self._daily_quota_exhausted(exc):
                            raise DailyQuotaExhausted("free2_daily_quota_exhausted") from exc
                        delay = self._governor.backoff_seconds(attempt, classified.retry_after)
                        self._sleep(delay)
                    elif classified.category is ErrorCategory.QUOTA_429:
                        # Retry-After/backoff applies even when the next attempt
                        # rotates to another free provider.
                        self._sleep(self._governor.backoff_seconds(attempt, classified.retry_after))
                    self._record_free_event(events, provider, next_provider, classified.category.value)
                    break
        if last is None:
            raise RuntimeError("free_provider_policy_without_attempt")
        raise last

    def _production_call(self, model: str, prompt: str) -> tuple[Any, str, list[dict[str, str]]]:
        """Authorized production extension; never used by ``--free-only``."""
        if all((provider, model) in self._capacity_veto for provider in self._free_steps()):
            event = {
                "from_provider": self._free_steps()[-1],
                "to_provider": "gemini_paid",
                "cause": "quota_429_run_veto",
            }
            self._fallback_events.append(dict(event))
            response = self._invoke("gemini_paid", model, prompt)
            return response, "gemini_paid", [event]
        try:
            return self._free_only_call(model, prompt)
        except (
            RescueInterrupted,
            PreventiveQuotaStop,
            DailyQuotaExhausted,
            PaidCallCapReached,
            PolicyFailure,
        ):
            raise
        except BaseException as exc:
            classified = classify_error(exc)
            if not classified.fallback_eligible and not _is_explicit_model_rejection(exc, model):
                raise
            cause = (
                "model_unavailable_for_provider"
                if _is_explicit_model_rejection(exc, model)
                else classified.category.value
            )
            event = {
                "from_provider": self._free_steps()[-1],
                "to_provider": "gemini_paid",
                "cause": cause,
            }
            self._fallback_events.append(dict(event))
            response = self._invoke("gemini_paid", model, prompt)
            return response, "gemini_paid", [event]

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        prompt = (
            "Atue somente como descobridor de URLs candidatas. Use Google Search. "
            "Nao confirme nem adjudique a URL. Encontre paginas oficiais de indice/listagem, "
            "nunca PDF, noticia individual ou edital individual. Responda com URLs completas "
            "e uma evidencia curta para cada uma. Consulta: " + query
        )
        if model != REQUIRED_MODEL:
            raise ValueError(f"model_debe_ser_exactamente_{REQUIRED_MODEL}")
        fallbacks: list[dict[str, str]] = []
        actual_model = REQUIRED_MODEL
        try:
            if self.free_only:
                response, provider, key_fallbacks = self._free_only_call(model, prompt)
            else:
                response, provider, key_fallbacks = self._production_call(model, prompt)
            fallbacks.extend(key_fallbacks)
        except (
            RescueInterrupted,
            PreventiveQuotaStop,
            DailyQuotaExhausted,
            PaidCallCapReached,
            PolicyFailure,
        ):
            raise
        except BaseException as exc:
            raise RuntimeError(_safe_error(exc, self._secret_values())) from exc
        urls, snippets = extract_grounding_metadata(response)
        query_count, grounded = extract_grounding_usage(response)
        return GroundedAnswer(
            text=str(getattr(response, "text", "") or ""),
            grounding_urls=tuple(urls),
            grounding_snippets=tuple(snippets),
            model=actual_model,
            provider=provider,
            fallbacks=tuple(fallbacks),
            google_search_query_count=query_count,
            grounded=grounded,
        )

    def _secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for name in ("GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2", "GEMINI_API_KEY")
            if isinstance((value := self._credentials.get(name)), str) and value
        )


def _iter_candidates(response: Any) -> list[Any]:
    candidates = response.get("candidates") if isinstance(response, Mapping) else getattr(response, "candidates", None)
    return list(candidates) if candidates else []


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def extract_grounding_metadata(response: Any) -> tuple[list[str], list[str]]:
    """Extract web chunk URLs and support snippets from SDK or dict responses."""
    urls: list[str] = []
    snippets: list[str] = []
    for candidate in _iter_candidates(response):
        metadata = _get(candidate, "grounding_metadata") or {}
        for chunk in _get(metadata, "grounding_chunks", ()) or ():
            web = _get(chunk, "web") or {}
            uri = _get(web, "uri", "")
            title = _get(web, "title", "")
            if isinstance(uri, str) and uri:
                urls.append(uri)
                if title:
                    snippets.append(str(title))
        for support in _get(metadata, "grounding_supports", ()) or ():
            segment = _get(support, "segment") or {}
            text = _get(segment, "text", "")
            if text:
                snippets.append(str(text))
        entry = _get(metadata, "search_entry_point") or {}
        rendered = _get(entry, "rendered_content", "")
        if rendered:
            snippets.append(str(rendered))
    return list(dict.fromkeys(urls)), list(dict.fromkeys(snippets))


def extract_grounding_usage(response: Any) -> tuple[int | None, bool]:
    """Return only real API search-query metadata; never infer a count."""
    counts: list[int] = []
    grounded = False
    metadata_seen = False
    for candidate in _iter_candidates(response):
        metadata = _get(candidate, "grounding_metadata")
        if not metadata:
            continue
        metadata_seen = True
        chunks = _get(metadata, "grounding_chunks", ()) or ()
        supports = _get(metadata, "grounding_supports", ()) or ()
        grounded = grounded or bool(chunks or supports)
        queries = _get(metadata, "web_search_queries", None)
        if queries is None:
            queries = _get(metadata, "google_search_queries", None)
        if isinstance(queries, Sequence) and not isinstance(queries, (str, bytes)):
            counts.append(len(queries))
    return (sum(counts) if counts else None), bool(metadata_seen and grounded)


_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]*\]\((https?://.*?)\)", re.IGNORECASE)
_FORMAT_QUOTES = "\"'“”‘’«»"


def _pre_normalize_url(raw: str) -> str:
    """Remove observed markdown/Unicode corruption before any URL parsing."""
    value = html.unescape(str(raw or ""))
    match = _MARKDOWN_LINK_PATTERN.fullmatch(value.strip())
    if match:
        value = match.group(1)
    value = re.sub(r"%60", "`", value, flags=re.IGNORECASE)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cf")
    value = value.strip().replace("`", "")
    value = value.replace("**", "").replace("__", "")
    previous = None
    while value != previous:
        previous = value
        value = value.strip().strip(_FORMAT_QUOTES).rstrip(".,;:!?").strip(_FORMAT_QUOTES)
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1].rstrip(".,;:!?")
    previous = None
    while value != previous:
        previous = value
        value = value.strip().strip(_FORMAT_QUOTES).rstrip(".,;:!?").strip(_FORMAT_QUOTES)
    return value


def _clean_url(raw: str) -> str:
    value = _pre_normalize_url(raw)
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    if host in GOOGLE_QUERY_REDIRECT_HOSTS and parsed.path == "/url":
        destination = (parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url") or [""])[0]
        if destination and destination != value:
            return _clean_url(destination)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def extract_answer_url_sources(answer: GroundedAnswer) -> list[tuple[str, str]]:
    """Extract candidate URLs while preserving their first model provenance."""
    found = [(item, "grounding") for item in answer.grounding_urls]
    found.extend((item, "texto_modelo") for item in _MARKDOWN_LINK_PATTERN.findall(answer.text))
    found.extend((item, "texto_modelo") for item in URL_PATTERN.findall(answer.text))
    extracted: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw, source in found:
        cleaned = _clean_url(raw)
        key = (cleaned.casefold(), source)
        if cleaned and key not in seen:
            seen.add(key)
            extracted.append((cleaned, source))
    return extracted


def extract_answer_urls(answer: GroundedAnswer) -> list[str]:
    """Backward-compatible URL-only view of answer candidates."""
    return list(dict.fromkeys(url for url, _source in extract_answer_url_sources(answer)))


_MUNICIPALITY_CONTEXT_CACHE: dict[str, tuple[str, str]] | None = None
_MUNICIPALITY_SITE_CACHE: dict[str, str] | None = None


def _municipality_context() -> dict[str, tuple[str, str]]:
    global _MUNICIPALITY_CONTEXT_CACHE
    if _MUNICIPALITY_CONTEXT_CACHE is not None:
        return _MUNICIPALITY_CONTEXT_CACHE
    path = Path(__file__).resolve().parents[4] / "data/fase2/municipios_rs_local.csv"
    context: dict[str, tuple[str, str]] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                natural = (row.get("municipio") or "").strip()
                site = _clean_url((row.get("site_base") or "").strip())
                slug = re.sub(r"[^a-z0-9]", "", _norm(natural))
                if slug and natural:
                    context[slug] = (natural, _host(site))
    except OSError:
        pass
    _MUNICIPALITY_CONTEXT_CACHE = context
    return context


def municipality_natural_name(slug_or_name: str) -> tuple[str, str]:
    slug = re.sub(r"[^a-z0-9]", "", _norm(slug_or_name))
    natural, domain = _municipality_context().get(slug, (slug_or_name.strip(), ""))
    words = natural.split()
    for index in range(1, len(words)):
        if words[index].casefold() in {"da", "das", "de", "do", "dos", "e"}:
            words[index] = words[index].casefold()
    return " ".join(words), domain


def _url_map_key(municipio: str, bucket: str) -> tuple[str, str]:
    """Reuse the runner's slug identity for both sides of a URL-map match."""

    return tuple(
        re.sub(r"[^a-z0-9]", "", _norm(value))
        for value in (municipio, bucket)
    )


def _municipality_site_base(slug_or_name: str) -> str:
    """Resolve the CSV site_base through the runner's natural-name identity."""

    global _MUNICIPALITY_SITE_CACHE
    natural_name, _ = municipality_natural_name(slug_or_name)
    wanted = re.sub(r"[^a-z0-9]", "", _norm(natural_name))
    if _MUNICIPALITY_SITE_CACHE is None:
        path = Path(__file__).resolve().parents[4] / "data/fase2/municipios_rs_local.csv"
        sites: dict[str, str] = {}
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("municipio") or "").strip()
                    slug = re.sub(r"[^a-z0-9]", "", _norm(name))
                    site = _clean_url((row.get("site_base") or "").strip())
                    if slug and site:
                        sites[slug] = site
        except OSError:
            pass
        _MUNICIPALITY_SITE_CACHE = sites
    return _MUNICIPALITY_SITE_CACHE.get(wanted, "")


def build_queries(target: Target) -> list[str]:
    natural_name, official_domain = municipality_natural_name(target.municipio)
    kind = (
        '"concurso público" cargos efetivos edital'
        if target.bucket == "concurso_publico"
        else '"processo seletivo simplificado" contratação temporária edital'
    )
    exclusions = (
        "excluir processo seletivo e contratação temporária"
        if target.bucket == "concurso_publico"
        else "excluir concurso público para cargos efetivos"
    )
    domain = f" site:{official_domain}" if official_domain else " site oficial"
    hint = f"Pista original: {target.pista}"
    base = f'prefeitura "{natural_name}" RS {kind} 2026{domain}; {exclusions}'
    if target.sub_causa == "render_incierto":
        return [
            f'{base}; superfície oficial estática alternativa com itens. {hint}',
            f'{base}; endpoint XHR AJAX da listagem oficial. {hint}',
            f'{base}; URL final da listagem oficial após carregamento. {hint}',
            f'{base}; documento ou índice oficial ligado pela página. {hint}',
            f'{base}; parâmetros reproduzíveis de filtro e paginação da listagem. {hint}',
        ]
    if target.sub_causa == "dificil_rederivado":
        return [
            f'{base}; onde publica a listagem oficial. {hint}',
            f'{base}; índice oficial e histórico. {hint}',
            f'{base}; página de publicações da prefeitura. {hint}',
            f'{base}; todos os anos e arquivos anteriores. {hint}',
            f'{base}; menu transparência editais e seleções. {hint}',
        ]
    return [
        f"{base}; índice ou listagem oficial. {hint}",
        f'{base}; publicações e editais vigentes. {hint}',
        f'{base}; histórico de editais todos os anos. {hint}',
        f'{base}; portal oficial delegado pela prefeitura. {hint}',
        f'{base}; menu transparência concursos e seleções. {hint}',
    ]


def _host(url: str) -> str:
    try:
        return (urlsplit(_pre_normalize_url(url)).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _is_google_grounding_redirect(url: str) -> bool:
    try:
        parsed = urlsplit(_pre_normalize_url(url))
    except ValueError:
        return False
    return (
        (parsed.hostname or "").casefold().rstrip(".") in GOOGLE_GROUNDING_REDIRECT_HOSTS
        and parsed.path.startswith("/grounding-api-redirect/")
    )


def _default_redirect_host_resolver(host: str) -> tuple[str, ...]:
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    }
    return tuple(sorted(addresses))


def _address_is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _redirect_hop_is_safe(
    url: str,
    host_resolver: Callable[[str], Sequence[str]] = _default_redirect_host_resolver,
) -> bool:
    """Fail closed for schemes and explicit local/private redirect targets."""
    try:
        parsed = urlsplit(_pre_normalize_url(url))
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or not host:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if host in _BLOCKED_REDIRECT_HOSTS or host.endswith(".localhost"):
        return False
    if host.endswith(".metadata.google.internal"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = tuple(host_resolver(host))
        except (OSError, ValueError):
            return False
        return bool(resolved) and all(_address_is_public(item) for item in resolved)
    return _address_is_public(str(address))


def _new_redirect_session() -> Any:
    """Create an isolated session that cannot inherit model credentials."""
    import requests

    session = requests.Session()
    session.headers.clear()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; GroundingRedirectResolver/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    session.auth = None
    session.cookies.clear()
    session.trust_env = False
    return session


def _resolve_grounding_redirect(
    url: str,
    *,
    timeout: int,
    cache: dict[str, str | None],
    session_factory: Callable[[], Any] = _new_redirect_session,
    host_resolver: Callable[[str], Sequence[str]] = _default_redirect_host_resolver,
) -> str | None:
    """Resolve a grounding redirect hop-by-hop under a strict SSRF policy."""
    key = url.casefold()
    if key not in cache:
        session: Any | None = None
        try:
            current = _clean_url(url)
            if not _is_google_grounding_redirect(current) or not _redirect_hop_is_safe(
                current, host_resolver
            ):
                cache[key] = None
                return cache[key]
            session = session_factory()
            for redirects_followed in range(MAX_GROUNDING_REDIRECTS):
                if not _redirect_hop_is_safe(current, host_resolver):
                    cache[key] = None
                    break
                response = session.get(
                    current,
                    timeout=min(timeout, REDIRECT_RESOLUTION_TIMEOUT),
                    allow_redirects=False,
                )
                try:
                    location = response.headers.get("Location", "")
                    is_redirect = 300 <= int(response.status_code) < 400 and bool(location)
                finally:
                    close_response = getattr(response, "close", None)
                    if callable(close_response):
                        close_response()
                if not is_redirect:
                    cache[key] = current if not _is_google_grounding_redirect(current) else None
                    break
                if redirects_followed + 1 >= MAX_GROUNDING_REDIRECTS:
                    cache[key] = None
                    break
                next_url = _clean_url(urljoin(current, location))
                if not next_url or not _redirect_hop_is_safe(next_url, host_resolver):
                    cache[key] = None
                    break
                current = next_url
        except RescueInterrupted:
            raise
        except BaseException:
            cache[key] = None
        finally:
            close_session = getattr(session, "close", None)
            if callable(close_session):
                close_session()
    return cache[key]


def official_host_check(municipio: str, url: str) -> tuple[bool, str]:
    host = _host(url)
    if not host:
        return False, "host_invalido"
    if authority.registry_official_host(municipio, host):
        return True, "registro_oficial"
    if authority.universe_site_base_match(municipio, url):
        return True, "universo_site_base"
    if authority.delegated_platform_provenance(municipio, url):
        return True, "plataforma_delegada"
    if host.endswith(".rs.gov.br") or host == "rs.gov.br":
        return True, "dominio_municipal_rs_gov_br"
    return False, "host_no_oficial"


def read_targets(path: Path) -> list[Target]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    targets: list[Target] = []
    for number, row in enumerate(rows, start=2):
        municipio = (row.get("municipio") or "").strip()
        bucket = (row.get("bucket") or "").strip()
        sub_causa = (row.get("sub_causa") or "").strip()
        pista = (row.get("pista") or "").strip()
        if (
            not municipio
            or bucket not in {"concurso_publico", "processo_seletivo"}
            or sub_causa not in SUB_CAUSAS
            or not pista
        ):
            raise ValueError(f"target_invalido:linea={number}")
        targets.append(Target(municipio, bucket, pista, sub_causa))
    return targets


def read_url_map(path: Path) -> dict[tuple[str, str], str]:
    """Load staging URL dispatch hints keyed by normalized municipio/bucket."""

    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"municipio", "bucket", "url"}.issubset(reader.fieldnames or ()):
            raise ValueError("url_map_columnas_invalidas")
        result: dict[tuple[str, str], str] = {}
        for number, row in enumerate(reader, start=2):
            municipio = (row.get("municipio") or "").strip()
            bucket = (row.get("bucket") or "").strip()
            raw_url = (row.get("url") or "").strip()
            key = _url_map_key(municipio, bucket)
            url = _clean_url(raw_url)
            if not all(key) or not url:
                raise ValueError(f"url_map_invalido:linea={number}")
            if key in result:
                raise ValueError(f"url_map_clave_duplicada:linea={number}")
            result[key] = url
    return result


def load_grounded_credentials(path: Path, *, free_only: bool) -> dict[str, str]:
    """Load only authorized names; paid is not required or returned in free-only."""
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("gemini_credential_file_unreadable") from exc
    allowed = {"GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2"}
    if not free_only:
        allowed.add("GEMINI_API_KEY")
    loaded: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name, value = name.strip(), value.strip()
        if name not in allowed:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            loaded[name] = value
    required = {"GEMINI_API_KEY_FREE"} if free_only else {
        "GEMINI_API_KEY_FREE", "GEMINI_API_KEY"
    }
    missing = sorted(required.difference(loaded))
    if missing:
        raise ValueError("gemini_authorized_credentials_missing:" + ",".join(missing))
    return loaded


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unit_path(output_dir: Path, target: Target) -> Path:
    safe_municipio = re.sub(r"[^a-zA-Z0-9_-]+", "_", target.municipio).strip("_")
    safe_bucket = re.sub(r"[^a-zA-Z0-9_-]+", "_", target.bucket).strip("_")
    return Path(output_dir) / f"unidad_{safe_municipio}_{safe_bucket}.json"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace a JSON artifact, never exposing a partial final file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup_tmp_files(output_dir: Path) -> None:
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return
    for path in output_dir.glob("*.tmp"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _provider_snapshot(client: GroundedClient) -> dict[str, Any]:
    telemetry = dict(getattr(client, "telemetry", {}) or {})
    providers = telemetry.get("providers", {}) or {}
    normalized: dict[str, dict[str, int]] = {}
    for provider in PROVIDERS:
        raw = providers.get(provider, {}) or {}
        normalized[provider] = {
            field: int(raw.get(field, 0) or 0)
            for field in ("calls", "errors", "responses", "tokens", "quota_rate")
        }
    snapshot = {
        "providers": normalized,
        "fallback_events": list(telemetry.get("fallback_events", ()) or ()),
        "capacidad_vetada": list(telemetry.get("capacidad_vetada", ()) or ()),
    }
    snapshot.update({name: int(telemetry.get(name, 0) or 0) for name in TELEMETRY_COUNTERS})
    # Legacy/injected clients still get honest provider-derived request totals.
    if "model_requests" not in telemetry:
        snapshot["model_requests"] = sum(item["calls"] for item in normalized.values())
    if "successful_model_responses" not in telemetry:
        snapshot["successful_model_responses"] = sum(item["responses"] for item in normalized.values())
    snapshot["paid_calls"] = normalized["gemini_paid"]["calls"]
    return snapshot


def _unit_telemetry(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fallbacks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    providers: dict[str, dict[str, int]] = {}
    for provider in PROVIDERS:
        providers[provider] = {}
        for field in ("calls", "errors", "responses", "tokens", "quota_rate"):
            start = int(before.get("providers", {}).get(provider, {}).get(field, 0))
            end = int(after.get("providers", {}).get(provider, {}).get(field, 0))
            providers[provider][field] = max(0, end - start)
    clean_fallbacks = [dict(event) for event in fallbacks]
    before_events = list(before.get("fallback_events", ()) or ())
    after_events = list(after.get("fallback_events", ()) or ())
    for event in after_events[len(before_events):]:
        normalized = dict(event)
        if normalized not in clean_fallbacks:
            clean_fallbacks.append(normalized)
    before_vetoes = {tuple(item) for item in before.get("capacidad_vetada", ()) or ()}
    new_vetoes = [
        list(item)
        for item in after.get("capacidad_vetada", ()) or ()
        if tuple(item) not in before_vetoes
    ]
    counters = {
        name: max(0, int(after.get(name, 0)) - int(before.get(name, 0)))
        for name in TELEMETRY_COUNTERS
    }
    return {
        "providers": providers,
        "fallbacks": clean_fallbacks,
        "capacidad_vetada": new_vetoes,
        **counters,
        "calls_by_provider": {name: providers[name]["calls"] for name in PROVIDERS},
        "responses_by_provider": {name: providers[name]["responses"] for name in PROVIDERS},
        "errors_by_provider": {name: providers[name]["errors"] for name in PROVIDERS},
    }


def _candidate_from_dict(value: Mapping[str, Any]) -> CandidateRow:
    return CandidateRow(**{
        name: value.get(name, "") if name in {"fuente", "redirector_original"} else value[name]
        for name in OUTPUT_COLUMNS
    })


def _read_unit_files(output_dir: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in sorted(Path(output_dir).glob("unidad_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == UNIT_SCHEMA_VERSION
            and payload.get("estado") in {
                "completed", "failed", "skipped", "DETENIDA_CUOTA_DIARIA_FREE2",
                "DETENIDA_FRENO_CUOTA", "FALLO_DE_POLITICA",
            }
        ):
            payloads.append(payload)
    return payloads


def _paid_calls_in_payloads(payloads: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        int(
            (payload.get("telemetria", {}) or {})
            .get("providers", {})
            .get("gemini_paid", {})
            .get("calls", 0)
            or 0
        )
        for payload in payloads
    )


def _snippet(answer: GroundedAnswer) -> str:
    source = " | ".join(answer.grounding_snippets) or answer.text
    return re.sub(r"\s+", " ", source).strip()[:SNIPPET_LIMIT]


def _bucket_matches(quote: str, bucket: str) -> bool:
    folded = _norm(quote)
    if bucket == "concurso_publico":
        return bool(re.search(r"\bconcurso(?:s)?\s+public", folded))
    return bool(re.search(r"\bprocesso\s+seletivo|\bprocesso\s+simplificado|\bselecao\s+publica", folded))


def _candidate_item_positive_quotes(text: str, bucket: str) -> list[str]:
    """Return literal, bucket-specific excerpts accepted by the certifier pattern."""
    quotes: list[str] = []
    for chunk in re.split(r"\r?\n+|(?<=[.!?])\s+", text or ""):
        literal = re.sub(r"\s+", " ", chunk).strip()
        if not literal:
            continue
        literal = literal[:500]
        if _bucket_matches(literal, bucket) and certifier._is_item_positive_quote(literal):
            quotes.append(literal)
    return list(dict.fromkeys(quotes))[:10]


def _status_stable(status: Any) -> bool:
    return isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 400


def _snapshot_identity_matches(municipio: str, text: str) -> bool:
    target = re.sub(r"[^a-z0-9]", "", _norm(municipio))
    snapshot = re.sub(r"[^a-z0-9]", "", _norm(text))
    return bool(target) and target in snapshot


@dataclass(frozen=True)
class _Multi24OfficialPage:
    site_base: str
    snapshot: f3_multi24_adapter.Multi24Snapshot | None
    href_targets: tuple[str, ...]
    status_code: int | None
    error: str
    source_url: str = ""
    navigation_chain: tuple[str, ...] = ()
    candidate_subpages: tuple[str, ...] = ()
    reviewed_subpages: tuple[str, ...] = ()
    navigation_attempts: tuple[tuple[str, ...], ...] = ()
    navigation_errors: tuple[tuple[str, str], ...] = ()


def _http_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return ""
    default = 443 if scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default else ""
    return f"{scheme}://{host}{suffix}"


def _http_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.username or parsed.password:
        return ""
    return (parsed.hostname or "").casefold()


def _multi24_official_links(
    page_url: str,
    html: str,
    *,
    official_host: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return document hrefs and depth-1, same-host navigation candidates.

    Candidates are content-neutral Portuguese municipal navigation concepts.
    DOM order is retained deliberately: this is a bounded traversal, not a
    numeric scorer or a municipality/provider-specific routing rule.
    """

    hrefs: list[str] = []
    candidates: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.casefold().startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        target = _clean_url(urljoin(page_url, href))
        if not target or not _http_origin(target):
            continue
        hrefs.append(target)
        if _http_host(target) != official_host:
            continue
        # Fragment-only and menu links back to the current document are not
        # depth-1 subpages and would only repeat the homepage fetch.
        if target.casefold() == page_url.casefold():
            continue
        parsed = urlsplit(target)
        hint_source = " ".join((
            anchor.get_text(" ", strip=True),
            parsed.path,
            parsed.query,
        ))
        hint = re.sub(r"[^a-z0-9]+", " ", _norm(hint_source)).strip()
        if MULTI24_OFFICIAL_NAV_HINT_RE.search(hint):
            candidates.append(target)
    return tuple(dict.fromkeys(hrefs)), tuple(dict.fromkeys(candidates))


def _multi24_snapshot(
    requested_url: str,
    fetched: Any,
    *,
    retrieved_at: str,
) -> f3_multi24_adapter.Multi24Snapshot:
    final_url = _clean_url(str(fetched.final_url or requested_url)) or requested_url
    return f3_multi24_adapter.Multi24Snapshot(
        requested_url=requested_url,
        final_url=final_url,
        status_code=int(fetched.status_code),
        body=str(fetched.html).encode("utf-8"),
        content_type="text/html; charset=utf-8",
        retrieved_at=retrieved_at,
    )


def _official_multi24_page(
    municipio: str,
    *,
    portal_url: str,
    fetcher: Fetcher,
    fetch_timeout: int,
    retrieved_at: str,
    cache: dict[str, _Multi24OfficialPage],
) -> tuple[_Multi24OfficialPage, bool]:
    natural_name, _ = municipality_natural_name(municipio)
    key = re.sub(r"[^a-z0-9]", "", _norm(natural_name))
    cached = cache.get(key)
    if cached is not None:
        return cached, True

    site_base = _municipality_site_base(natural_name)
    if not site_base:
        page = _Multi24OfficialPage("", None, (), None, "site_base_missing")
        cache[key] = page
        return page, False
    try:
        fetched = fetcher.get(site_base, min(fetch_timeout, MULTI24_FETCH_TIMEOUT))
        effective_url = _clean_url(str(fetched.final_url or site_base)) or site_base
        # The frozen adapter only accepts HTTPS municipal origins.  Represent
        # the actual post-redirect document as the requested proof snapshot.
        snapshot = _multi24_snapshot(effective_url, fetched, retrieved_at=retrieved_at)
        official_host = _http_host(effective_url)
        portal_host = _http_host(portal_url)
        hrefs, candidates = _multi24_official_links(
            effective_url,
            str(fetched.html),
            official_host=official_host,
        )
        matching = tuple(href for href in hrefs if _http_host(href) == portal_host)
        source_snapshot = snapshot
        source_url = effective_url
        root_chain = [site_base]
        canonical_site_base = _clean_url(site_base) or site_base
        if effective_url.casefold() != canonical_site_base.casefold():
            root_chain.append(effective_url)
        navigation_chain: tuple[str, ...] = (
            tuple((*root_chain, matching[0])) if matching else ()
        )
        reviewed: list[str] = []
        attempts: list[tuple[str, ...]] = []
        navigation_errors: list[tuple[str, str]] = []

        # Depth is exactly one.  A redirect away from the official hostname is
        # never inspected or accepted as authority; the Fetcher interface may
        # follow it at transport level, so its final URL is also checked here.
        if not matching and official_host and portal_host:
            for subpage_url in candidates[:MULTI24_OFFICIAL_SUBPAGE_LIMIT]:
                reviewed.append(subpage_url)
                try:
                    subpage_fetch = fetcher.get(
                        subpage_url,
                        min(fetch_timeout, MULTI24_FETCH_TIMEOUT),
                    )
                    subpage_effective = (
                        _clean_url(str(subpage_fetch.final_url or subpage_url)) or subpage_url
                    )
                    attempt = [*root_chain, subpage_url]
                    if subpage_effective.casefold() != subpage_url.casefold():
                        attempt.append(subpage_effective)
                    if _http_host(subpage_effective) != official_host:
                        attempts.append(tuple(attempt))
                        navigation_errors.append((subpage_url, "redirected_outside_official_host"))
                        continue
                    subpage_hrefs, _ = _multi24_official_links(
                        subpage_effective,
                        str(subpage_fetch.html),
                        official_host=official_host,
                    )
                    subpage_matching = tuple(
                        href for href in subpage_hrefs if _http_host(href) == portal_host
                    )
                    if subpage_matching:
                        attempt.append(subpage_matching[0])
                        attempts.append(tuple(attempt))
                        source_snapshot = _multi24_snapshot(
                            subpage_effective,
                            subpage_fetch,
                            retrieved_at=retrieved_at,
                        )
                        source_url = subpage_effective
                        hrefs = subpage_hrefs
                        navigation_chain = tuple(attempt)
                        matching = subpage_matching
                        break
                    attempts.append(tuple(attempt))
                except RescueInterrupted:
                    raise
                except Exception as exc:
                    attempts.append(tuple((*root_chain, subpage_url)))
                    navigation_errors.append((subpage_url, type(exc).__name__))
        page = _Multi24OfficialPage(
            site_base=site_base,
            snapshot=source_snapshot,
            href_targets=tuple(hrefs),
            status_code=int(fetched.status_code),
            error="",
            source_url=source_url,
            navigation_chain=navigation_chain,
            candidate_subpages=candidates,
            reviewed_subpages=tuple(reviewed),
            navigation_attempts=tuple(attempts),
            navigation_errors=tuple(navigation_errors),
        )
    except RescueInterrupted:
        raise
    except Exception as exc:
        page = _Multi24OfficialPage(
            site_base=site_base,
            snapshot=None,
            href_targets=(),
            status_code=None,
            error=f"official_fetch_error:{type(exc).__name__}",
        )
    cache[key] = page
    return page, False


def _acquire_multi24_dispatch_context(
    *,
    target: Target,
    portal_url: str,
    portal_fetch: Any,
    fetcher: Fetcher,
    fetch_timeout: int,
    retrieved_at: str,
    current_year: int,
    base_context: Mapping[str, Any],
    authority_cache: dict[str, _Multi24OfficialPage],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Acquire only official navigation proof and adapter-observed children."""

    context = dict(base_context)
    natural_name, _ = municipality_natural_name(target.municipio)
    entry = _multi24_snapshot(portal_url, portal_fetch, retrieved_at=retrieved_at)
    context["multi24_entry_snapshot"] = entry
    provenance: dict[str, Any] = {
        "attempted": True,
        "municipio_natural": natural_name,
        "portal_url": portal_url,
        "official_site_base": "",
        "official_cache_hit": False,
        "official_status_code": None,
        "official_href": "",
        "official_source_url": "",
        "official_navigation_chain": [],
        "official_subpage_limit": MULTI24_OFFICIAL_SUBPAGE_LIMIT,
        "official_subpage_candidates": [],
        "official_subpages_reviewed": [],
        "official_navigation_attempts": [],
        "official_navigation_errors": [],
        "linked_pages_limit": MULTI24_CHILD_LIMIT,
        "linked_pages_fetched": [],
        "linked_page_errors": [],
        "result": "",
    }

    official, cache_hit = _official_multi24_page(
        target.municipio,
        portal_url=portal_url,
        fetcher=fetcher,
        fetch_timeout=fetch_timeout,
        retrieved_at=retrieved_at,
        cache=authority_cache,
    )
    provenance.update({
        "official_site_base": official.site_base,
        "official_cache_hit": cache_hit,
        "official_status_code": official.status_code,
        "official_source_url": official.source_url,
        "official_navigation_chain": list(official.navigation_chain),
        "official_subpage_candidates": list(official.candidate_subpages),
        "official_subpages_reviewed": list(official.reviewed_subpages),
        "official_navigation_attempts": [list(item) for item in official.navigation_attempts],
        "official_navigation_errors": [
            {"url": url, "error": error}
            for url, error in official.navigation_errors
        ],
    })
    if official.snapshot is None:
        provenance["result"] = official.error or "official_snapshot_missing"
        return context, provenance, natural_name

    portal_host = _http_host(portal_url)
    matching_hrefs = tuple(
        href for href in official.href_targets
        if _http_host(href) == portal_host
    )
    if not matching_hrefs:
        provenance["result"] = "official_portal_href_missing"
        return context, provenance, natural_name
    official_origin = _http_origin(official.snapshot.final_url)
    if not official_origin.startswith("https://"):
        provenance["result"] = "official_source_origin_not_https"
        return context, provenance, natural_name

    provenance["official_href"] = matching_hrefs[0]
    authority_proof = f3_multi24_adapter.Multi24Authority(
        official_source_origins=(official_origin,),
        navigation_snapshots=(official.snapshot,),
    )
    context["multi24_authority"] = authority_proof
    context["multi24_linked_pages"] = {}
    try:
        observed = f3_multi24_adapter.analyze_multi24(
            entry=entry,
            linked_pages={},
            authority=authority_proof,
            municipio=natural_name,
            bucket=target.bucket,
            current_year=current_year,
        )
    except f3_multi24_adapter.Multi24ContractError as exc:
        provenance["result"] = f"adapter_contract_error:{exc}"
        return context, provenance, natural_name

    eligible_edges = [
        edge for edge in observed.edges
        if f3_multi24_adapter._year_in_text(
            f3_multi24_adapter._norm(edge.label), current_year
        )
        and f3_multi24_adapter._classify_path(edge.provenance) == target.bucket
    ]
    linked_pages: dict[str, f3_multi24_adapter.Multi24Snapshot] = {}
    seen: set[str] = set()
    attempted_children = 0
    for edge in eligible_edges:
        canonical = edge.target_url.casefold()
        if canonical in seen:
            continue
        if attempted_children >= MULTI24_CHILD_LIMIT:
            break
        seen.add(canonical)
        attempted_children += 1
        try:
            child_fetch = fetcher.get(
                edge.target_url,
                min(fetch_timeout, MULTI24_FETCH_TIMEOUT),
            )
            linked_pages[edge.target_url] = _multi24_snapshot(
                edge.target_url,
                child_fetch,
                retrieved_at=retrieved_at,
            )
            provenance["linked_pages_fetched"].append(edge.target_url)
        except RescueInterrupted:
            raise
        except Exception as exc:
            provenance["linked_page_errors"].append({
                "url": edge.target_url,
                "error": type(exc).__name__,
            })
    context["multi24_linked_pages"] = linked_pages
    provenance["result"] = "context_acquired"
    return context, provenance, natural_name


def micro_acquire_unit(
    target: Target,
    url: str,
    *,
    output_dir: Path,
    fetcher: Fetcher,
    timestamp_run: str,
    fetch_timeout: int = 30,
    renderer: Callable[[str], Any] = render_page_networkidle,
    selection_reason: str = "url_candidata_unica_evaluada",
    prior_redirector: str = "",
    adapter_dispatcher: Callable[..., Mapping[str, Any]] = dispatch_f3_adapter,
    adapter_context: Mapping[str, Any] | None = None,
    multi24_authority_cache: dict[str, _Multi24OfficialPage] | None = None,
    dispatch_only: bool = False,
) -> dict[str, Any]:
    """Perform exactly one controlled fetch+render acquisition and persist it."""
    initial_url = url
    final_url = url
    trigger = "fetch+render_page_networkidle"
    snapshot_text = ""
    status: Any = None
    render_obtained = False
    adapter_result: dict[str, Any] = {}
    redirect_chain = [item for item in (prior_redirector, initial_url) if item]
    error = ""
    if not initial_url:
        trigger = "sin_url_candidata_grounded"
    else:
        try:
            initial_timeout = (
                min(fetch_timeout, MULTI24_FETCH_TIMEOUT)
                if detect_platform(initial_url, "") == "multi24"
                else fetch_timeout
            )
            fetched = fetcher.get(initial_url, initial_timeout)
            final_url = _clean_url(fetched.final_url or initial_url) or initial_url
            if final_url and final_url != redirect_chain[-1]:
                redirect_chain.append(final_url)
            dispatch_context = dict(adapter_context or {})
            dispatch_context.setdefault("status_code", fetched.status_code)
            dispatch_municipio = target.municipio
            acquisition_provenance: dict[str, Any] = {}
            current_year = datetime.now().year
            detected_platform = detect_platform(final_url, fetched.html)
            if detected_platform == "multi24":
                try:
                    dispatch_context, acquisition_provenance, dispatch_municipio = (
                        _acquire_multi24_dispatch_context(
                            target=target,
                            portal_url=final_url,
                            portal_fetch=fetched,
                            fetcher=fetcher,
                            fetch_timeout=fetch_timeout,
                            retrieved_at=timestamp_run,
                            current_year=current_year,
                            base_context=dispatch_context,
                            authority_cache=(
                                multi24_authority_cache
                                if multi24_authority_cache is not None else {}
                            ),
                        )
                    )
                except RescueInterrupted:
                    raise
                except Exception as exc:
                    dispatch_municipio = municipality_natural_name(target.municipio)[0]
                    acquisition_provenance = {
                        "attempted": True,
                        "municipio_natural": dispatch_municipio,
                        "portal_url": final_url,
                        "official_site_base": _municipality_site_base(target.municipio),
                        "linked_pages_limit": MULTI24_CHILD_LIMIT,
                        "linked_pages_fetched": [],
                        "linked_page_errors": [],
                        "result": f"acquisition_error:{type(exc).__name__}",
                    }
            try:
                adapter_result = dict(adapter_dispatcher(
                    url=final_url,
                    page_html=fetched.html,
                    municipio=dispatch_municipio,
                    bucket=target.bucket,
                    current_year=current_year,
                    context=dispatch_context,
                ) or {})
                if acquisition_provenance:
                    adapter_result["acquisition_provenance"] = acquisition_provenance
            except RescueInterrupted:
                raise
            except BaseException as exc:
                adapter_result = {
                    "platform": detected_platform,
                    "candidates": [],
                    "refusal_reason": f"dispatcher_error:{type(exc).__name__}",
                }
                if acquisition_provenance:
                    adapter_result["acquisition_provenance"] = acquisition_provenance
            if adapter_result.get("candidates"):
                _, snapshot_text = extract_title_and_text(fetched.html)
                status = fetched.status_code
                trigger = f'f3_adapter:{adapter_result.get("adapter", "unknown")}'
            elif dispatch_only:
                _, snapshot_text = extract_title_and_text(fetched.html)
                status = fetched.status_code
                trigger = f'f3_adapter:{adapter_result.get("adapter", "unknown")}:sin_candidatas'
            else:
                rendered = renderer(final_url)
                if rendered is not None:
                    render_obtained = True
                    final_url = _clean_url(getattr(rendered, "final_url", "") or final_url) or final_url
                    if final_url and final_url != redirect_chain[-1]:
                        redirect_chain.append(final_url)
                    snapshot_text = str(getattr(rendered, "text", "") or "")
                    status = getattr(rendered, "status", None)
                else:
                    _, snapshot_text = extract_title_and_text(fetched.html)
                    status = fetched.status_code
                    trigger = "fetch+render_page_networkidle_sin_resultado"
        except RescueInterrupted:
            raise
        except BaseException as exc:
            trigger = "fetch_error"
            error = type(exc).__name__

    quotes = _candidate_item_positive_quotes(snapshot_text, target.bucket) if render_obtained else []
    authority_ok, authority_reason = official_host_check(target.municipio, final_url)
    gate = {
        "autoridad": authority_ok,
        "identidad": _snapshot_identity_matches(target.municipio, snapshot_text),
        "bucket": bool(quotes),
        "estabilidad": render_obtained and _status_stable(status) and bool(snapshot_text.strip()),
        "item_positive": bool(quotes),
        "pasa": False,
        "razon_autoridad": authority_reason,
    }
    gate["pasa"] = all(gate[name] for name in ("autoridad", "identidad", "bucket", "estabilidad", "item_positive"))
    payload = {
        "url_inicial": initial_url,
        "url_final": final_url,
        "cadena_redirects": redirect_chain,
        "razon_seleccion_url": selection_reason,
        "trigger": trigger,
        "snapshot_recortado": snapshot_text[:SNAPSHOT_LIMIT],
        "citas_candidatas": quotes,
        "veredicto_gate": gate,
        "http_status": status,
        "timestamp": timestamp_run,
    }
    if error:
        payload["error_tipo"] = error
    if (
        adapter_result.get("candidates")
        or adapter_result.get("hook")
        or adapter_result.get("platform") == "multi24"
        or (dispatch_only and adapter_result.get("platform") is not None)
    ):
        payload["adaptador"] = adapter_result
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"micro_{target.municipio}_{target.bucket}.json"
    _atomic_write_json(path, payload)
    return payload


def _adapter_candidate_rows(
    target: Target,
    payload: Mapping[str, Any],
    *,
    query: str,
    snippet: str,
    host_reason: str,
    fuente: str,
    redirector_original: str,
) -> list[CandidateRow]:
    dispatch = payload.get("adaptador", {}) or {}
    rows: list[CandidateRow] = []
    for raw in dispatch.get("candidates", ()) or ():
        if raw.get("confirmed") is not False or raw.get("disposition") not in {"propose", "revisar"}:
            continue
        rows.append(CandidateRow(
            municipio=target.municipio,
            bucket=target.bucket,
            url_candidata=str(raw.get("url_candidata") or dispatch.get("source_url") or ""),
            query_usada=query,
            snippet_grounding=snippet,
            host_oficial_check=host_reason,
            item_markers=int(raw.get("item_markers") or 0),
            http_status=f'f3_adapter:{dispatch.get("platform", "unknown")}',
            fuente=f'adapter:{dispatch.get("adapter", "unknown")}',
            redirector_original=redirector_original,
            disposition=str(raw["disposition"]),
            confirmed=False,
            provenance=dict(raw.get("provenance", {}) or {}),
        ))
    return rows


def _select_micro_target(
    target: Target,
    pending: Sequence[Mapping[str, Any]],
    *,
    require_platform: bool = False,
) -> dict[str, Any]:
    """Require an evaluated, unambiguous candidate; never parse target.pista."""

    evaluated = [
        dict(item) for item in pending
        if item.get("url")
        and item.get("host_oficial_check")
        and (not require_platform or item.get("detected_platform") in {
            "multi24", "atende", "datatables"
        })
    ]
    bucket_specific = [
        item for item in evaluated
        if _bucket_matches(f'{item.get("url", "")} {item.get("snippet", "")}', target.bucket)
    ]
    if len(bucket_specific) == 1:
        selected = bucket_specific[0]
        selected["selection_reason"] = "unica_candidata_con_evidencia_del_bucket"
        return selected
    if len(evaluated) == 1:
        selected = evaluated[0]
        selected["selection_reason"] = "unica_candidata_oficial_estable_evaluada"
        return selected
    return {
        "url": "", "query": "", "snippet": "",
        "host_oficial_check": "sin_url_no_ambigua",
        "fuente": "", "redirector_original": "",
        "selection_reason": "sin_candidata_evaluada_no_ambigua",
    }


def _select_detectable_target_url(
    target: Target,
    url_map: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    """Select a URL-map hint only when URL-first platform proof exists.

    The map is deliberately not authority evidence. Normal acquisition and
    adapter authority/delegation checks still run after this dispatch choice.
    """

    mapped_url = _clean_url(html.unescape(
        url_map.get(_url_map_key(target.municipio, target.bucket), "")
    ))
    platform = detect_platform(mapped_url, "") if mapped_url else None
    if platform in {"multi24", "atende", "datatables"}:
        return {
            "url": mapped_url,
            "query": "",
            "snippet": "",
            "host_oficial_check": "urlmap_solo_dispatch_sin_prueba_autoridad",
            "fuente": "url_map",
            "redirector_original": "",
            "selection_reason": f"urlmap_plataforma_detectable:{platform}",
        }
    return {
        "url": "", "query": "", "snippet": "",
        "host_oficial_check": "sin_plataforma_detectable",
        "fuente": "", "redirector_original": "",
        "selection_reason": "sin_plataforma_detectable",
    }


def run_micro_acquisitions(
    targets: Sequence[Target],
    rows: Sequence[CandidateRow],
    summary: dict[str, Any],
    *,
    output_dir: Path,
    fetcher: Fetcher,
    timestamp_run: str,
    fetch_timeout: int = 30,
    renderer: Callable[[str], Any] = render_page_networkidle,
    adapter_context_provider: Callable[[Target, str], Mapping[str, Any]] | None = None,
    adapter_dispatcher: Callable[..., Mapping[str, Any]] = dispatch_f3_adapter,
) -> list[CandidateRow]:
    """Run one controlled acquisition per unresolved render-incierto unit.

    A unit with no grounded URL still receives a durable, fail-closed artifact
    explaining that no acquisition target existed.  It never calls the fetcher
    or renderer and cannot create a candidate.
    """
    result_rows = list(rows)
    multi24_authority_cache: dict[str, _Multi24OfficialPage] = {}
    for target in targets:
        if target.sub_causa != "render_incierto":
            continue
        key = f"{target.municipio}/{target.bucket}"
        unit = summary["unidades"][key]
        pending = unit.get("micro_pendientes", [])
        if unit["candidatas"]:
            continue
        selected = _select_micro_target(target, pending)
        payload = micro_acquire_unit(
            target,
            selected["url"],
            output_dir=output_dir,
            fetcher=fetcher,
            timestamp_run=timestamp_run,
            fetch_timeout=fetch_timeout,
            renderer=renderer,
            selection_reason=selected["selection_reason"],
            prior_redirector=selected.get("redirector_original", ""),
            adapter_dispatcher=adapter_dispatcher,
            adapter_context=(
                adapter_context_provider(target, selected["url"])
                if adapter_context_provider is not None else None
            ),
            multi24_authority_cache=multi24_authority_cache,
        )
        unit["micro_archivo"] = f"micro_{target.municipio}_{target.bucket}.json"
        unit["micro_veredicto"] = payload["veredicto_gate"]
        adapter_rows = _adapter_candidate_rows(
            target,
            payload,
            query=selected["query"],
            snippet=selected["snippet"],
            host_reason=selected["host_oficial_check"],
            fuente=selected["fuente"],
            redirector_original=selected.get("redirector_original", ""),
        )
        if adapter_rows:
            result_rows.extend(adapter_rows)
            unit["candidatas"] = len(adapter_rows)
        elif payload["veredicto_gate"]["pasa"]:
            result_rows.append(CandidateRow(
                municipio=target.municipio,
                bucket=target.bucket,
                url_candidata=payload["url_final"],
                query_usada=selected["query"],
                snippet_grounding=selected["snippet"],
                host_oficial_check=payload["veredicto_gate"]["razon_autoridad"],
                item_markers=len(payload["citas_candidatas"]),
                http_status="micro_acquire",
            ))
            unit["candidatas"] = 1
    summary["global"]["candidatas"] = len(result_rows)
    summary["policy"]["micro_acquire"] = True
    summary["policy"]["timestamp_run"] = timestamp_run
    return result_rows


def run_rescue(
    targets: Sequence[Target],
    *,
    client: GroundedClient,
    fetcher: Fetcher,
    model: str = REQUIRED_MODEL,
    max_searches: int = MAX_POLICY_SEARCHES,
    sleep_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    fetch_timeout: int = 30,
    output_dir: Path | None = None,
    resume: bool = False,
    skip_existing: bool = False,
    micro_acquire: bool = False,
    adapters_only: bool = False,
    url_map: Mapping[tuple[str, str], str] | None = None,
    renderer: Callable[[str], Any] = render_page_networkidle,
    redirect_session_factory: Callable[[], Any] = _new_redirect_session,
    redirect_host_resolver: Callable[[str], Sequence[str]] = _default_redirect_host_resolver,
    interruption: InterruptionState | None = None,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
    free_only: bool = False,
    paid_authorization: str | None = None,
    max_paid_calls: int | None = None,
    adapter_context_provider: Callable[[Target, str], Mapping[str, Any]] | None = None,
    adapter_dispatcher: Callable[..., Mapping[str, Any]] = dispatch_f3_adapter,
) -> tuple[list[CandidateRow], dict[str, Any]]:
    if model != REQUIRED_MODEL:
        raise ValueError(f"model_debe_ser_exactamente_{REQUIRED_MODEL}")
    if not 1 <= max_searches <= MAX_POLICY_SEARCHES:
        raise ValueError("max_searches_debe_estar_entre_1_y_5")
    if adapters_only and url_map is None:
        raise ValueError("adapters_only_requiere_--url-map")
    policy = {
        "grounding_tool": None if adapters_only else "google_search",
        "retrieval": False,
        "map_grounding": False,
        "max_searches_per_unit": 0 if adapters_only else max_searches,
        "confirmation_performed": False,
        "writes_url_map": False,
        "micro_acquire": micro_acquire,
        "adapters_only": adapters_only,
        "free_only": free_only,
        "provider_sequence": (
            "NONE_ADAPTERS_ONLY" if adapters_only else
            ("FREE1->FREE2->STOP" if free_only else "FREE1->FREE2->PAID")
        ),
    }
    if paid_authorization is not None:
        policy["paid_authorization"] = paid_authorization
    if free_only and _provider_snapshot(client)["paid_calls"] != 0:
        raise PolicyFailure("FALLO_DE_POLITICA:paid_calls_before_run")
    adapters_only_model_calls_before = sum(
        item["calls"] for item in _provider_snapshot(client)["providers"].values()
    )
    output_path = Path(output_dir) if output_dir is not None else None
    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)
        _cleanup_tmp_files(output_path)
    should_resume = resume or skip_existing
    paid_calls_previas = (
        _paid_calls_in_payloads(_read_unit_files(output_path))
        if should_resume and output_path is not None else 0
    )
    tope_efectivo = (
        max(0, max_paid_calls - paid_calls_previas)
        if max_paid_calls is not None else None
    )
    paid_calls_at_start = _provider_snapshot(client)["paid_calls"]
    if paid_authorization is not None:
        policy.update({
            "max_paid_calls": max_paid_calls,
            "paid_calls_previas": paid_calls_previas,
            "tope_efectivo": tope_efectivo,
        })
        if hasattr(client, "max_paid_calls"):
            client.max_paid_calls = tope_efectivo
    payloads: list[dict[str, Any]] = []
    skipped_existing = 0
    stop_after_current = False
    redirect_cache: dict[str, str | None] = {}
    multi24_authority_cache: dict[str, _Multi24OfficialPage] = {}
    for target in targets:
        key = f"{target.municipio}/{target.bucket}"
        path = _unit_path(output_path, target) if output_path is not None else None
        if should_resume and path is not None and path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if (
                existing.get("schema_version") == UNIT_SCHEMA_VERSION
                and existing.get("municipio") == target.municipio
                and existing.get("bucket") == target.bucket
                and existing.get("estado") == "completed"
            ):
                skipped_existing += 1
                continue
        before = _provider_snapshot(client)
        unit_candidates: list[CandidateRow] = []
        grounded: dict[str, Any] = {
            "sub_causa": target.sub_causa,
            "busquedas_usadas": 0,
            "candidatas": [],
            "queries": [],
            "modelo_real_usado": [],
            "proveedores_que_respondieron": [],
            "fallbacks": [],
            "errores": [],
            "descartadas": [],
            "micro_pendientes": [],
            "confirmacion": False,
        }
        seen_final: set[str] = set()
        queries = [] if adapters_only else build_queries(target)[:max_searches]
        micro_result: dict[str, Any] | None = None
        estado = "completed"
        causa: str | None = None
        try:
            for index, query in enumerate(queries):
                if interruption is not None:
                    interruption.raise_if_requested()
                grounded["busquedas_usadas"] += 1
                query_result: dict[str, Any] = {"query": query}
                try:
                    answer = client.search(
                        query,
                        model=model,
                        municipio=target.municipio,
                        bucket=target.bucket,
                    )
                    if free_only and _provider_snapshot(client)["paid_calls"] != 0:
                        raise PolicyFailure("FALLO_DE_POLITICA:paid_calls_changed")
                except RescueInterrupted:
                    raise
                except PolicyFailure as exc:
                    error = {"query": query, "type": "FALLO_DE_POLITICA"}
                    grounded["errores"].append(error)
                    query_result["error"] = dict(error)
                    grounded["queries"].append(query_result)
                    estado = "FALLO_DE_POLITICA"
                    causa = str(exc)
                    stop_after_current = True
                    break
                except DailyQuotaExhausted:
                    error = {"query": query, "type": "cuota_diaria_free2_agotada"}
                    grounded["errores"].append(error)
                    query_result["error"] = dict(error)
                    grounded["queries"].append(query_result)
                    estado = "DETENIDA_CUOTA_DIARIA_FREE2"
                    causa = "checkpoint_atomico_cuota_diaria_free2"
                    stop_after_current = True
                    break
                except PreventiveQuotaStop as exc:
                    error = {"query": query, "type": str(exc)}
                    grounded["errores"].append(error)
                    query_result["error"] = dict(error)
                    grounded["queries"].append(query_result)
                    estado = "DETENIDA_FRENO_CUOTA"
                    causa = str(exc)
                    stop_after_current = True
                    break
                except PaidCallCapReached:
                    error = {"query": query, "type": "paid_cap_alcanzado"}
                    grounded["errores"].append(error)
                    query_result["error"] = dict(error)
                    grounded["queries"].append(query_result)
                    estado = "failed"
                    causa = "paid_cap_alcanzado"
                    stop_after_current = True
                    break
                except BaseException as exc:
                    if free_only and _provider_snapshot(client)["paid_calls"] != 0:
                        error = {"query": query, "type": "FALLO_DE_POLITICA"}
                        grounded["errores"].append(error)
                        query_result["error"] = dict(error)
                        grounded["queries"].append(query_result)
                        estado = "FALLO_DE_POLITICA"
                        causa = "FALLO_DE_POLITICA:paid_calls_changed_during_error"
                        stop_after_current = True
                        break
                    error = {"query": query, "type": type(exc).__name__}
                    grounded["errores"].append(error)
                    query_result["error"] = dict(error)
                    grounded["queries"].append(query_result)
                    if index + 1 < len(queries) and sleep_seconds:
                        sleep(sleep_seconds)
                    continue
                query_result.update({
                    "answer_text": answer.text,
                    "grounding_urls": list(answer.grounding_urls),
                    "grounding_snippets": list(answer.grounding_snippets),
                    "model": answer.model,
                    "provider": answer.provider,
                    "fallbacks": [dict(item) for item in answer.fallbacks],
                })
                grounded["queries"].append(query_result)
                if answer.model not in grounded["modelo_real_usado"]:
                    grounded["modelo_real_usado"].append(answer.model)
                if answer.provider and answer.provider not in grounded["proveedores_que_respondieron"]:
                    grounded["proveedores_que_respondieron"].append(answer.provider)
                grounded["fallbacks"].extend(answer.fallbacks)
                snippet = _snippet(answer)
                for url, source in extract_answer_url_sources(answer):
                    source_snippet = snippet if source == "grounding" else ""
                    grounding_redirect = _is_google_grounding_redirect(url)
                    redirector_original = url if grounding_redirect else ""
                    candidate_url = url
                    if grounding_redirect:
                        candidate_url = _resolve_grounding_redirect(
                            url,
                            timeout=fetch_timeout,
                            cache=redirect_cache,
                            session_factory=redirect_session_factory,
                            host_resolver=redirect_host_resolver,
                        ) or ""
                        if not candidate_url:
                            grounded["descartadas"].append({
                                "url": url,
                                "razon": "redirect_no_resuelto",
                                "fuente": source,
                                "redirector_original": url,
                            })
                            continue
                    allowed, reason = official_host_check(target.municipio, candidate_url)
                    if not allowed:
                        grounded["descartadas"].append({
                            "url": candidate_url,
                            "razon": reason,
                            "fuente": source,
                            "redirector_original": redirector_original,
                        })
                        continue
                    normalized = candidate_url.casefold()
                    if normalized in seen_final:
                        continue
                    seen_final.add(normalized)
                    status = ""
                    markers = 0
                    positive_quotes: list[str] = []
                    try:
                        result = fetcher.get(candidate_url, fetch_timeout)
                        status = str(result.status_code)
                        candidate_url = _clean_url(result.final_url or candidate_url) or candidate_url
                        final_allowed, final_reason = official_host_check(target.municipio, candidate_url)
                        if not final_allowed:
                            grounded["descartadas"].append({
                                "url": candidate_url,
                                "razon": final_reason,
                                "fuente": source,
                                "redirector_original": redirector_original,
                            })
                            continue
                        reason = final_reason
                        _, visible_text = extract_title_and_text(result.html)
                        markers = _count_item_markers(_norm(visible_text))
                        positive_quotes = _candidate_item_positive_quotes(visible_text, target.bucket)
                        if not _status_stable(result.status_code):
                            grounded["descartadas"].append({
                                "url": candidate_url,
                                "razon": "http_no_estable",
                                "fuente": source,
                                "redirector_original": redirector_original,
                            })
                            continue
                        if not _snapshot_identity_matches(target.municipio, visible_text):
                            grounded["descartadas"].append({
                                "url": candidate_url,
                                "razon": "identidad_no_demostrada",
                                "fuente": source,
                                "redirector_original": redirector_original,
                            })
                            continue
                        detected_platform = detect_platform(candidate_url, result.html)
                    except RescueInterrupted:
                        raise
                    except BaseException as exc:
                        grounded["errores"].append({
                            "query": query,
                            "type": type(exc).__name__,
                            "stage": "fetch",
                        })
                        status = f"error:{type(exc).__name__}"
                        grounded["descartadas"].append({
                            "url": candidate_url,
                            "razon": "fetch_fallido",
                            "fuente": source,
                            "redirector_original": redirector_original,
                        })
                        continue
                    final_normalized = candidate_url.casefold()
                    if final_normalized != normalized and final_normalized in seen_final:
                        continue
                    seen_final.add(final_normalized)
                    if (
                        target.sub_causa == "url_mala"
                        and detected_platform in {"multi24", "atende", "datatables"}
                    ):
                        grounded["micro_pendientes"].append({
                            "url": candidate_url,
                            "query": query,
                            "snippet": source_snippet,
                            "host_oficial_check": reason,
                            "fuente": source,
                            "redirector_original": redirector_original,
                            "detected_platform": detected_platform,
                        })
                        continue
                    if target.sub_causa == "render_incierto" and not positive_quotes:
                        grounded["micro_pendientes"].append({
                            "url": candidate_url,
                            "query": query,
                            "snippet": source_snippet,
                            "host_oficial_check": reason,
                            "fuente": source,
                            "redirector_original": redirector_original,
                        })
                        continue
                    if not positive_quotes:
                        grounded["descartadas"].append({
                            "url": candidate_url,
                            "razon": "bucket_item_positive_no_demostrado",
                            "fuente": source,
                            "redirector_original": redirector_original,
                        })
                        continue
                    unit_candidates.append(CandidateRow(
                        municipio=target.municipio,
                        bucket=target.bucket,
                        url_candidata=candidate_url,
                        query_usada=query,
                        snippet_grounding=source_snippet,
                        host_oficial_check=reason,
                        item_markers=markers,
                        http_status=status,
                        fuente=source,
                        redirector_original=redirector_original,
                    ))
                if index + 1 < len(queries) and sleep_seconds:
                    sleep(sleep_seconds)
            if adapters_only:
                if output_path is None:
                    raise ValueError("adapters_only_requiere_output_dir")
                selected = _select_detectable_target_url(target, url_map or {})
                if not selected["url"]:
                    estado = "skipped"
                    causa = "sin_plataforma_detectable"
                else:
                    micro_result = micro_acquire_unit(
                        target,
                        selected["url"],
                        output_dir=output_path,
                        fetcher=fetcher,
                        timestamp_run=timestamp_factory(),
                        fetch_timeout=fetch_timeout,
                        renderer=renderer,
                        selection_reason=selected["selection_reason"],
                        adapter_dispatcher=adapter_dispatcher,
                        adapter_context=(
                            adapter_context_provider(target, selected["url"])
                            if adapter_context_provider is not None else None
                        ),
                        multi24_authority_cache=multi24_authority_cache,
                        dispatch_only=True,
                    )
                    unit_candidates.extend(_adapter_candidate_rows(
                        target,
                        micro_result,
                        query="",
                        snippet=selected["snippet"],
                        host_reason=selected["host_oficial_check"],
                        fuente=selected["fuente"],
                        redirector_original="",
                    ))
            if (
                estado == "completed"
                and grounded["queries"]
                and all("error" in item for item in grounded["queries"])
            ):
                estado = "failed"
                causa = "all_grounded_searches_failed"
            if (
                estado == "completed" and micro_acquire
                and target.sub_causa in {"render_incierto", "url_mala"}
                and not unit_candidates
                and not adapters_only
            ):
                pending = grounded["micro_pendientes"]
                selected = _select_micro_target(
                    target,
                    pending,
                    require_platform=(target.sub_causa == "url_mala"),
                )
                if output_path is None:
                    raise ValueError("micro_acquire_requiere_output_dir")
                micro_result = micro_acquire_unit(
                    target,
                    selected["url"],
                    output_dir=output_path,
                    fetcher=fetcher,
                    timestamp_run=timestamp_factory(),
                    fetch_timeout=fetch_timeout,
                    renderer=renderer,
                    selection_reason=selected["selection_reason"],
                    prior_redirector=selected.get("redirector_original", ""),
                    adapter_dispatcher=adapter_dispatcher,
                    adapter_context=(
                        adapter_context_provider(target, selected["url"])
                        if adapter_context_provider is not None else None
                    ),
                    multi24_authority_cache=multi24_authority_cache,
                )
                adapter_rows = _adapter_candidate_rows(
                    target,
                    micro_result,
                    query=selected["query"],
                    snippet=selected["snippet"],
                    host_reason=selected["host_oficial_check"],
                    fuente=selected["fuente"],
                    redirector_original=selected["redirector_original"],
                )
                if adapter_rows:
                    unit_candidates.extend(adapter_rows)
                elif micro_result["veredicto_gate"]["pasa"]:
                    unit_candidates.append(CandidateRow(
                        municipio=target.municipio,
                        bucket=target.bucket,
                        url_candidata=micro_result["url_final"],
                        query_usada=selected["query"],
                        snippet_grounding=selected["snippet"],
                        host_oficial_check=micro_result["veredicto_gate"]["razon_autoridad"],
                        item_markers=len(micro_result["citas_candidatas"]),
                        http_status="micro_acquire",
                        fuente=selected["fuente"],
                        redirector_original=selected["redirector_original"],
                    ))
        except RescueInterrupted:
            estado = "failed"
            causa = "interrupted"
            stop_after_current = True
        except BaseException as exc:
            estado = "failed"
            causa = type(exc).__name__
        grounded["candidatas"] = [row.as_dict() for row in unit_candidates]
        after = _provider_snapshot(client)
        paid_cap_reached_here = (
            tope_efectivo is not None
            and before["paid_calls"] < tope_efectivo <= after["paid_calls"]
        )
        payload = {
            "schema_version": UNIT_SCHEMA_VERSION,
            "municipio": target.municipio,
            "bucket": target.bucket,
            "sub_causa": target.sub_causa,
            "pista": target.pista,
            "grounded": grounded,
            "telemetria": _unit_telemetry(before, after, grounded["fallbacks"]),
            "microadquisicion": micro_result,
            "estado": estado,
            "causa": causa,
            "paid_cap_alcanzado_en_unidad": paid_cap_reached_here,
            "timestamp": timestamp_factory(),
        }
        if path is not None:
            try:
                _atomic_write_json(path, payload)
            except RescueInterrupted:
                payload["estado"] = "failed"
                payload["causa"] = "interrupted"
                payload["timestamp"] = timestamp_factory()
                stop_after_current = True
                _atomic_write_json(path, payload)
        payloads.append(payload)
        if stop_after_current:
            break
    policy["paid_calls_nuevas"] = max(
        0, _provider_snapshot(client)["paid_calls"] - paid_calls_at_start
    )
    if output_path is not None:
        summary = rebuild_summary(output_path, policy=policy, skipped_existing=skipped_existing)
        candidates = _rows_from_unit_payloads(_read_unit_files(output_path))
    else:
        summary = _aggregate_unit_payloads(payloads, policy=policy, skipped_existing=0)
        candidates = _rows_from_unit_payloads(payloads)
    if free_only and summary["global"]["paid_calls"] != 0:
        summary["global"]["estado_corrida"] = "FALLO_DE_POLITICA"
        raise PolicyFailure("FALLO_DE_POLITICA:paid_calls_after_run")
    if adapters_only:
        adapters_only_model_calls_after = sum(
            item["calls"] for item in _provider_snapshot(client)["providers"].values()
        )
        if adapters_only_model_calls_after != adapters_only_model_calls_before:
            raise PolicyFailure("FALLO_DE_POLITICA:adapters_only_model_client_invoked")
    return candidates, summary


def _rows_from_unit_payloads(payloads: Sequence[Mapping[str, Any]]) -> list[CandidateRow]:
    rows: list[CandidateRow] = []
    for payload in payloads:
        for raw in payload.get("grounded", {}).get("candidatas", ()) or ():
            rows.append(_candidate_from_dict(raw))
    return rows


def _aggregate_unit_payloads(
    payloads: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
    skipped_existing: int,
) -> dict[str, Any]:
    providers = {
        provider: {field: 0 for field in ("calls", "errors", "responses", "tokens", "quota_rate")}
        for provider in PROVIDERS
    }
    fallback_counters = {provider: Counter() for provider in (*PROVIDERS, "unknown")}
    units: dict[str, Any] = {}
    candidates = 0
    searches = 0
    capacity_vetoes: list[tuple[str, str, str]] = []
    telemetry_counters = Counter({name: 0 for name in TELEMETRY_COUNTERS})
    for payload in payloads:
        grounded = dict(payload.get("grounded", {}) or {})
        key = f'{payload.get("municipio", "")}/{payload.get("bucket", "")}'
        unit_summary = dict(grounded)
        unit_summary.update({
            "estado": payload.get("estado"),
            "causa": payload.get("causa"),
            "timestamp": payload.get("timestamp"),
            "telemetria": payload.get("telemetria", {}),
            "microadquisicion": payload.get("microadquisicion"),
        })
        if payload.get("microadquisicion"):
            unit_summary["micro_veredicto"] = payload["microadquisicion"].get("veredicto_gate", {})
        units[key] = unit_summary
        candidates += len(grounded.get("candidatas", ()) or ())
        searches += int(grounded.get("busquedas_usadas", 0) or 0)
        telemetry = payload.get("telemetria", {}) or {}
        for name in TELEMETRY_COUNTERS:
            telemetry_counters[name] += int(telemetry.get(name, 0) or 0)
        for provider in PROVIDERS:
            raw = telemetry.get("providers", {}).get(provider, {}) or {}
            for field in providers[provider]:
                providers[provider][field] += int(raw.get(field, 0) or 0)
        for event in telemetry.get("fallbacks", ()) or ():
            provider = str(event.get("from_provider") or "unknown")
            if provider not in fallback_counters:
                provider = "unknown"
            fallback_counters[provider][str(event.get("cause") or "unknown")] += 1
        for item in telemetry.get("capacidad_vetada", ()) or ():
            normalized_veto = tuple(str(part) for part in item)
            if len(normalized_veto) == 3 and normalized_veto not in capacity_vetoes:
                capacity_vetoes.append(normalized_veto)
    calls_by_provider = {name: values["calls"] for name, values in providers.items()}
    errors_by_provider = {name: values["errors"] for name, values in providers.items()}
    responses_by_provider = {name: values["responses"] for name, values in providers.items()}
    tokens_by_provider = {name: values["tokens"] for name, values in providers.items()}
    fallbacks_by_provider = {
        name: dict(counter) for name, counter in fallback_counters.items()
    }
    result = {
        "policy": dict(policy),
        "capacidad_vetada": capacity_vetoes,
        "unidades": units,
        "global": {
            "unidades": len(payloads),
            "completed": sum(payload.get("estado") == "completed" for payload in payloads),
            "failed": sum(payload.get("estado") == "failed" for payload in payloads),
            "skipped_existing": skipped_existing,
            "busquedas_grounded": searches,
            "candidatas": candidates,
            "llamadas": sum(calls_by_provider.values()),
            "errores": sum(errors_by_provider.values()),
            "calls_by_provider": calls_by_provider,
            "responses_by_provider": responses_by_provider,
            "tokens_by_provider": tokens_by_provider,
            "errors_by_provider": errors_by_provider,
            "fallbacks_by_provider": fallbacks_by_provider,
            "capacidad_vetada": capacity_vetoes,
            "paid_calls": calls_by_provider["gemini_paid"],
            "paid_calls_previas": int(policy.get("paid_calls_previas", 0) or 0),
            "paid_calls_nuevas": int(policy.get("paid_calls_nuevas", 0) or 0),
            "tope_efectivo": policy.get("tope_efectivo"),
            "model_requests": telemetry_counters["model_requests"],
            "successful_model_responses": telemetry_counters["successful_model_responses"],
            "google_search_queries": telemetry_counters["google_search_queries"],
            "query_count_unknown": telemetry_counters["query_count_unknown"],
            "grounded_responses": telemetry_counters["grounded_responses"],
            "quota_429": telemetry_counters["quota_429"],
            "telemetria": {
                "providers": providers,
                "model_requests": telemetry_counters["model_requests"],
                "successful_model_responses": telemetry_counters["successful_model_responses"],
                "google_search_queries": telemetry_counters["google_search_queries"],
                "query_count_unknown": telemetry_counters["query_count_unknown"],
                "grounded_responses": telemetry_counters["grounded_responses"],
                "calls_by_provider": calls_by_provider,
                "responses_by_provider": responses_by_provider,
                "errors_by_provider": errors_by_provider,
                "quota_429": telemetry_counters["quota_429"],
                "fallbacks_by_provider": fallbacks_by_provider,
                "capacidad_vetada": capacity_vetoes,
                "paid_calls": calls_by_provider["gemini_paid"],
            },
        },
    }
    if policy.get("max_paid_calls") is not None:
        reached_unit = next(
            (
                f'{payload.get("municipio", "")}/{payload.get("bucket", "")}'
                for payload in payloads
                if payload.get("paid_cap_alcanzado_en_unidad") is True
            ),
            None,
        )
        result["paid_cap"] = {
            "limite": policy.get("tope_efectivo", policy["max_paid_calls"]),
            "alcanzado_en_unidad": reached_unit,
        }
    return result


def rebuild_summary(
    output_dir: Path,
    *,
    policy: Mapping[str, Any] | None = None,
    skipped_existing: int = 0,
) -> dict[str, Any]:
    """Rebuild the final summary exclusively from durable unit JSON files."""
    effective_policy = policy or {
        "grounding_tool": "google_search",
        "retrieval": False,
        "map_grounding": False,
        "confirmation_performed": False,
        "writes_url_map": False,
    }
    return _aggregate_unit_payloads(
        _read_unit_files(output_dir),
        policy=effective_policy,
        skipped_existing=skipped_existing,
    )


def write_outputs(output_dir: Path, rows: Sequence[CandidateRow], summary: Mapping[str, Any]) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "candidates.csv"
    csv_tmp = csv_path.with_name(csv_path.name + ".tmp")
    with csv_tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            csv_row = row.as_dict()
            csv_row["provenance"] = json.dumps(
                csv_row["provenance"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            writer.writerow(csv_row)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(csv_tmp, csv_path)
    _atomic_write_json(output_dir / "summary.json", summary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Propone candidatas URL con Google Search grounding; nunca confirma.")
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument(
        "--url-map",
        type=Path,
        help="CSV municipio,bucket,url usado como hint de dispatch en --adapters-only.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--model", default=REQUIRED_MODEL)
    parser.add_argument("--max-searches", type=int, default=MAX_POLICY_SEARCHES)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--fetch-timeout", type=int, default=30)
    authorization = parser.add_mutually_exclusive_group()
    authorization.add_argument(
        "--free-only", action="store_true",
        help="Secuencia estructural FREE1 -> FREE2 -> STOP; obligatoria para rescate/evaluacion.",
    )
    authorization.add_argument(
        "--paid-authorized", action="store_true",
        help=(
            "Autorizacion explicita de Luis (2026-07-14): habilita "
            "FREE1 -> FREE2 -> PAID."
        ),
    )
    parser.add_argument(
        "--global-call-budget", type=int,
        default=int(os.environ.get("GROUNDED_GLOBAL_CALL_BUDGET", "100")),
    )
    parser.add_argument("--max-paid-calls", type=int)
    parser.add_argument(
        "--daily-model-limit", type=int,
        default=int(os.environ.get("GROUNDED_DAILY_MODEL_LIMIT", str(DEFAULT_DAILY_MODEL_LIMIT))),
    )
    parser.add_argument(
        "--daily-search-limit", type=int,
        default=int(os.environ.get("GROUNDED_DAILY_SEARCH_LIMIT", str(DEFAULT_DAILY_SEARCH_LIMIT))),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Salta unidades completed ya persistidas; reintenta failed o ausentes.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Alias operativo de --resume; solo salta estado completed.",
    )
    parser.add_argument(
        "--micro-acquire",
        action="store_true",
        help="Tras grounding, ejecuta una adquisicion fetch+render por unidad render_incierto pendiente.",
    )
    parser.add_argument(
        "--adapters-only",
        action="store_true",
        help="Ejecuta solo fetch+dispatch para URLs con plataforma detectable; cero modelo.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.adapters_only and (args.free_only or args.paid_authorized):
        parser.error("--adapters-only no se combina con autorizaciones de modelo")
    if args.adapters_only and args.url_map is None:
        parser.error("--url-map es obligatorio con --adapters-only")
    if args.paid_authorized and args.max_paid_calls is None:
        parser.error("--max-paid-calls es obligatorio con --paid-authorized")
    if args.max_paid_calls is not None and args.max_paid_calls < 1:
        parser.error("--max-paid-calls debe ser un entero positivo")
    if args.sleep < 0 or args.fetch_timeout < 1:
        raise ValueError("sleep/fetch-timeout invalidos")
    if not args.adapters_only and not args.free_only and not args.paid_authorized:
        raise PolicyFailure("FALLO_DE_POLITICA:rescate_cli_requiere_--free-only")
    if not args.adapters_only and args.credentials_file is None:
        parser.error("--credentials-file es obligatorio fuera de --adapters-only")
    if args.global_call_budget < 1 or args.daily_model_limit < 1 or args.daily_search_limit < 1:
        raise ValueError("limites_de_cuota_invalidos")
    free_only = args.free_only
    if args.adapters_only:
        client: GroundedClient = _AdaptersOnlyModelGuard()
    else:
        credentials = load_grounded_credentials(args.credentials_file, free_only=free_only)
        client = GeminiGroundedClient(
            credentials,
            free_only=free_only,
            max_paid_calls=(args.max_paid_calls if args.paid_authorized else None),
            global_call_budget=args.global_call_budget,
            daily_model_limit=args.daily_model_limit,
            daily_search_limit=args.daily_search_limit,
        )
    targets = read_targets(args.targets)
    url_map = read_url_map(args.url_map) if args.url_map is not None else {}
    fetcher = RequestsFetcher()
    interruption = InterruptionState()
    previous_handlers: dict[signal.Signals, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interruption.handle)
    try:
        try:
            rows, summary = run_rescue(
                targets,
                client=client,
                fetcher=fetcher,
                model=args.model,
                max_searches=args.max_searches,
                sleep_seconds=args.sleep,
                fetch_timeout=args.fetch_timeout,
                output_dir=args.output_dir,
                resume=args.resume,
                skip_existing=args.skip_existing,
                micro_acquire=args.micro_acquire,
                adapters_only=args.adapters_only,
                url_map=url_map,
                interruption=interruption,
                free_only=free_only,
                paid_authorization=(PAID_AUTHORIZATION if args.paid_authorized else None),
                max_paid_calls=(args.max_paid_calls if args.paid_authorized else None),
            )
        except RescueInterrupted:
            payloads = _read_unit_files(args.output_dir)
            rows = _rows_from_unit_payloads(payloads)
            summary = rebuild_summary(args.output_dir, policy={
                "grounding_tool": "google_search",
                "retrieval": False,
                "map_grounding": False,
                "confirmation_performed": False,
                "writes_url_map": False,
                "free_only": free_only,
                "provider_sequence": (
                    "FREE1->FREE2->STOP" if free_only else "FREE1->FREE2->PAID"
                ),
                **(
                    {"paid_authorization": PAID_AUTHORIZATION}
                    if args.paid_authorized else {}
                ),
                **(
                    {"max_paid_calls": args.max_paid_calls}
                    if args.paid_authorized else {}
                ),
            })
        write_outputs(args.output_dir, rows, summary)
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        _cleanup_tmp_files(args.output_dir)
    LOGGER.info(
        "rescate_completo unidades=%s candidatas=%s",
        summary["global"]["unidades"],
        len(rows),
    )
    if interruption.requested:
        return 130
    states = {unit.get("estado") for unit in summary.get("unidades", {}).values()}
    if "FALLO_DE_POLITICA" in states:
        return 4
    if states & {"DETENIDA_CUOTA_DIARIA_FREE2", "DETENIDA_FRENO_CUOTA"}:
        return 3
    if any(unit.get("causa") == "paid_cap_alcanzado" for unit in summary.get("unidades", {}).values()):
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
