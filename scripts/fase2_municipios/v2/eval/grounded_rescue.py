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

from scripts.fase2_municipios.v2 import authority
from scripts.fase2_municipios.v2.agents import certifier
from scripts.fase2_municipios.v2.eval.live_abc_adapter import render_page_networkidle
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
)
PROVIDERS = ("gemini_free_1", "gemini_free_2", "gemini_paid")
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

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in OUTPUT_COLUMNS}


class GroundedClient(Protocol):
    telemetry: Mapping[str, Any]

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        """Perform one grounded search intent and return candidate evidence."""


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
                    if provider == "gemini_free_2" and classified.category is ErrorCategory.QUOTA_429:
                        if self._daily_quota_exhausted(exc):
                            raise DailyQuotaExhausted("free2_daily_quota_exhausted") from exc
                        delay = self._governor.backoff_seconds(attempt, classified.retry_after)
                        if attempt < attempts:
                            self._sleep(delay)
                            continue
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
        try:
            return self._free_only_call(model, prompt)
        except (RescueInterrupted, PreventiveQuotaStop, DailyQuotaExhausted, PolicyFailure):
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
        except (RescueInterrupted, PreventiveQuotaStop, DailyQuotaExhausted, PolicyFailure):
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
                "completed", "failed", "DETENIDA_CUOTA_DIARIA_FREE2",
                "DETENIDA_FRENO_CUOTA", "FALLO_DE_POLITICA",
            }
        ):
            payloads.append(payload)
    return payloads


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
) -> dict[str, Any]:
    """Perform exactly one controlled fetch+render acquisition and persist it."""
    initial_url = url
    final_url = url
    trigger = "fetch+render_page_networkidle"
    snapshot_text = ""
    status: Any = None
    render_obtained = False
    redirect_chain = [item for item in (prior_redirector, initial_url) if item]
    error = ""
    if not initial_url:
        trigger = "sin_url_candidata_grounded"
    else:
        try:
            fetched = fetcher.get(initial_url, fetch_timeout)
            final_url = _clean_url(fetched.final_url or initial_url) or initial_url
            if final_url and final_url != redirect_chain[-1]:
                redirect_chain.append(final_url)
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
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"micro_{target.municipio}_{target.bucket}.json"
    _atomic_write_json(path, payload)
    return payload


def _select_micro_target(target: Target, pending: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Prefer a replay URL; otherwise require an evaluated, unambiguous candidate."""
    original_urls = extract_answer_urls(GroundedAnswer(text=target.pista))
    for original in original_urls:
        allowed, reason = official_host_check(target.municipio, original)
        if allowed:
            return {
                "url": original, "query": "", "snippet": target.pista,
                "host_oficial_check": reason, "fuente": "target_original",
                "redirector_original": "", "selection_reason": "url_original_target_replay",
            }
    evaluated = [dict(item) for item in pending if item.get("url") and item.get("host_oficial_check")]
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
) -> list[CandidateRow]:
    """Run one controlled acquisition per unresolved render-incierto unit.

    A unit with no grounded URL still receives a durable, fail-closed artifact
    explaining that no acquisition target existed.  It never calls the fetcher
    or renderer and cannot create a candidate.
    """
    result_rows = list(rows)
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
        )
        unit["micro_archivo"] = f"micro_{target.municipio}_{target.bucket}.json"
        unit["micro_veredicto"] = payload["veredicto_gate"]
        if payload["veredicto_gate"]["pasa"]:
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
    renderer: Callable[[str], Any] = render_page_networkidle,
    redirect_session_factory: Callable[[], Any] = _new_redirect_session,
    redirect_host_resolver: Callable[[str], Sequence[str]] = _default_redirect_host_resolver,
    interruption: InterruptionState | None = None,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
    free_only: bool = False,
) -> tuple[list[CandidateRow], dict[str, Any]]:
    if model != REQUIRED_MODEL:
        raise ValueError(f"model_debe_ser_exactamente_{REQUIRED_MODEL}")
    if not 1 <= max_searches <= MAX_POLICY_SEARCHES:
        raise ValueError("max_searches_debe_estar_entre_1_y_5")
    policy = {
        "grounding_tool": "google_search",
        "retrieval": False,
        "map_grounding": False,
        "max_searches_per_unit": max_searches,
        "confirmation_performed": False,
        "writes_url_map": False,
        "micro_acquire": micro_acquire,
        "free_only": free_only,
        "provider_sequence": "FREE1->FREE2->STOP" if free_only else "FREE1->FREE2->PAID",
    }
    if free_only and _provider_snapshot(client)["paid_calls"] != 0:
        raise PolicyFailure("FALLO_DE_POLITICA:paid_calls_before_run")
    output_path = Path(output_dir) if output_dir is not None else None
    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)
        _cleanup_tmp_files(output_path)
    should_resume = resume or skip_existing
    payloads: list[dict[str, Any]] = []
    skipped_existing = 0
    stop_after_current = False
    redirect_cache: dict[str, str | None] = {}
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
        queries = build_queries(target)[:max_searches]
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
            if (
                estado == "completed"
                and grounded["queries"]
                and all("error" in item for item in grounded["queries"])
            ):
                estado = "failed"
                causa = "all_grounded_searches_failed"
            if (
                estado == "completed" and micro_acquire
                and target.sub_causa == "render_incierto" and not unit_candidates
            ):
                pending = grounded["micro_pendientes"]
                selected = _select_micro_target(target, pending)
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
                )
                if micro_result["veredicto_gate"]["pasa"]:
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
    if output_path is not None:
        summary = rebuild_summary(output_path, policy=policy, skipped_existing=skipped_existing)
        candidates = _rows_from_unit_payloads(_read_unit_files(output_path))
    else:
        summary = _aggregate_unit_payloads(payloads, policy=policy, skipped_existing=0)
        candidates = _rows_from_unit_payloads(payloads)
    if free_only and summary["global"]["paid_calls"] != 0:
        summary["global"]["estado_corrida"] = "FALLO_DE_POLITICA"
        raise PolicyFailure("FALLO_DE_POLITICA:paid_calls_after_run")
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
    return {
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
        writer.writerows(row.as_dict() for row in rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(csv_tmp, csv_path)
    _atomic_write_json(output_dir / "summary.json", summary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Propone candidatas URL con Google Search grounding; nunca confirma.")
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--model", default=REQUIRED_MODEL)
    parser.add_argument("--max-searches", type=int, default=MAX_POLICY_SEARCHES)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--fetch-timeout", type=int, default=30)
    parser.add_argument(
        "--free-only", action="store_true",
        help="Secuencia estructural FREE1 -> FREE2 -> STOP; obligatoria para rescate/evaluacion.",
    )
    parser.add_argument(
        "--global-call-budget", type=int,
        default=int(os.environ.get("GROUNDED_GLOBAL_CALL_BUDGET", "100")),
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.sleep < 0 or args.fetch_timeout < 1:
        raise ValueError("sleep/fetch-timeout invalidos")
    if not args.free_only:
        raise PolicyFailure("FALLO_DE_POLITICA:rescate_cli_requiere_--free-only")
    if args.global_call_budget < 1 or args.daily_model_limit < 1 or args.daily_search_limit < 1:
        raise ValueError("limites_de_cuota_invalidos")
    credentials = load_grounded_credentials(args.credentials_file, free_only=True)
    client = GeminiGroundedClient(
        credentials,
        free_only=True,
        global_call_budget=args.global_call_budget,
        daily_model_limit=args.daily_model_limit,
        daily_search_limit=args.daily_search_limit,
    )
    targets = read_targets(args.targets)
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
                interruption=interruption,
                free_only=True,
            )
        except RescueInterrupted:
            payloads = _read_unit_files(args.output_dir)
            rows = _rows_from_unit_payloads(payloads)
            summary = rebuild_summary(args.output_dir)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
