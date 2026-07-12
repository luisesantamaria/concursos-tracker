"""Opt-in live A/B/C adapter with one fetched snapshot per target.

The public constructor accepts already-built role adapters for offline tests and
for dependency injection.  :meth:`from_free_environment` is the only real-model
factory: it resolves ``GEMINI_API_KEY_FREE`` through the existing client policy,
constructs one transport, and shares the existing project limiter across A/B/C.
No public API in this module accepts an API key, grounding, or native tools.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
import codecs
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import gzip
import hashlib
import http.client
import re
import socket
import time
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
import zlib

from pydantic import ValidationError

try:
    import brotli as _brotli
except ImportError:  # Optional: do not advertise br when no decoder is installed.
    _brotli = None

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2 import authority
from scripts.shared import waf_guard
from scripts.fase2_municipios.v2.agents import (
    ABCOrchestrator,
    AgentError,
    ConflictJudge,
    DecisionProposal,
    JudgeOutcome,
    ProposalValidationError,
    build_certifier_agent,
    build_conflict_judge,
    build_prosecutor_agent,
)
from scripts.fase2_municipios.v2.agents.base import SnapshotInvalidOutput
from scripts.fase2_municipios.v2.eval.cassette_producer import (
    ABCLayer,
    CandidateLayer,
    CitationLayer,
    EvidenceLayer,
    ExternalAccessBlocked,
    ProposalLayer,
    SourceLayer,
)
from scripts.fase2_municipios.v2.eval.live_model_policy import (
    ErrorCategory,
    EvidenceInsufficientError,
    ModelPolicyTelemetry,
    PolicyTransport,
    SemanticModelError,
    classify_error,
)
from scripts.fase2_municipios.v2.eval.live_observability import StageArtifactWriter
from scripts.fase2_municipios.v2.eval.structural_evidence import structural_candidate
from scripts.fase2_municipios.v2.gemini import (
    GeminiClientError,
    RealGeminiTransport,
    RoleModels,
    build_judge_client,
    resolve_free_api_key,
)
from scripts.fase2_municipios.v2.ratelimit import get_shared_limiter
from scripts.fase2_municipios.v2.snapshot import (
    EvidenceSnapshot,
    EvidenceSource,
    SnapshotError,
    build_snapshot,
)


VALID_BUCKETS = frozenset({"concurso_publico", "processo_seletivo"})


class LiveABCConfigurationError(ValueError):
    """The live adapter cannot run safely with the supplied configuration."""


class LiveFetchError(EvidenceInsufficientError):
    """An HTTP response was reached but could not become admissible evidence."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class ModelResponseValidationError(SemanticModelError):
    """A role returned a value that cannot satisfy the A/B/C response contract."""


def _looks_like_zlib(payload: bytes) -> bool:
    if len(payload) < 2 or payload[0] != 0x78:
        return False
    return payload[0] & 0x0F == 8 and int.from_bytes(payload[:2], "big") % 31 == 0


def _content_codings(payload: bytes, content_encoding: str) -> tuple[str, ...]:
    declared = tuple(
        item.strip().lower()
        for item in content_encoding.split(",")
        if item.strip() and item.strip().lower() != "identity"
    )
    if declared:
        return declared
    if payload.startswith(b"\x1f\x8b"):
        return ("gzip",)
    if _looks_like_zlib(payload):
        return ("deflate",)
    return ()


def _decompress_payload(payload: bytes, content_encoding: str) -> bytes:
    """Decode HTTP content codings, falling back to gzip/zlib magic bytes."""

    decoded = payload
    # Content codings are listed in application order and removed in reverse.
    for coding in reversed(_content_codings(payload, content_encoding)):
        try:
            if coding in {"gzip", "x-gzip"}:
                decoded = gzip.decompress(decoded)
            elif coding == "deflate":
                try:
                    decoded = zlib.decompress(decoded)
                except zlib.error as wrapped_error:
                    try:
                        decoded = zlib.decompress(decoded, -zlib.MAX_WBITS)
                    except zlib.error as raw_error:
                        raise raw_error from wrapped_error
            elif coding == "br":
                if _brotli is None:
                    raise LiveFetchError("brotli_decoder_unavailable")
                decoded = _brotli.decompress(decoded)
            else:
                raise LiveFetchError(f"unsupported_content_encoding:{coding}")
        except LiveFetchError:
            raise
        except Exception as exc:
            raise LiveFetchError("response_decompression_failed") from exc
    return decoded


_DOCUMENT_CHARSET_RE = re.compile(
    br"<meta\b[^>]*\bcharset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_BOMS = (
    (codecs.BOM_UTF32_BE, "utf-32", "utf-32-be"),
    (codecs.BOM_UTF32_LE, "utf-32", "utf-32-le"),
    (codecs.BOM_UTF8, "utf-8-sig", "utf-8"),
    (codecs.BOM_UTF16_BE, "utf-16", "utf-16-be"),
    (codecs.BOM_UTF16_LE, "utf-16", "utf-16-le"),
)


def _canonical_charset(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return codecs.lookup(value.strip()).name
    except LookupError:
        return None


# Codificacion UTF-8 del bloque Latin-1 Supplement (U+00C0-U+00FF): cubre todos
# los acentos/cedilla del portugues. Prueba binaria de utf-8 genuino en el body.
_MOJIBAKE_UTF8_FINGERPRINT_RE = re.compile(rb"\xc3[\x80-\xbf]")
# Umbral minimo de ocurrencias antes de asumir utf-8 genuino con byte(s) suelto(s)
# invalido(s). Una sola coincidencia no basta: en portugues cp1252 el byte 0xC3
# ES 'Ã' (SELEÇÃO, NÃO, CIDADÃO) y casi siempre va seguido de una letra ASCII
# ('O'/'E'), fuera del rango de continuacion [0x80-0xBF] -- por eso 'Ã' real casi
# nunca dispara el fingerprint. Pero un documento cp1252 grande puede chocar
# incidentalmente 1-2 veces (un script, un comentario, un atributo) sin ser
# utf-8; decodificar TODO el documento con errors='replace' en ese caso
# destruiria acentos genuinos (caso Porto Alegre al reves: un doc cp1252 mal
# etiquetado utf-8 perderia todo su acentuado). Un doc utf-8 real con acentos
# porto-alegrenses produce cientos de matches, muy por encima de este umbral.
_MOJIBAKE_UTF8_FINGERPRINT_MIN_MATCHES = 3


def _decode_response_payload(
    payload: bytes, declared_charset: str | None
) -> tuple[str, tuple[str, ...]]:
    """Decode HTTP bytes in the normative strict order, preserving diagnostics."""

    diagnostics: list[str] = []
    canonical_header = _canonical_charset(declared_charset)
    if declared_charset and canonical_header is None:
        diagnostics.append("invalid_header_charset")

    for marker, decoder, bom_name in _BOMS:
        if not payload.startswith(marker):
            continue
        if canonical_header and canonical_header not in {
            bom_name, decoder, "utf-16", "utf-32"
        }:
            diagnostics.append(
                f"charset_conflict:bom={bom_name},header={canonical_header}"
            )
        try:
            return payload.decode(decoder), tuple((f"bom:{bom_name}", *diagnostics))
        except UnicodeDecodeError as exc:
            raise LiveFetchError("response_decode_failed") from exc

    if canonical_header is not None:
        try:
            return payload.decode(canonical_header), tuple(
                (f"header_charset:{canonical_header}", *diagnostics)
            )
        except UnicodeDecodeError:
            # Charset DECLARADO que miente (comunisimo en portales municipales:
            # header utf-8 con bytes latin-1/cp1252, caso Porto Alegre). No se
            # falla cerrado: se registra el conflicto como diagnostico auditable
            # y se continua la cadena determinista. El BOM corrupto (arriba)
            # sigue siendo estricto: es evidencia binaria de payload roto.
            diagnostics.append(
                f"declared_charset_decode_failed:{canonical_header}"
            )

    declared_utf8 = canonical_header == "utf-8"
    meta = _DOCUMENT_CHARSET_RE.search(payload[:8192])
    if meta is not None:
        raw_meta = meta.group(1).decode("ascii")
        document_charset = _canonical_charset(raw_meta)
        if document_charset is None:
            diagnostics.append("invalid_document_charset")
        else:
            declared_utf8 = declared_utf8 or document_charset == "utf-8"
            try:
                return payload.decode(document_charset), tuple(
                    (f"document_charset:{document_charset}", *diagnostics)
                )
            except UnicodeDecodeError:
                diagnostics.append(
                    f"declared_charset_decode_failed:{document_charset}"
                )

    # Caso Porto Alegre (golden36): la fuente declara utf-8 y el body ES utf-8
    # genuino con algun byte suelto invalido. Caer a cp1252 destruiria TODO el
    # acentuado (mojibake) y romperia las citas literales. Fingerprint
    # ESTRUCTURAL: la secuencia 0xC3 + byte de continuacion [0x80-0xBF] es
    # exactamente la codificacion UTF-8 del bloque Latin-1 Supplement (todos
    # los acentos y la cedilla del portugues). Una unica coincidencia NO
    # basta como prueba (ver comentario del umbral arriba): se exige un
    # minimo de ocurrencias para blindar contra un choque incidental en un
    # documento cp1252 genuino mal etiquetado utf-8. Solo aplica cuando el
    # charset DECLARADO era utf-8 y su decode estricto fallo; el byte roto
    # queda marcado como U+FFFD (visible), nunca oculto.
    if (
        declared_utf8
        and len(_MOJIBAKE_UTF8_FINGERPRINT_RE.findall(payload))
        >= _MOJIBAKE_UTF8_FINGERPRINT_MIN_MATCHES
    ):
        diagnostics.append("utf8_replace_recovered_declared_charset")
        return payload.decode("utf-8", errors="replace"), tuple(diagnostics)

    try:
        return payload.decode("utf-8"), tuple(("fallback:utf-8", *diagnostics))
    except UnicodeDecodeError:
        pass
    for fallback in ("cp1252", "latin-1"):
        try:
            return payload.decode(fallback), tuple((f"fallback:{fallback}", *diagnostics))
        except UnicodeDecodeError:
            continue
    raise LiveFetchError("response_decode_failed")


# Bytes crudos conservados en la anomalia (base64 de la cabeza, no el payload
# entero) para poder auditar un decode dudoso sin persistir el body completo.
_RAW_PAYLOAD_HEAD_BYTES = 65536


def _decode_diagnostics_show_charset_anomaly(diagnostics: tuple[str, ...]) -> bool:
    """True cuando el decode tuvo que apartarse del charset declarado."""
    return any(
        item == "utf8_replace_recovered_declared_charset"
        or item.startswith("declared_charset_decode_failed")
        for item in diagnostics
    )


def _live_request_headers() -> dict[str, str]:
    headers = dict(cascade.REQUEST_HEADERS)
    if _brotli is None:
        advertised = headers.get("Accept-Encoding", "")
        headers["Accept-Encoding"] = ", ".join(
            item.strip() for item in advertised.split(",")
            if item.strip().lower() != "br"
        )
    return headers


class LiveCauseKind(str, Enum):
    SUCCESS = "success"
    LEGITIMATE_ABSENCE = "legitimate_absence"
    ACCESS_FAILURE = "access_failure"
    MODEL_FAILURE = "model_failure"
    EVIDENCE_FAILURE = "evidence_failure"
    DISAGREEMENT_UNRESOLVED = "disagreement_unresolved"
    CONFIGURATION_FAILURE = "configuration_failure"
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True)
class LiveCause:
    kind: LiveCauseKind
    code: str
    comment: str
    revisar_por: str = ""


@dataclass(frozen=True)
class LiveAuditEvent:
    """Stable exception evidence captured at one live processing boundary.

    Events are append-only and ordered by processing phase.  Each event stores
    the outer exception followed by its explicit cause (or implicit context),
    with object-identity de-duplication so chained exceptions occur once.
    """

    phase: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class LiveABCOutcome:
    municipio: str
    bucket: str
    decision: str
    url: str
    cause: LiveCause
    layer: ABCLayer | None
    evidence_snapshot: EvidenceSnapshot | None = field(
        default=None, compare=False, repr=False
    )
    original_exception: BaseException | None = None
    audit_events: tuple[LiveAuditEvent, ...] = ()


@dataclass(frozen=True)
class FetchedEvidence:
    requested_url: str
    final_url: str
    retrieved_at: datetime
    status: int
    content: str
    html: str
    title: str
    decode_diagnostics: tuple[str, ...] = ()
    # Preservacion de bytes crudos (hash SIEMPRE, blob SOLO en anomalia): el
    # sha256 se calcula sobre el payload de bytes antes de decodificar y no
    # cuesta nada guardarlo siempre. raw_payload_head_b64/raw_payload_truncated
    # solo se pueblan cuando decode_diagnostics muestra una anomalia de charset
    # (ver _decode_diagnostics_show_charset_anomaly) -- no tiene sentido cargar
    # 64KB en base64 en memoria para cada fetch limpio.
    raw_payload_sha256: str = ""
    raw_payload_head_b64: str = ""
    raw_payload_truncated: bool = False
    # Espeja cascade.EvidenceSnapshot.evidence_state. Por defecto el fetch
    # plano-HTTP es 'completa'; RenderFallbackFetcher devuelve 'renderizada'
    # cuando el render-once limpio un shell SPA/challenge antibot (ver clase
    # 3 del QA 20260712: fixture_qa.json). El gate downstream ya acepta ambos
    # (orchestration._safety_blockers).
    evidence_state: str = "completa"

    def __post_init__(self) -> None:
        if not self.requested_url or not self.final_url:
            raise LiveFetchError("fetch_url_missing")
        if self.retrieved_at.tzinfo is None:
            raise LiveFetchError("fetch_timestamp_not_timezone_aware")
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise LiveFetchError("fetch_status_invalid")
        if not isinstance(self.content, str) or not isinstance(self.html, str):
            raise LiveFetchError("fetch_content_invalid")
        if not isinstance(self.evidence_state, str) or not self.evidence_state:
            raise LiveFetchError("fetch_evidence_state_invalid")


class OrionFetcher(Protocol):
    def fetch(self, url: str, *, timeout_seconds: float) -> FetchedEvidence: ...


class CertifierRole(Protocol):
    def certify(self, *, snapshot: EvidenceSnapshot, task: str) -> Any: ...


class ProsecutorRole(Protocol):
    def audit(
        self, *, snapshot: EvidenceSnapshot, certifier_output: Mapping[str, Any]
    ) -> Any: ...


class JudgeRole(Protocol):
    def choose(
        self,
        *,
        snapshot: EvidenceSnapshot,
        candidates,
        proposal_a: Mapping[str, Any],
        proposal_b: Mapping[str, Any],
    ) -> JudgeOutcome: ...


class OrionHTTPFetcher:
    """Single plain-HTTP ownership seam for Orion's directed target fetch.

    It reuses the project's real browser-like requests session, performs exactly
    one ``GET``, and deliberately does not call the legacy fallback fetcher (which
    converts exceptions to ``Page.error``).  Therefore
    :class:`ExternalAccessBlocked` and timeouts propagate unchanged from this
    low-level boundary to :class:`LiveABCAdapter`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        max_redirects: int = 5,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
    ) -> None:
        if (
            isinstance(max_redirects, bool)
            or not isinstance(max_redirects, int)
            or max_redirects < 0
        ):
            raise LiveABCConfigurationError("max_redirects_must_be_non_negative")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_redirects = max_redirects
        for name, value in (
            ("connect_timeout_seconds", connect_timeout_seconds),
            ("read_timeout_seconds", read_timeout_seconds),
        ):
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
            ):
                raise LiveABCConfigurationError(f"{name}_must_be_positive")
        self._connect_timeout_seconds = (
            float(connect_timeout_seconds) if connect_timeout_seconds is not None else None
        )
        self._read_timeout_seconds = (
            float(read_timeout_seconds) if read_timeout_seconds is not None else None
        )

    @staticmethod
    def _connection(parsed, timeout_seconds: float):
        host = parsed.hostname
        if not host:
            raise LiveFetchError("fetch_host_missing")
        port = parsed.port
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                host,
                port=port,
                timeout=timeout_seconds,
            )
        elif parsed.scheme == "http":
            connection = http.client.HTTPConnection(
                host,
                port=port,
                timeout=timeout_seconds,
            )
        else:
            raise LiveFetchError("fetch_scheme_must_be_http_or_https")
        # http.client otherwise retains an import-time alias. This dynamic seam
        # is intentional: the V2 guard owns socket.create_connection.
        connection._create_connection = socket.create_connection
        return connection

    def fetch(self, url: str, *, timeout_seconds: float) -> FetchedEvidence:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise LiveABCConfigurationError("timeout_seconds_must_be_positive")
        requested_url = url
        current_url = url
        connect_timeout = self._connect_timeout_seconds or float(timeout_seconds)
        read_timeout = self._read_timeout_seconds or float(timeout_seconds)
        response = None
        payload = b""
        for redirect_count in range(self._max_redirects + 1):
            parsed = urlsplit(current_url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            for fetch_attempt in range(2):
                connection = self._connection(parsed, connect_timeout)
                try:
                    connection.request("GET", path, headers=_live_request_headers())
                    sock = getattr(connection, "sock", None)
                    if sock is not None:
                        sock.settimeout(read_timeout)
                    response = connection.getresponse()
                    payload = response.read()
                    break
                except ExternalAccessBlocked:
                    raise
                except (TimeoutError, OSError):
                    if fetch_attempt == 1:
                        raise
                finally:
                    connection.close()
            status = response.status
            if status in {301, 302, 303, 307, 308}:
                location = response.getheader("location")
                if not location:
                    raise LiveFetchError("redirect_without_location")
                if redirect_count == self._max_redirects:
                    raise LiveFetchError("too_many_redirects")
                current_url = urljoin(current_url, location)
                continue
            break
        assert response is not None
        status = response.status
        if status < 200 or status >= 400:
            raise LiveFetchError("http_status", status_code=status)
        content_type = str(response.getheader("content-type", ""))
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise LiveFetchError("response_not_html_or_text")
        content_encoding = str(response.getheader("content-encoding", ""))
        payload = _decompress_payload(payload, content_encoding)
        raw_payload_sha256 = hashlib.sha256(payload).hexdigest()
        declared_charset = response.headers.get_content_charset()
        response_text, decode_diagnostics = _decode_response_payload(
            payload, declared_charset
        )
        raw_payload_head_b64 = ""
        raw_payload_truncated = False
        if _decode_diagnostics_show_charset_anomaly(decode_diagnostics):
            head = payload[:_RAW_PAYLOAD_HEAD_BYTES]
            raw_payload_head_b64 = base64.b64encode(head).decode("ascii")
            raw_payload_truncated = len(payload) > _RAW_PAYLOAD_HEAD_BYTES
        final_url = current_url
        page = cascade._page_from_html(
            final_url,
            status,
            content_type,
            response_text,
            requested_url=requested_url,
        )
        return FetchedEvidence(
            requested_url=requested_url,
            final_url=final_url,
            retrieved_at=self._clock(),
            status=status,
            content=page.text,
            html=response_text,
            title=page.title,
            decode_diagnostics=decode_diagnostics,
            raw_payload_sha256=raw_payload_sha256,
            raw_payload_head_b64=raw_payload_head_b64,
            raw_payload_truncated=raw_payload_truncated,
        )


# Fixed synthetic content-type for the objective SPA/antibot revalidation that
# RenderFallbackFetcher performs over plain-HTTP and rendered HTML alike:
# neither carries a real HTTP header at that point, and cascade._page_from_html
# only branches on "text/html"/"text/plain" membership, so any well-formed
# text/html value is equivalent here.
_RENDER_REVALIDATION_CONTENT_TYPE = "text/html; charset=UTF-8"

# Shell delgado OBJETIVO (QA 12-jul): los shells client-rendered de
# atende.net (207KB de HTML) y oxy.elotech (2.9KB, mount React con bundles)
# sirven 45-67 chars de texto visible, pero NO llevan markers Next/Nuxt/React,
# asi que Page.is_spa no los detecta. La firma estructural comun (no un
# hardcode municipal): pagina OK cuyo texto visible es casi nulo pero que
# carga bundles JS externos (<script src=...>) -- no hay nada que el
# certificador pueda citar sin render. Una pagina estatica legitimamente
# escueta sin bundles queda fuera; si un shell falso-positivo se renderiza,
# la regla de mejora estricta de texto conserva la evidencia original.
_THIN_SHELL_MIN_HTML_CHARS = 2_000
_THIN_SHELL_MAX_TEXT_CHARS = 500
_SCRIPT_SRC_RE = re.compile(r"<script[^>]+\bsrc\s*=", re.IGNORECASE)


def _is_thin_shell(page: "cascade.Page") -> bool:
    html = page.html or ""
    return bool(
        page.ok
        and len(html) >= _THIN_SHELL_MIN_HTML_CHARS
        and len((page.text or "").strip()) < _THIN_SHELL_MAX_TEXT_CHARS
        and _SCRIPT_SRC_RE.search(html)
    )


def render_page_networkidle(url: str):
    """Render-once V2: como ``cascade.render_page_sync`` pero esperando a que
    el SPA cargue datos reales.

    El render de cascade espera 2000ms fijos tras domcontentloaded; los shells
    de atende.net terminan con title correcto y body VACIO (verificado en vivo
    12-jul: Gramado 0 chars). Aqui: networkidle acotado + sondeo del texto del
    body hasta ~8s. Reutiliza el browser singleton y el perfil pt-BR de
    cascade/playwright_net (neutrales); cascade queda intocado.
    """
    try:
        browser = cascade._get_browser()
    except Exception:
        return None
    context = None
    try:
        # Contexto propio, NO cascade.new_browser_context: el perfil compartido
        # aplica PLAYWRIGHT_EXTRA_HTTP_HEADERS (Sec-Fetch-Dest: document,
        # Sec-Fetch-Mode: navigate, ...) a TODAS las requests, y Chromium
        # rechaza esos overrides en subrecursos (CSS/JS/XHR) con
        # net::ERR_INVALID_ARGUMENT -- el app JS nunca carga y el SPA queda en
        # blanco (biseccion verificada en vivo 12-jul con gramado.atende.net:
        # bare/opts/init_script renderizan ~1300-1600 chars; headers -> 0).
        from scripts.shared.browser_profile import (
            HUMAN_BROWSER_INIT_SCRIPT,
            PLAYWRIGHT_CONTEXT_OPTIONS,
        )

        options = dict(PLAYWRIGHT_CONTEXT_OPTIONS)
        options["ignore_https_errors"] = True
        options["extra_http_headers"] = {
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }
        context = browser.new_context(**options)
        context.add_init_script(HUMAN_BROWSER_INIT_SCRIPT)
        browser_page = context.new_page()
        response = browser_page.goto(
            url, wait_until="domcontentloaded", timeout=20000,
        )
        try:
            browser_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # networkidle puede no llegar (polling/analytics); sondeamos.
        body_text = ""
        for _ in range(16):
            body_text = browser_page.locator("body").inner_text()
            if len(body_text.strip()) >= _THIN_SHELL_MAX_TEXT_CHARS:
                break
            browser_page.wait_for_timeout(500)
        links = browser_page.eval_on_selector_all(
            "a[href]",
            "els => els.map(el => [el.href, (el.innerText || '').trim()])",
        )
        return cascade.RenderedPage(
            html=browser_page.content(),
            text=body_text,
            title=browser_page.title(),
            requested_url=url,
            final_url=browser_page.url,
            status=response.status if response is not None else None,
            links=tuple((href, text) for href, text in links),
        )
    except Exception:
        return None
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


class RenderFallbackFetcher:
    """One-shot headless-render fallback for SPA shells / anti-bot challenges.

    Wraps another :class:`OrionFetcher` (``inner``, ``OrionHTTPFetcher()`` by
    default). It changes nothing for a page that is already usable: the
    plain-HTTP :class:`FetchedEvidence` is revalidated with
    ``cascade._page_from_html`` (the same objective, hard-marker detector the
    rest of the pipeline uses) and returned unchanged unless that revalidation
    shows ``is_spa`` or the STRICT ``is_antibot`` (never the lax
    ``cascade.is_antibot_challenge``, which false-positives on benign
    Cloudflare banners -- see ``structural_evidence.structural_candidate``).

    Only then -- and only if the URL's provider group is not already frozen
    -- does it spend exactly one Playwright render (``render_once``,
    ``cascade.render_page_sync`` by default) and revalidate the rendered DOM
    the same way. Freezing (``waf.freeze``) fires only when the render itself
    still shows the hard challenge, never for a merely-thin/empty render, so a
    legitimately sparse page does not poison the shared per-provider freeze
    for the next unit.

    A transport-level failure from ``inner`` (``LiveFetchError``, timeouts,
    ``ExternalAccessBlocked``) is never caught here -- it propagates
    unchanged, fail-closed, exactly as it would without this wrapper. Any
    failure to render (``None`` or an exception) or a render that does not
    genuinely clear the page is likewise a no-op: the original plain-HTTP
    evidence is returned, never a regression.
    """

    def __init__(
        self,
        inner: OrionFetcher | None = None,
        render_once: Callable[[str], Any] | None = None,
        waf: Any | None = None,
    ) -> None:
        self._inner: OrionFetcher = inner if inner is not None else OrionHTTPFetcher()
        # Default V2 (networkidle + sondeo de texto), no cascade.render_page_sync:
        # el wait fijo de 2000ms de cascade devuelve body vacio en los shells
        # de atende.net (verificado en vivo 12-jul).
        self._render_once: Callable[[str], Any] = (
            render_once if render_once is not None else render_page_networkidle
        )
        self._waf = waf if waf is not None else waf_guard

    @staticmethod
    def _revalidate(
        *, final_url: str, status: int | None, html: str, requested_url: str
    ) -> cascade.Page:
        return cascade._page_from_html(
            final_url,
            status,
            _RENDER_REVALIDATION_CONTENT_TYPE,
            html,
            requested_url=requested_url,
        )

    def fetch(self, url: str, *, timeout_seconds: float) -> FetchedEvidence:
        # Transport failures from `inner` are not caught: they bubble intact
        # (fail-closed at the transport boundary, never masked by a render
        # attempt).
        fetched = self._inner.fetch(url, timeout_seconds=timeout_seconds)

        page = self._revalidate(
            final_url=fetched.final_url,
            status=fetched.status,
            html=fetched.html,
            requested_url=fetched.requested_url,
        )
        if not (page.is_antibot or page.is_spa or _is_thin_shell(page)):
            return fetched
        if self._waf.is_frozen(url):
            return fetched  # Provider already frozen: do not burn a pass.

        try:
            rendered = self._render_once(url)
        except Exception:
            rendered = None
        if rendered is None:
            return fetched

        rendered_page = self._revalidate(
            final_url=rendered.final_url or url,
            status=rendered.status,
            html=rendered.html,
            requested_url=url,
        )
        if rendered_page.is_antibot:
            self._waf.freeze(url)
            return fetched
        rendered_text = (rendered.text or "").strip()
        if rendered_page.is_spa or not rendered_text:
            return fetched
        # El render debe MEJORAR estrictamente el texto visible: un render que
        # no aporta mas contenido que el shell original no justifica sustituir
        # la evidencia (fail-closed; la original queda intacta).
        if len(rendered_text) <= len((fetched.content or "").strip()):
            return fetched

        return replace(
            fetched,
            final_url=rendered.final_url or fetched.final_url,
            status=(
                rendered.status
                if isinstance(rendered.status, int)
                and not isinstance(rendered.status, bool)
                else fetched.status
            ),
            content=rendered.text,
            html=rendered.html,
            title=rendered.title or fetched.title,
            evidence_state="renderizada",
            decode_diagnostics=(
                *fetched.decode_diagnostics, "render_fallback_applied",
            ),
        )


class _RecordingJudge:
    def __init__(
        self,
        delegate: JudgeRole,
        observer: Callable[[Mapping[str, Any]], None] | None = None,
        model: str = "",
    ) -> None:
        self.delegate = delegate
        self.outcome: JudgeOutcome | None = None
        self.observer = observer
        self.model = model

    def choose(self, **kwargs) -> JudgeOutcome:
        if self.observer is not None:
            self.observer({
                "stage": "juez", "model": self.model,
                "provider": "gemini_policy", "status": "start",
            })
        try:
            self.outcome = self.delegate.choose(**kwargs)
        except BaseException as exc:
            if self.observer is not None:
                self.observer({
                    "stage": "juez", "model": self.model,
                    "provider": "gemini_policy", "status": "error",
                    "error_class": classify_error(exc).category.value,
                    "error_message": type(exc).__name__,
                })
            raise
        if self.observer is not None:
            status = "error" if self.outcome.decision is None else "ok"
            event = {
                "stage": "juez", "model": self.model,
                "provider": "gemini_policy", "status": status,
            }
            if status == "error":
                original = self.outcome.original_exception
                event["error_class"] = classify_error(
                    original or SemanticModelError()
                ).category.value
                event["error_message"] = type(original).__name__ if original else "judge_unresolved"
            self.observer(event)
        return self.outcome


def _role_output(value: Any, role: str) -> Mapping[str, Any]:
    if isinstance(value, SnapshotInvalidOutput):
        raise ModelResponseValidationError(
            f"{role}_invalid_output:{value.code}"
        ) from value.original_exception
    output = getattr(value, "output", value)
    if not isinstance(output, Mapping) or not output:
        raise ModelResponseValidationError(f"{role}_empty_or_non_object")
    return dict(output)


def _stable_exception_text(exc: BaseException) -> str:
    """Return ``Class: message`` without repr-only or multiline variability."""

    message = " ".join(str(exc).split())
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    status = (
        f"; status_code={status_code}"
        if isinstance(status_code, int) and not isinstance(status_code, bool)
        else ""
    )
    return (f"{name}: {message}" if message else name) + status


def _exception_chain(exc: BaseException) -> tuple[str, ...]:
    """Serialize an exception and its causal chain once, outermost first."""

    errors: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        errors.append(_stable_exception_text(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return tuple(errors)


def _citation_layer(raw: Mapping[str, Any]) -> CitationLayer:
    return CitationLayer(
        source_id=raw["source_id"],
        start=raw["start"],
        end=raw["end"],
        quote=raw["quote"],
    )


def _proposal_layer(raw: Mapping[str, Any]) -> ProposalLayer:
    checked = DecisionProposal.from_mapping(raw)
    return ProposalLayer(
        decision=checked.decision,
        bucket=checked.bucket,
        candidate_id=checked.candidate_id,
        resource_url=checked.resource_url,
        citations=tuple(_citation_layer(item) for item in checked.citations),
        reason=checked.reason,
    )


class _FreeTelemetryTransport:
    """Contabiliza en ModelPolicyTelemetry cada request free REAL (G4: la
    telemetria debe reflejar requests reales; el canario r1/r2 marcaba
    free_calls=0 con llamadas reales). No altera politica alguna: el camino
    free no construye transporte pago ni fallback; esto es solo auditoria."""

    def __init__(self, inner, telemetry: ModelPolicyTelemetry) -> None:
        self._inner = inner
        self._telemetry = telemetry

    def generate(self, model, contents, config):
        try:
            response = self._inner.generate(model, contents, config)
        except Exception as exc:
            self._telemetry.record_call(
                "gemini_free", None, model=model, status="error",
                error_class=type(exc).__name__,
            )
            raise
        self._telemetry.record_call("gemini_free", response, model=model)
        return response


# El camino free-only (from_free_environment) no tiene fallback pago -- a
# diferencia de PolicyTransport (live_model_policy.py), que solo corre en
# from_model_policy_environment -- asi que un 429 transitorio mataba la
# unidad entera sin reintento (caso real: Canoas/CP B, 'ClientError: 429
# RESOURCE_EXHAUSTED ... Please retry in 20.37s'). classify_error()/_retry_
# after() solo leen atributos estructurados (.response.status_code,
# .response.headers['Retry-After']) que el ClientError real del SDK no
# expone; por eso el mensaje se parsea con una regex local como respaldo.
_QUOTA_429_MESSAGE_RE = re.compile(r"\b429\b|RESOURCE_EXHAUSTED", re.IGNORECASE)
_RETRY_AFTER_MESSAGE_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)
_FREE_QUOTA_RETRY_DEFAULT_SECONDS = 30.0
_FREE_QUOTA_RETRY_MAX_SLEEP_SECONDS = 65.0


def _is_quota_429(exc: BaseException) -> bool:
    if classify_error(exc).category is ErrorCategory.QUOTA_429:
        return True
    return bool(_QUOTA_429_MESSAGE_RE.search(str(exc)))


def _extract_retry_after_seconds(exc: BaseException) -> float:
    classified_retry_after = classify_error(exc).retry_after
    if classified_retry_after is not None:
        return classified_retry_after
    match = _RETRY_AFTER_MESSAGE_RE.search(str(exc))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return _FREE_QUOTA_RETRY_DEFAULT_SECONDS


class _FreeQuotaRetryTransport:
    """Reintento local acotado ante 429/RESOURCE_EXHAUSTED en el camino free-only.

    Envuelve un transporte ya telemetrado (``_inner`` es un
    ``_FreeTelemetryTransport``), NO al reves: cada llamada real -- incluidos
    los reintentos -- pasa por ``_inner.generate`` y por lo tanto queda
    contabilizada en ``ModelPolicyTelemetry`` (G4: la telemetria debe
    reflejar requests reales). Si el orden fuera inverso (telemetria por
    fuera), solo se contaria la llamada externa y los reintentos internos
    quedarian invisibles.
    """

    def __init__(
        self,
        inner,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
        max_sleep_seconds: float = _FREE_QUOTA_RETRY_MAX_SLEEP_SECONDS,
    ) -> None:
        self._inner = inner
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._max_sleep_seconds = max_sleep_seconds

    def generate(self, model, contents, config):
        attempt = 0
        while True:
            try:
                return self._inner.generate(model, contents, config)
            except Exception as exc:
                if attempt >= self._max_retries or not _is_quota_429(exc):
                    raise
                retry_after = _extract_retry_after_seconds(exc)
                self._sleeper(min(retry_after + 1.0, self._max_sleep_seconds))
                attempt += 1


class LiveABCAdapter:
    """Real/live implementation of the cassette producer's ``ABCProvider``."""

    def __init__(
        self,
        *,
        fetcher: OrionFetcher,
        target_urls: Mapping[tuple[str, str], str],
        certifier: CertifierRole,
        prosecutor: ProsecutorRole,
        judge: JudgeRole,
        timeout_seconds: float = 15.0,
        observer: Callable[[Mapping[str, Any]], None] | None = None,
        stage_transports: Mapping[str, PolicyTransport] | None = None,
        telemetry: ModelPolicyTelemetry | None = None,
        artifact_writer: StageArtifactWriter | None = None,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise LiveABCConfigurationError("timeout_seconds_must_be_positive")
        self.fetcher = fetcher
        self.target_urls = dict(target_urls)
        self.certifier = certifier
        self.prosecutor = prosecutor
        self.judge = judge
        self.timeout_seconds = float(timeout_seconds)
        self.observer = observer
        self.stage_transports = dict(stage_transports or {})
        self.telemetry = telemetry
        self.artifact_writer = artifact_writer
        self._artifact_attempt = 1
        self._outcomes: dict[tuple[str, str], LiveABCOutcome] = {}

    def set_observer(
        self, observer: Callable[[Mapping[str, Any]], None] | None
    ) -> None:
        self.observer = observer
        if self.telemetry is not None:
            self.telemetry.set_observer(observer)

    def set_artifact_writer(self, writer: StageArtifactWriter | None) -> None:
        self.artifact_writer = writer

    def set_attempt(self, attempt: int) -> None:
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ValueError("attempt must be a positive integer")
        self._artifact_attempt = attempt

    def reset_unit(self, municipio: str, bucket: str) -> None:
        """Forget one terminal outcome before a full A->B->gate retry."""

        self._outcomes.pop((municipio, bucket), None)

    def artifact_reference(
        self, municipio: str, bucket: str, attempt: int
    ) -> dict[str, str]:
        if self.artifact_writer is None:
            return {}
        return self.artifact_writer.reference((municipio, bucket), attempt)

    @staticmethod
    def _snapshot_mapping(
        municipio: str, bucket: str, snapshot: EvidenceSnapshot
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "unit": {"municipio": municipio, "bucket": bucket},
            "sources": [
                {
                    "source_id": source.source_id,
                    "url": source.url,
                    "retrieved_at": source.retrieved_at.isoformat(),
                    "content": source.content,
                }
                for source in snapshot.sources
            ],
        }

    def _record_stage(
        self, municipio: str, bucket: str, stage: str, state: str, *,
        snapshot: EvidenceSnapshot | None = None, raw: Any = None,
        raw_exists: bool = False, error: BaseException | None = None,
    ) -> None:
        if self.artifact_writer is None:
            return
        kwargs = {
            "unit": (municipio, bucket), "attempt": self._artifact_attempt,
            "stage": stage, "state": state, "error": error,
        }
        if snapshot is not None:
            kwargs["snapshot"] = self._snapshot_mapping(municipio, bucket, snapshot)
        if raw_exists:
            kwargs["raw"] = raw
        self.artifact_writer.record_stage(**kwargs)

    def _emit(self, **event: Any) -> None:
        if self.observer is not None:
            self.observer(dict(event))

    def _bind_stage(self, stage: str, municipio: str, bucket: str) -> None:
        transport = self.stage_transports.get(stage)
        if transport is not None:
            transport.set_unit(municipio, bucket)

    @classmethod
    def from_free_environment(
        cls,
        *,
        fetcher: OrionFetcher,
        target_urls: Mapping[tuple[str, str], str],
        environ: Mapping[str, str] | None = None,
        limiter=None,
        sdk_client_factory=None,
        timeout_seconds: float = 15.0,
    ) -> "LiveABCAdapter":
        free_key = resolve_free_api_key(environ)
        telemetry = ModelPolicyTelemetry()
        # Orden: retry POR FUERA de la telemetria (ver docstring de
        # _FreeQuotaRetryTransport) para que cada intento real -- incluidos
        # los reintentos ante 429 -- quede contabilizado.
        transport = _FreeQuotaRetryTransport(
            _FreeTelemetryTransport(
                RealGeminiTransport(
                    free_key,
                    client_factory=sdk_client_factory,
                ),
                telemetry,
            ),
        )
        shared_limiter = limiter or get_shared_limiter()
        models = RoleModels()
        certifier = build_certifier_agent(
            transport=transport,
            limiter=shared_limiter,
            models=models,
            invocation_mode="direct",
        )
        prosecutor = build_prosecutor_agent(
            transport=transport,
            limiter=shared_limiter,
            models=models,
            invocation_mode="direct",
        )
        judge_client = build_judge_client(
            transport=transport,
            limiter=shared_limiter,
            models=models,
        )
        judge = build_conflict_judge(client=judge_client)
        return cls(
            fetcher=fetcher,
            target_urls=target_urls,
            certifier=certifier,
            prosecutor=prosecutor,
            judge=judge,
            timeout_seconds=timeout_seconds,
            telemetry=telemetry,
        )

    @classmethod
    def from_model_policy_environment(
        cls,
        *,
        fetcher: OrionFetcher,
        target_urls: Mapping[tuple[str, str], str],
        environ: Mapping[str, str],
        limiter=None,
        sdk_client_factory=None,
        timeout_seconds: float = 30.0,
        gemini_timeout: float = 60.0,
        isolate_model_calls: bool = True,
    ) -> "LiveABCAdapter":
        free_key = environ.get("GEMINI_API_KEY_FREE")
        paid_key = environ.get("GEMINI_API_KEY")
        if not isinstance(free_key, str) or not free_key.strip():
            raise LiveABCConfigurationError("free_model_credential_missing")
        if not isinstance(paid_key, str) or not paid_key.strip():
            raise LiveABCConfigurationError("paid_fallback_credential_missing")
        models = RoleModels()
        telemetry = ModelPolicyTelemetry()
        free_transport = RealGeminiTransport(
            free_key,
            client_factory=sdk_client_factory,
            timeout_seconds=gemini_timeout,
        )
        paid_transport = RealGeminiTransport(
            paid_key,
            client_factory=sdk_client_factory,
            timeout_seconds=gemini_timeout,
        )
        stage_models = {
            "A": models.certifier_model,
            "B": models.prosecutor_model,
            "juez": models.judge_model,
        }
        policies = {
            stage: PolicyTransport(
                free_transport=free_transport,
                paid_transport=paid_transport,
                model=model,
                stage=stage,
                telemetry=telemetry,
                timeout_seconds=gemini_timeout,
                isolate_calls=isolate_model_calls,
            )
            for stage, model in stage_models.items()
        }
        shared_limiter = limiter or get_shared_limiter()
        certifier = build_certifier_agent(
            transport=policies["A"], limiter=shared_limiter, models=models,
            invocation_mode="direct",
        )
        prosecutor = build_prosecutor_agent(
            transport=policies["B"], limiter=shared_limiter, models=models,
            invocation_mode="direct",
        )
        judge_client = build_judge_client(
            transport=policies["juez"], limiter=shared_limiter, models=models
        )
        return cls(
            fetcher=fetcher,
            target_urls=target_urls,
            certifier=certifier,
            prosecutor=prosecutor,
            judge=build_conflict_judge(client=judge_client),
            timeout_seconds=timeout_seconds,
            stage_transports=policies,
            telemetry=telemetry,
        )

    @staticmethod
    def _failure(
        municipio: str,
        bucket: str,
        *,
        kind: LiveCauseKind,
        code: str,
        error: BaseException | None = None,
        phase: str | None = None,
        audit_events: tuple[LiveAuditEvent, ...] = (),
        evidence_snapshot: EvidenceSnapshot | None = None,
    ) -> LiveABCOutcome:
        comments = {
            LiveCauseKind.ACCESS_FAILURE: "no se pudo acceder",
            LiveCauseKind.MODEL_FAILURE: "fallo de Gemini free-only",
            LiveCauseKind.EVIDENCE_FAILURE: "evidencia o cita rechazada",
            LiveCauseKind.DISAGREEMENT_UNRESOLVED: "desacuerdo A/B/C no resuelto",
            LiveCauseKind.CONFIGURATION_FAILURE: "configuracion live invalida",
            LiveCauseKind.INTERNAL_FAILURE: "error interno live inesperado",
        }
        events = audit_events
        if error is not None:
            events += (LiveAuditEvent(phase or "unknown", _exception_chain(error)),)
        revisar_por = {
            LiveCauseKind.ACCESS_FAILURE: "revisar_por_adquisicion",
            LiveCauseKind.EVIDENCE_FAILURE: "revisar_por_adquisicion",
            LiveCauseKind.DISAGREEMENT_UNRESOLVED: "revisar_por_C",
            LiveCauseKind.CONFIGURATION_FAILURE: "revisar_por_gate",
            LiveCauseKind.INTERNAL_FAILURE: "revisar_por_gate",
        }.get(kind, "")
        if kind is LiveCauseKind.MODEL_FAILURE:
            revisar_por = "revisar_por_B" if phase == "B" else "revisar_por_A"
        diagnostic_code = code
        if isinstance(error, LiveFetchError) and error.status_code is not None:
            diagnostic_code = f"{str(error)}:{error.status_code}"
        return LiveABCOutcome(
            municipio=municipio,
            bucket=bucket,
            decision="revisar",
            url="",
            cause=LiveCause(kind, diagnostic_code, comments[kind], revisar_por),
            layer=None,
            evidence_snapshot=evidence_snapshot,
            original_exception=error,
            audit_events=events,
        )

    @staticmethod
    def _snapshots(
        fetched: FetchedEvidence,
    ) -> tuple[EvidenceSnapshot, cascade.EvidenceSnapshot]:
        source = EvidenceSource(
            source_id="main",
            url=fetched.final_url,
            retrieved_at=fetched.retrieved_at,
            content=fetched.content,
        )
        model_snapshot = build_snapshot((source,))
        candidate_snapshot = cascade.EvidenceSnapshot(
            html=fetched.html,
            text=fetched.content,
            title=fetched.title,
            final_url=fetched.final_url,
            requested_url=fetched.requested_url,
            status=fetched.status,
            source="orion_http",
            evidence_state=fetched.evidence_state,
        )
        return model_snapshot, candidate_snapshot

    @staticmethod
    def _candidate(
        *,
        municipio: str,
        bucket: str,
        fetched: FetchedEvidence,
        snapshot: cascade.EvidenceSnapshot,
    ) -> cascade.CandidateRecord:
        # Independencia V1 (12-jul): SOLO evidencia estructural (autoridad,
        # identidad, accesibilidad, challenge). El clasificador semantico V1
        # (verdict via build_candidate_record) ya no corre en el runtime V2:
        # la semantica la adjudican los agentes A/B/C.
        # Provenance de autoridad general: si el fetcher siguio un redirect
        # real (301/302/...) desde un host oficial *.rs.gov.br confirmado
        # hasta otro host (caso Porto Alegre: smap/... -> prefeitura.poa.br),
        # eso es evidencia estructural de origen oficial aunque el destino no
        # sea *.rs.gov.br. Ver scripts/fase2_municipios/v2/authority.py.
        provenance = authority.redirect_provenance(
            fetched.requested_url, fetched.final_url, municipio
        )
        return structural_candidate(
            requested_url=fetched.requested_url,
            source="orion_http",
            tier="live",
            municipio=municipio,
            bucket=bucket,
            evidence=snapshot,
            provenance=provenance,
        )

    @staticmethod
    def _task(
        municipio: str, bucket: str, candidate: cascade.CandidateRecord
    ) -> str:
        return (
            "Validate exactly the frozen candidate for the requested municipality "
            f"and bucket. municipio={municipio!r}; bucket={bucket!r}; "
            f"candidate_id={candidate.candidate_id!r}; no refetch is permitted."
        )

    def request(self, municipio: str, bucket: str) -> LiveABCOutcome:
        unit = (municipio, bucket)
        cached = self._outcomes.get(unit)
        if cached is not None:
            return cached
        if bucket not in VALID_BUCKETS or not municipio:
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.CONFIGURATION_FAILURE,
                code="invalid_target",
            )
            self._outcomes[unit] = outcome
            return outcome
        url = self.target_urls.get(unit)
        if not isinstance(url, str) or not url:
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.CONFIGURATION_FAILURE,
                code="target_url_missing",
            )
            self._outcomes[unit] = outcome
            return outcome
        self._emit(stage="fetch", model="", provider="orion_http", status="start")
        try:
            fetched = self.fetcher.fetch(url, timeout_seconds=self.timeout_seconds)
        except (ExternalAccessBlocked, TimeoutError) as exc:
            self._record_stage(municipio, bucket, "fetch", "request_failed", error=exc)
            classified = classify_error(exc)
            self._emit(
                stage="fetch", model="", provider="orion_http", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.ACCESS_FAILURE,
                code=type(exc).__name__,
                error=exc,
                phase="fetch",
            )
            self._outcomes[unit] = outcome
            return outcome
        except Exception as exc:
            self._record_stage(municipio, bucket, "fetch", "request_failed", error=exc)
            classified = classify_error(exc)
            self._emit(
                stage="fetch", model="", provider="orion_http", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.ACCESS_FAILURE,
                code=type(exc).__name__,
                error=exc,
                phase="fetch",
            )
            self._outcomes[unit] = outcome
            return outcome
        self._emit(stage="fetch", model="", provider="orion_http", status="ok")

        try:
            snapshot, candidate_snapshot = self._snapshots(fetched)
            candidate = self._candidate(
                municipio=municipio,
                bucket=bucket,
                fetched=fetched,
                snapshot=candidate_snapshot,
            )
            source = snapshot.get_source("main")
            if (
                cascade._normalized_candidate_url(candidate.final_url)
                != cascade._normalized_candidate_url(source.url)
            ):
                raise SnapshotError("candidate_url_does_not_match_snapshot_origin")
            fetch_raw = {
                "requested_url": fetched.requested_url,
                "final_url": fetched.final_url,
                "status": fetched.status,
                "title": fetched.title,
                "decode_diagnostics": list(fetched.decode_diagnostics),
                # Hash siempre presente (barato); el blob de bytes crudos solo
                # se adjunta cuando el decode tuvo que apartarse del charset
                # declarado -- ver _decode_diagnostics_show_charset_anomaly.
                "raw_payload_sha256": fetched.raw_payload_sha256,
            }
            if _decode_diagnostics_show_charset_anomaly(fetched.decode_diagnostics):
                fetch_raw["raw_payload_b64_head"] = fetched.raw_payload_head_b64
                fetch_raw["raw_payload_truncated"] = fetched.raw_payload_truncated
            self._record_stage(
                municipio, bucket, "fetch", "raw_received", snapshot=snapshot,
                raw=fetch_raw, raw_exists=True,
            )
        except Exception as exc:
            self._record_stage(
                municipio, bucket, "fetch", "validation_failed", error=exc
            )
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.EVIDENCE_FAILURE,
                code=type(exc).__name__,
                error=exc,
                phase="evidence_snapshot",
            )
            self._outcomes[unit] = outcome
            return outcome

        models = RoleModels()
        self._bind_stage("A", municipio, bucket)
        self._emit(
            stage="A", model=models.certifier_model,
            provider="gemini_policy", status="start",
        )
        try:
            certified_result = self.certifier.certify(
                snapshot=snapshot,
                task=self._task(municipio, bucket, candidate),
            )
        except ValidationError as exc:
            self._record_stage(
                municipio, bucket, "A", "validation_failed",
                snapshot=snapshot, error=exc,
            )
            classified = classify_error(exc)
            self._emit(
                stage="A", model=models.certifier_model,
                provider="gemini_policy", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE, code=type(exc).__name__,
                error=exc, phase="A", evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        except Exception as exc:
            self._record_stage(
                municipio, bucket, "A", "request_failed",
                snapshot=snapshot, error=exc,
            )
            classified = classify_error(exc)
            self._emit(
                stage="A", model=models.certifier_model,
                provider="gemini_policy", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE,
                code=type(exc).__name__, error=exc, phase="A",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome

        certified_raw = getattr(certified_result, "raw", None)
        if isinstance(certified_result, SnapshotInvalidOutput):
            self._record_stage(
                municipio, bucket, "A", "validation_failed", snapshot=snapshot,
                raw=certified_raw, raw_exists=certified_raw is not None,
                error=certified_result.original_exception,
            )
            original = certified_result.original_exception
            self._emit(
                stage="A", model=models.certifier_model,
                provider="gemini_policy", status="error",
                error_class=classify_error(
                    original or SemanticModelError()
                ).category.value,
                error_message=(
                    type(original).__name__ if original else certified_result.code
                ),
            )
        else:
            certified_raw = getattr(certified_result, "output", certified_result)
            self._record_stage(
                municipio, bucket, "A", "raw_received", snapshot=snapshot,
                raw=certified_raw, raw_exists=True,
            )
        try:
            certified = _role_output(certified_result, "certifier")
        except ModelResponseValidationError as exc:
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE,
                code=type(exc).__name__, error=exc, phase="A",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        try:
            proposal_a = ABCOrchestrator._proposal_from_certifier(
                certified, (candidate,)
            )
            proposal_a = ABCOrchestrator._normalize_combined_bucket(
                proposal_a, bucket
            )
            proposal_a_layer = _proposal_layer(proposal_a)
        except ProposalValidationError as exc:
            self._record_stage(
                municipio, bucket, "A", "validation_failed", snapshot=snapshot,
                raw=certified_raw, raw_exists=True, error=exc,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE,
                code=type(exc).__name__, error=exc, phase="A",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        except Exception as exc:
            self._record_stage(
                municipio, bucket, "A", "validation_failed", snapshot=snapshot,
                raw=certified_raw, raw_exists=True, error=exc,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.INTERNAL_FAILURE,
                code=type(exc).__name__, error=exc, phase="abc_internal",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        self._emit(
            stage="A", model=models.certifier_model,
            provider="gemini_policy", status="ok",
        )

        run_b = certified.get("decision") in {
            "indice_oficial", "indice_oficial_combinado", "portal_externo_oficial"
        }
        if run_b:
            self._bind_stage("B", municipio, bucket)
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="start",
            )
        else:
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="skipped",
            )
        try:
            if run_b:
                prosecuted_result = self.prosecutor.audit(
                    snapshot=snapshot,
                    certifier_output=proposal_a,
                )
            else:
                prosecuted_result = {
                    "result": "review",
                    "reason": "skipped_nonaffirmative_A",
                    "citations": [],
                    "accusations": [],
                }
        except ValidationError as exc:
            self._record_stage(
                municipio, bucket, "B", "validation_failed",
                snapshot=snapshot, error=exc,
            )
            classified = classify_error(exc)
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE, code=type(exc).__name__,
                error=exc, phase="B", evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        except Exception as exc:
            self._record_stage(
                municipio, bucket, "B", "request_failed",
                snapshot=snapshot, error=exc,
            )
            classified = classify_error(exc)
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE,
                code=type(exc).__name__, error=exc, phase="B",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome

        prosecuted_raw = getattr(prosecuted_result, "raw", None)
        if isinstance(prosecuted_result, SnapshotInvalidOutput):
            self._record_stage(
                municipio, bucket, "B", "validation_failed", snapshot=snapshot,
                raw=prosecuted_raw, raw_exists=prosecuted_raw is not None,
                error=prosecuted_result.original_exception,
            )
            original = prosecuted_result.original_exception
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="error",
                error_class=classify_error(
                    original or SemanticModelError()
                ).category.value,
                error_message=(
                    type(original).__name__ if original else prosecuted_result.code
                ),
            )
        else:
            prosecuted_raw = getattr(prosecuted_result, "output", prosecuted_result)
            self._record_stage(
                municipio, bucket, "B", "raw_received" if run_b else "skipped", snapshot=snapshot,
                raw=prosecuted_raw, raw_exists=True,
            )
        try:
            prosecuted = _role_output(prosecuted_result, "prosecutor")
        except ModelResponseValidationError as exc:
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE,
                code=type(exc).__name__, error=exc, phase="B",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        try:
            proposal_b = ABCOrchestrator._proposal_from_prosecutor(
                prosecuted, proposal_a
            )
            proposal_b_layer = _proposal_layer(proposal_b)
        except ProposalValidationError as exc:
            self._record_stage(
                municipio, bucket, "B", "validation_failed", snapshot=snapshot,
                raw=prosecuted_raw, raw_exists=True, error=exc,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE,
                code=type(exc).__name__, error=exc, phase="B",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        except Exception as exc:
            self._record_stage(
                municipio, bucket, "B", "validation_failed", snapshot=snapshot,
                raw=prosecuted_raw, raw_exists=True, error=exc,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.INTERNAL_FAILURE,
                code=type(exc).__name__, error=exc, phase="abc_internal",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        if run_b:
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="ok",
            )

        self._bind_stage("juez", municipio, bucket)
        recording_judge = _RecordingJudge(
            self.judge, observer=self.observer, model=models.judge_model
        )
        try:
            result = ABCOrchestrator(judge=recording_judge).resolve(
                snapshot=snapshot,
                candidates=(candidate,),
                proposal_a=proposal_a,
                proposal_b=proposal_b,
                requested_bucket=bucket,
                prosecutor_result=str(prosecuted.get("result", "")),
            )
        except Exception as exc:
            self._record_stage(
                municipio, bucket, "C", "request_failed",
                snapshot=snapshot, error=exc,
            )
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.DISAGREEMENT_UNRESOLVED,
                code=type(exc).__name__,
                error=exc,
                phase="judge",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        judge_outcome = recording_judge.outcome
        if judge_outcome is None:
            self._emit(
                stage="juez", model=models.judge_model,
                provider="gemini_policy", status="skipped",
            )
            judge_response: Mapping[str, Any] = {
                "decision": "revisar",
                "reason": "not_invoked_consensus",
            }
            self._record_stage(
                municipio, bucket, "C", "skipped", snapshot=snapshot,
                raw=judge_response, raw_exists=True,
            )
        elif judge_outcome.decision is None:
            self._record_stage(
                municipio, bucket, "C", "validation_failed", snapshot=snapshot,
                raw={"decision": None, "reason": judge_outcome.reason}, raw_exists=True,
                error=judge_outcome.original_exception,
            )
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.DISAGREEMENT_UNRESOLVED,
                code=judge_outcome.error_code or "judge_unresolved",
                error=judge_outcome.original_exception,
                phase="judge_response",
                evidence_snapshot=snapshot,
            )
            self._outcomes[unit] = outcome
            return outcome
        else:
            judge_response = {
                "decision": judge_outcome.decision,
                "reason": judge_outcome.reason,
            }
            self._record_stage(
                municipio, bucket, "C", "raw_received", snapshot=snapshot,
                raw=judge_response, raw_exists=True,
            )

        selected_proposal = proposal_a_layer
        if judge_outcome is not None and judge_outcome.decision == "aceptar_B":
            selected_proposal = proposal_b_layer
        selected_citations = (
            selected_proposal.citations
            if result.final_decision.status == "confirmado"
            else ()
        )
        layer = ABCLayer(
            evidence=EvidenceLayer(
                snapshot_ref=f"sha256:{snapshot.snapshot_sha256}",
                authority=candidate.authority,
                identity=candidate.identity,
                reason="single_orion_http_snapshot_shared_by_A_B_C",
            ),
            sources=(SourceLayer(
                source_id="main",
                url=source.url,
                retrieved_at=source.retrieved_at.isoformat(),
                content=source.content,
            ),),
            citations=selected_citations,
            candidate=CandidateLayer(
                candidate_id=candidate.candidate_id,
                url=candidate.final_url,
                decision=candidate.decision,
                bucket=candidate.bucket,
                authority=candidate.authority,
                identity=candidate.identity,
                evidence_state=candidate.evidence_state,
                source_kind=candidate.source_kind,
            ),
            proposal_a=proposal_a_layer,
            proposal_b=proposal_b_layer,
            judge_response=judge_response,
        )

        if result.final_decision.status == "confirmado":
            kind = LiveCauseKind.SUCCESS
            comment = "A/B/C y gate determinista confirmaron"
        elif proposal_a_layer is not None and proposal_a_layer.decision == "nao_encontrado":
            # Independencia V1: la ausencia legitima la adjudica el agente A
            # (autoridad semantica), no la etiqueta del clasificador V1.
            kind = LiveCauseKind.LEGITIMATE_ABSENCE
            comment = "acceso exitoso sin evidencia de concurso para el bucket"
        elif result.judge_invoked:
            kind = LiveCauseKind.DISAGREEMENT_UNRESOLVED
            comment = "desacuerdo A/B/C no resuelto"
        else:
            kind = LiveCauseKind.EVIDENCE_FAILURE
            comment = "evidencia o cita rechazada"
        revisar_por = ""
        if result.final_decision.status != "confirmado":
            if result.reason_code == "prosecutor_review":
                revisar_por = "revisar_por_B"
            elif result.judge_invoked or result.reason_code.startswith("judge_"):
                revisar_por = "revisar_por_C"
            else:
                revisar_por = "revisar_por_gate"
        final_decision = result.final_decision.decision
        final_url = result.final_decision.url
        if proposal_a_layer is not None and proposal_a_layer.decision == "nao_encontrado":
            final_decision = "nao_encontrado"
            final_url = ""
            revisar_por = ""
        outcome = LiveABCOutcome(
            municipio=municipio,
            bucket=bucket,
            decision=final_decision,
            url=final_url,
            cause=LiveCause(kind, result.reason_code, comment, revisar_por),
            layer=layer,
            evidence_snapshot=snapshot,
        )
        self._outcomes[unit] = outcome
        return outcome

    def get(self, municipio: str, bucket: str) -> ABCLayer | None:
        """Implement the cassette producer's exact ``ABCProvider`` protocol."""
        return self.request(municipio, bucket).layer


__all__ = [
    "FetchedEvidence",
    "LiveABCAdapter",
    "LiveABCConfigurationError",
    "LiveABCOutcome",
    "LiveAuditEvent",
    "LiveCause",
    "LiveCauseKind",
    "LiveFetchError",
    "ModelResponseValidationError",
    "OrionFetcher",
    "OrionHTTPFetcher",
    "RenderFallbackFetcher",
]
