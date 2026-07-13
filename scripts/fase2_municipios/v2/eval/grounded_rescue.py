"""Grounded URL-candidate rescue runner for the F2 V2 adjudication gate.

POLITICA DE LUIS (obligatoria):
- Gemini API con modelo OBLIGATORIO gemini-2.5-pro; gemini-2.5-flash
  UNICAMENTE si la API rechaza Pro explicitamente (capturar y registrar el
  error exacto).
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
import json
import logging
import os
import re
import signal
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from scripts.fase2_municipios.v2 import authority
from scripts.fase2_municipios.v2.agents import certifier
from scripts.fase2_municipios.v2.eval.live_abc_adapter import render_page_networkidle
from scripts.fase2_municipios.v2.eval.live_model_policy import (
    ErrorCategory,
    classify_error,
    load_model_credentials,
)
from scripts.fase2_municipios.v2.eval.platform_probe_runner import (
    Fetcher,
    RequestsFetcher,
    _count_item_markers,
    _norm,
    extract_title_and_text,
)


LOGGER = logging.getLogger(__name__)
REQUIRED_MODEL = "gemini-2.5-pro"
FALLBACK_MODEL = "gemini-2.5-flash"
MAX_POLICY_SEARCHES = 5
SNIPPET_LIMIT = 500
SNAPSHOT_LIMIT = 8000
URL_PATTERN = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.IGNORECASE)
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
)
PROVIDERS = ("gemini_free_1", "gemini_free_2", "gemini_paid")
UNIT_SCHEMA_VERSION = 1


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

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in OUTPUT_COLUMNS}


class GroundedClient(Protocol):
    telemetry: Mapping[str, Any]

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        """Perform one grounded search intent and return candidate evidence."""


class ExplicitProRejection(RuntimeError):
    """The API explicitly rejected gemini-2.5-pro for this request."""

    def __init__(self, exact_error: str, provider: str = "") -> None:
        self.exact_error = exact_error
        self.provider = provider
        super().__init__(exact_error)


class RescueInterrupted(RuntimeError):
    """Cooperative stop used by SIGINT/SIGTERM handling."""


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
        for marker in ("not found", "unsupported", "not supported", "not available", "invalid model")
    )
    return rejection and any(token in text for token in model_tokens)


class GeminiGroundedClient:
    """google-genai adapter with the repository's free/free/paid key policy."""

    def __init__(
        self,
        credentials: Mapping[str, str],
        *,
        client_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if "GEMINI_API_KEY_FREE" not in credentials or "GEMINI_API_KEY" not in credentials:
            raise ValueError("gemini_authorized_credentials_missing")
        if client_factory is None:
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("google-genai no esta instalado") from exc
            client_factory = genai.Client
        self._credentials = credentials
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}
        self._sleep = sleep
        self._calls: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._responses: Counter[str] = Counter()
        self._tokens: Counter[str] = Counter()
        self._quota_rate: Counter[str] = Counter()
        self._fallback_events: list[dict[str, str]] = []

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
            "paid_calls": self._calls["gemini_paid"],
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
            raise
        self._responses[provider] += 1
        usage = getattr(response, "usage_metadata", None)
        total = getattr(usage, "total_token_count", 0) if usage is not None else 0
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self._tokens[provider] += total
        return response

    def _key_policy_call(self, model: str, prompt: str) -> tuple[Any, str, list[dict[str, str]]]:
        last: BaseException | None = None
        events: list[dict[str, str]] = []
        free_steps = (
            ("gemini_free_1", "gemini_free_2")
            if "GEMINI_API_KEY_FREE_2" in self._credentials
            else ("gemini_free_1", "gemini_free_1")
        )
        for attempt, provider in enumerate(free_steps, start=1):
            try:
                return self._invoke(provider, model, prompt), provider, events
            except RescueInterrupted:
                raise
            except BaseException as exc:
                if _is_explicit_model_rejection(exc, model):
                    raise ExplicitProRejection(
                        _safe_error(exc, self._secret_values()), provider
                    ) from exc
                classified = classify_error(exc)
                if not classified.fallback_eligible:
                    raise
                last = exc
                if attempt == 1:
                    event = {
                        "from_provider": provider,
                        "to_provider": free_steps[1],
                        "cause": classified.category.value,
                    }
                    events.append(event)
                    self._fallback_events.append(dict(event))
                    if free_steps[1] == provider:
                        self._sleep(max(1.0, classified.retry_after or 0.0))
        assert last is not None
        event = {
            "from_provider": free_steps[-1],
            "to_provider": "gemini_paid",
            "cause": classify_error(last).category.value,
        }
        events.append(event)
        self._fallback_events.append(dict(event))
        try:
            return self._invoke("gemini_paid", model, prompt), "gemini_paid", events
        except RescueInterrupted:
            raise
        except BaseException as exc:
            if _is_explicit_model_rejection(exc, model):
                raise ExplicitProRejection(
                    _safe_error(exc, self._secret_values()), "gemini_paid"
                ) from exc
            raise

    def search(self, query: str, *, model: str, municipio: str, bucket: str) -> GroundedAnswer:
        prompt = (
            "Atue somente como descobridor de URLs candidatas. Use Google Search. "
            "Nao confirme nem adjudique a URL. Encontre paginas oficiais de indice/listagem, "
            "nunca PDF, noticia individual ou edital individual. Responda com URLs completas "
            "e uma evidencia curta para cada uma. Consulta: " + query
        )
        fallbacks: list[dict[str, str]] = []
        actual_model = model
        try:
            response, provider, key_fallbacks = self._key_policy_call(model, prompt)
            fallbacks.extend(key_fallbacks)
        except ExplicitProRejection as exc:
            if model != REQUIRED_MODEL:
                raise
            fallbacks.append({
                "from_provider": exc.provider or "unknown",
                "to_provider": exc.provider or "unknown",
                "from_model": REQUIRED_MODEL,
                "to_model": FALLBACK_MODEL,
                "cause": "explicit_pro_rejection",
                "exact_error": exc.exact_error,
            })
            actual_model = FALLBACK_MODEL
            try:
                response, provider, key_fallbacks = self._key_policy_call(actual_model, prompt)
                fallbacks.extend(key_fallbacks)
            except RescueInterrupted:
                raise
            except BaseException as fallback_exc:
                raise RuntimeError(_safe_error(fallback_exc, self._secret_values())) from fallback_exc
        except RescueInterrupted:
            raise
        except BaseException as exc:
            raise RuntimeError(_safe_error(exc, self._secret_values())) from exc
        urls, snippets = extract_grounding_metadata(response)
        return GroundedAnswer(
            text=str(getattr(response, "text", "") or ""),
            grounding_urls=tuple(urls),
            grounding_snippets=tuple(snippets),
            model=actual_model,
            provider=provider,
            fallbacks=tuple(fallbacks),
        )

    def _secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for name in ("GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2", "GEMINI_API_KEY")
            if isinstance((value := self._credentials.get(name)), str) and value
        )


def _iter_candidates(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None)
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


def _clean_url(raw: str) -> str:
    value = raw.strip().rstrip(".,;:!?")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def extract_answer_urls(answer: GroundedAnswer) -> list[str]:
    found = list(answer.grounding_urls)
    found.extend(URL_PATTERN.findall(answer.text))
    cleaned = (_clean_url(item) for item in found)
    return list(dict.fromkeys(item for item in cleaned if item))


def build_queries(target: Target) -> list[str]:
    kind = "concursos publicos edital" if target.bucket == "concurso_publico" else "processo seletivo simplificado edital"
    base = f'prefeitura "{target.municipio}" RS {kind} site oficial'
    if target.sub_causa == "render_incierto":
        return [
            f'{base} superficie oficial estatica alternativa com itens. Pista: {target.pista}',
            f'{base} endpoint XHR AJAX da listagem oficial. Pista: {target.pista}',
            f'{base} URL final da listagem oficial apos carregamento. Pista: {target.pista}',
            f'{base} documento ou indice oficial enlazado pela pagina. Pista: {target.pista}',
            f'{base} parametros reproduziveis de filtro e paginacao da listagem. Pista: {target.pista}',
        ]
    if target.sub_causa == "dificil_rederivado":
        qualifier = (
            "onde publica concursos publicos para cargos efetivos; excluir selecao publica, "
            "processo seletivo e contratacao temporaria"
        )
        return [
            f'{base}; {qualifier}. Pista: {target.pista}',
            f'prefeitura "{target.municipio}" RS indice oficial de concurso publico para cargos efetivos',
            f'site:rs.gov.br "{target.municipio}" "concurso publico" edital -"processo seletivo"',
            f'"{target.municipio}" onde publica concursos publicos nao-selecoes site oficial',
            f'prefeitura municipal de "{target.municipio}" concursos publicos historico editais efetivos',
        ]
    return [
        f"{base}. Pista: {target.pista}",
        f'{kind} "{target.municipio}" RS indice listagem prefeitura',
        f'site:rs.gov.br "{target.municipio}" {kind}',
        f'"{target.municipio}" {kind} atende.net OR multi24h',
        f'prefeitura municipal de "{target.municipio}" {kind} todos os anos',
    ]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _is_google_grounding_redirect(url: str) -> bool:
    host = _host(url)
    return host == "vertexaisearch.cloud.google.com" and "grounding-api-redirect" in url


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
    return {
        "providers": normalized,
        "fallback_events": list(telemetry.get("fallback_events", ()) or ()),
    }


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
    return {
        "providers": providers,
        "fallbacks": clean_fallbacks,
        "paid_calls": providers["gemini_paid"]["calls"],
    }


def _candidate_from_dict(value: Mapping[str, Any]) -> CandidateRow:
    return CandidateRow(**{name: value[name] for name in OUTPUT_COLUMNS})


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
            and payload.get("estado") in {"completed", "failed"}
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
) -> dict[str, Any]:
    """Perform exactly one controlled fetch+render acquisition and persist it."""
    initial_url = url
    final_url = url
    trigger = "fetch+render_page_networkidle"
    snapshot_text = ""
    status: Any = None
    render_obtained = False
    error = ""
    if not initial_url:
        trigger = "sin_url_candidata_grounded"
    else:
        try:
            fetched = fetcher.get(initial_url, fetch_timeout)
            final_url = _clean_url(fetched.final_url or initial_url) or initial_url
            rendered = renderer(final_url)
            if rendered is not None:
                render_obtained = True
                final_url = _clean_url(getattr(rendered, "final_url", "") or final_url) or final_url
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
        selected = pending[0] if pending else {
            "url": "",
            "query": "",
            "snippet": "",
            "host_oficial_check": "sin_url_candidata_grounded",
        }
        payload = micro_acquire_unit(
            target,
            selected["url"],
            output_dir=output_dir,
            fetcher=fetcher,
            timestamp_run=timestamp_run,
            fetch_timeout=fetch_timeout,
            renderer=renderer,
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
    interruption: InterruptionState | None = None,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
) -> tuple[list[CandidateRow], dict[str, Any]]:
    if model not in (REQUIRED_MODEL, FALLBACK_MODEL):
        # FALLBACK_MODEL autorizado por Luis (13-jul): canary independiente
        # confirmó que gemini-2.5-pro tiene limit=0 en free tier
        # (429 generate_content_free_tier_requests); Pro queda no disponible
        # para free durante toda la corrida, sin reintentos por unidad.
        raise ValueError(f"model_debe_ser_{REQUIRED_MODEL}_o_{FALLBACK_MODEL}")
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
    }
    output_path = Path(output_dir) if output_dir is not None else None
    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)
        _cleanup_tmp_files(output_path)
    should_resume = resume or skip_existing
    payloads: list[dict[str, Any]] = []
    skipped_existing = 0
    stop_after_current = False
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
        seen: set[str] = set()
        fetched: set[str] = set()
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
                except RescueInterrupted:
                    raise
                except BaseException as exc:
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
                for url in extract_answer_urls(answer):
                    normalized = url.casefold()
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    grounding_redirect = _is_google_grounding_redirect(url)
                    allowed, reason = official_host_check(target.municipio, url)
                    if not allowed and not grounding_redirect:
                        grounded["descartadas"].append({"url": url, "razon": reason})
                        continue
                    if normalized in fetched:
                        continue
                    fetched.add(normalized)
                    status = ""
                    markers = 0
                    positive_quotes: list[str] = []
                    candidate_url = url
                    try:
                        result = fetcher.get(url, fetch_timeout)
                        status = str(result.status_code)
                        candidate_url = _clean_url(result.final_url or url) or url
                        final_allowed, final_reason = official_host_check(target.municipio, candidate_url)
                        if not final_allowed:
                            grounded["descartadas"].append({
                                "url": candidate_url,
                                "razon": "redirect_final_no_oficial" if grounding_redirect else final_reason,
                            })
                            continue
                        reason = (
                            f"google_grounding_redirect->{final_reason}"
                            if grounding_redirect
                            else final_reason
                        )
                        _, visible_text = extract_title_and_text(result.html)
                        markers = _count_item_markers(_norm(visible_text))
                        positive_quotes = _candidate_item_positive_quotes(visible_text, target.bucket)
                    except RescueInterrupted:
                        raise
                    except BaseException as exc:
                        grounded["errores"].append({
                            "query": query,
                            "type": type(exc).__name__,
                            "stage": "fetch",
                        })
                        status = f"error:{type(exc).__name__}"
                        if grounding_redirect:
                            grounded["descartadas"].append({
                                "url": url,
                                "razon": "grounding_redirect_fetch_error",
                            })
                            continue
                    final_normalized = candidate_url.casefold()
                    if final_normalized != normalized and final_normalized in seen:
                        continue
                    seen.add(final_normalized)
                    if target.sub_causa == "render_incierto" and not positive_quotes:
                        grounded["micro_pendientes"].append({
                            "url": candidate_url,
                            "query": query,
                            "snippet": snippet,
                            "host_oficial_check": reason,
                        })
                        continue
                    unit_candidates.append(CandidateRow(
                        municipio=target.municipio,
                        bucket=target.bucket,
                        url_candidata=candidate_url,
                        query_usada=query,
                        snippet_grounding=snippet,
                        host_oficial_check=reason,
                        item_markers=markers,
                        http_status=status,
                    ))
                if index + 1 < len(queries) and sleep_seconds:
                    sleep(sleep_seconds)
            if grounded["queries"] and all("error" in item for item in grounded["queries"]):
                estado = "failed"
                causa = "all_grounded_searches_failed"
            if micro_acquire and target.sub_causa == "render_incierto" and not unit_candidates:
                pending = grounded["micro_pendientes"]
                selected = pending[0] if pending else {
                    "url": "",
                    "query": "",
                    "snippet": "",
                    "host_oficial_check": "sin_url_candidata_grounded",
                }
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
        for provider in PROVIDERS:
            raw = telemetry.get("providers", {}).get(provider, {}) or {}
            for field in providers[provider]:
                providers[provider][field] += int(raw.get(field, 0) or 0)
        for event in telemetry.get("fallbacks", ()) or ():
            provider = str(event.get("from_provider") or "unknown")
            if provider not in fallback_counters:
                provider = "unknown"
            fallback_counters[provider][str(event.get("cause") or "unknown")] += 1
    calls_by_provider = {name: values["calls"] for name, values in providers.items()}
    errors_by_provider = {name: values["errors"] for name, values in providers.items()}
    tokens_by_provider = {name: values["tokens"] for name, values in providers.items()}
    fallbacks_by_provider = {
        name: dict(counter) for name, counter in fallback_counters.items()
    }
    return {
        "policy": dict(policy),
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
            "tokens_by_provider": tokens_by_provider,
            "errors_by_provider": errors_by_provider,
            "fallbacks_by_provider": fallbacks_by_provider,
            "paid_calls": calls_by_provider["gemini_paid"],
            "telemetria": {
                "providers": providers,
                "fallbacks_by_provider": fallbacks_by_provider,
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
    credentials = load_model_credentials(args.credentials_file)
    client = GeminiGroundedClient(credentials)
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
    return 130 if interruption.requested else 0


if __name__ == "__main__":
    raise SystemExit(main())
