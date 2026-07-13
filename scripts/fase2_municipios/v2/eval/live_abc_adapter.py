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
from html.parser import HTMLParser
import http.client
import os
import re
import socket
import ssl
import tempfile
import time
import unicodedata
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
import zlib

import certifi
from pydantic import ValidationError

try:
    import brotli as _brotli
except ImportError:  # Optional: do not advertise br when no decoder is installed.
    _brotli = None

# SUB-CAUSA 2 (holdout 12-jul: saovendelino/multi24h). cryptography ya es
# dependencia transitiva real (google-auth, pdfminer.six -- ver pip show), no
# opcional como brotli; se protege igual con try/except para que la ausencia
# nunca tumbe el import del modulo, solo desactive la recuperacion AIA (el
# SSLCertVerificationError original sigue propagando sin mascarar nada).
try:
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives import serialization as _x509_serialization
    from cryptography.x509.oid import (
        AuthorityInformationAccessOID as _AIA_OID,
        ExtensionOID as _EXTENSION_OID,
    )
except ImportError:  # pragma: no cover - always present transitively today.
    _x509 = None
    _x509_serialization = None
    _AIA_OID = None
    _EXTENSION_OID = None

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


@dataclass(frozen=True)
class _NavigationEvidenceSnapshot(EvidenceSnapshot):
    """Evidence snapshot plus optional structural navigation metadata.

    ``navigation_zone_texts`` is deliberately outside the content-addressed
    evidence payload: it annotates where visible strings came from but never
    changes source content, hashes, literal quotes, or character offsets.
    Consumers using the base ``EvidenceSnapshot`` contract remain compatible.
    """

    navigation_zone_texts: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict,
        repr=False,
    )


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

# SUB-CAUSA 2 (holdout 12-jul: doutormauriciocardoso/saodomingosdosul/inhacora/
# estrela). iso-8859-1 y cp1252 son "permisivos": decodifican CUALQUIER byte
# 0x00-0xFF sin excepcion nunca, asi que un decode exitoso con uno de estos NO
# prueba que el charset declarado sea correcto (a diferencia de utf-8, que es
# auto-validante -- una secuencia de bytes al azar casi nunca es utf-8 valido).
# Cuando el header declara uno de estos dos, se lo contrasta ANTES de confiar
# ciegamente: ver la cadena de precedencia documentada en _decode_response_payload.
_PERMISSIVE_SINGLE_BYTE_CHARSETS = frozenset({"iso8859-1", "cp1252"})


def _decode_declared_html_single_byte(
    payload: bytes, canonical_header: str
) -> tuple[str, str]:
    """Decode a declared single-byte HTML charset with the WHATWG alias.

    Browsers interpret the ``iso-8859-1`` label as Windows-1252.  Municipal
    portals commonly rely on that behavior for bytes 0x80-0x9F (for example,
    0x96 is an en dash in cp1252 but a non-printing C1 control in Latin-1).
    Only use the alias when such a byte is actually present, and fall back to
    strict ISO-8859-1 if cp1252 cannot represent an undefined C1 byte.
    """

    if canonical_header == "iso8859-1" and any(
        0x80 <= byte <= 0x9F for byte in payload
    ):
        try:
            return payload.decode("cp1252"), "html_alias:iso8859-1->cp1252"
        except UnicodeDecodeError:
            pass
    return payload.decode(canonical_header), f"header_charset:{canonical_header}"


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

    if canonical_header in _PERMISSIVE_SINGLE_BYTE_CHARSETS:
        # Precedencia (documentada, SUB-CAUSA 2 12-jul) antes de confiar en un
        # header permisivo: (a) el <meta charset> del propio documento, si
        # CONTRADICE al header y decodifica limpio -- el exito silencioso del
        # header no es evidencia a su favor; (b) un decode utf-8 ESTRICTO del
        # body completo -- solo posible si el body es utf-8 genuino, porque un
        # byte latin-1/cp1252 suelto con acento (ej. 0xE3 'ã') casi nunca
        # completa una secuencia utf-8 valida. Si ninguno aplica, cae al
        # header declarado (comportamiento previo -- sigue sirviendo iso8859
        # real, ver test_declared_non_utf8_charset_decodes_strictly).
        meta = _DOCUMENT_CHARSET_RE.search(payload[:8192])
        if meta is not None:
            raw_meta = meta.group(1).decode("ascii")
            document_charset = _canonical_charset(raw_meta)
            if document_charset is None:
                diagnostics.append("invalid_document_charset")
            elif document_charset != canonical_header:
                try:
                    return payload.decode(document_charset), tuple((
                        f"document_charset_overrides_header:{document_charset}",
                        f"header_charset:{canonical_header}",
                        *diagnostics,
                    ))
                except UnicodeDecodeError:
                    diagnostics.append(
                        f"declared_charset_decode_failed:{document_charset}"
                    )
        if _MOJIBAKE_UTF8_FINGERPRINT_RE.search(payload) is not None:
            try:
                return payload.decode("utf-8"), tuple((
                    f"utf8_strict_overrides_header:{canonical_header}", *diagnostics
                ))
            except UnicodeDecodeError:
                pass  # body no es utf-8 genuino: cae al header declarado abajo

    if canonical_header is not None:
        try:
            response_text, decode_source = _decode_declared_html_single_byte(
                payload, canonical_header
            ) if canonical_header in _PERMISSIVE_SINGLE_BYTE_CHARSETS else (
                payload.decode(canonical_header), f"header_charset:{canonical_header}"
            )
            return response_text, tuple((decode_source, *diagnostics))
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


# SUB-CAUSA 1 (holdout 12-jul: canudosdovale, Vercel). Un 200 con HTML real
# puede llegar sin cabecera Content-Type. Decidir SOLO por la cabecera
# (ausente == binario) descarta evidencia buena; cuando la cabecera falta se
# huele el cuerpo en vez de rechazar a ciegas. Cuando la cabecera SI esta
# presente y es claramente no-texto (pdf/image/octet-stream/etc.), el
# rechazo original se mantiene intacto -- este sniff nunca se ejecuta ahi.
_CONTENT_TYPE_SNIFF_BYTES = 2048
_HTML_SNIFF_MARKERS = (b"<html", b"<!doctype", b"<head", b"<title")


def _looks_like_html_or_text(head: bytes) -> bool:
    if not head:
        return False
    if head.lstrip().startswith(b"<"):
        return True
    lowered = head.lower()
    return any(marker in lowered for marker in _HTML_SNIFF_MARKERS)


# SUB-CAUSA 2 (holdout 12-jul: saovendelino.multi24h.com.br). El servidor
# sirve su leaf cert sin el intermedio (GlobalSign GCC R6 AlphaSSL CA 2025):
# SSLCertVerificationError "unable to get local issuer certificate". curl lo
# resuelve porque arma la cadena via AIA (Authority Information Access,
# CA Issuers). Se reproduce esa recuperacion UNA vez: leer el leaf cert con
# un handshake sin verificar (solo para inspeccionarlo, jamas para la
# peticion real), seguir su URI de CA Issuers, descargar el intermedio y
# verificar el reintento contra un bundle temporal certifi+intermedio.
_SSL_INCOMPLETE_CHAIN_MESSAGE = "unable to get local issuer certificate"
_TLS_HANDSHAKE_TIMEOUT_MESSAGE = "handshake operation timed out"


def _is_incomplete_chain_error(exc: ssl.SSLCertVerificationError) -> bool:
    """True solo para la cadena rota (AIA la arregla); False para cualquier
    otro fallo de verificacion (hostname mismatch, cert expirado, cert
    self-signed, etc.) -- esos jamas intentan recuperacion, propagan tal
    cual (ver test de SSL error distinto)."""

    message = (getattr(exc, "verify_message", "") or str(exc)).lower()
    return _SSL_INCOMPLETE_CHAIN_MESSAGE in message


def _is_tls_handshake_timeout(exc: BaseException) -> bool:
    """Recognize only the transient TLS-handshake timeout variant of SSLError."""

    return (
        isinstance(exc, TimeoutError)
        and _TLS_HANDSHAKE_TIMEOUT_MESSAGE in str(exc).lower()
    )


def _fetch_leaf_certificate_der(host: str, port: int, timeout: float) -> bytes:
    """Handshake SIN verificar, solo para leer los bytes del leaf cert.

    verify_mode=CERT_NONE esta deliberadamente confinado a esta lectura: la
    peticion de contenido real jamas pasa por este contexto (ver
    _recover_ssl_context_via_aia, que construye un contexto VERIFICADO nuevo
    para el reintento).
    """

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw_sock:
        with context.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
            der = tls_sock.getpeercert(binary_form=True)
    if not der:
        raise LiveFetchError("ssl_aia_leaf_certificate_unavailable")
    return der


def _aia_ca_issuer_uris(der_cert: bytes) -> tuple[str, ...]:
    """Extrae las URI "CA Issuers" de la extension Authority Information
    Access del leaf cert. Devuelve () si la extension no existe -- el
    llamador trata eso como recuperacion imposible, nunca como error."""

    certificate = _x509.load_der_x509_certificate(der_cert)
    try:
        aia = certificate.extensions.get_extension_for_oid(
            _EXTENSION_OID.AUTHORITY_INFORMATION_ACCESS
        )
    except _x509.ExtensionNotFound:
        return ()
    return tuple(
        description.access_location.value
        for description in aia.value
        if description.access_method == _AIA_OID.CA_ISSUERS
        and isinstance(description.access_location, _x509.UniformResourceIdentifier)
    )


def _download_intermediate_pem(uri: str, timeout: float) -> str:
    """Descarga el certificado intermedio referenciado por la URI CA Issuers
    y lo devuelve en PEM. El body puede llegar en DER (application/pkix-cert,
    lo mas comun) o ya en PEM -- se prueba DER primero y se cae a PEM."""

    parsed = urlsplit(uri)
    if parsed.scheme not in {"http", "https"}:
        raise LiveFetchError("ssl_aia_uri_scheme_unsupported")
    connection_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_cls(parsed.hostname, port=parsed.port, timeout=timeout)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers=_live_request_headers())
        response = connection.getresponse()
        body = response.read()
        if response.status < 200 or response.status >= 400:
            raise LiveFetchError(
                "ssl_aia_issuer_fetch_failed", status_code=response.status
            )
    finally:
        connection.close()
    try:
        certificate = _x509.load_der_x509_certificate(body)
    except ValueError:
        certificate = _x509.load_pem_x509_certificate(body)
    return certificate.public_bytes(
        _x509_serialization.Encoding.PEM
    ).decode("ascii")


def _write_temp_ca_bundle(intermediate_pem: str) -> str:
    """Bundle temporal certifi + intermedio. El SSLContext parsea el archivo
    dentro de create_default_context; el archivo se borra apenas se
    construye el contexto (ver _recover_ssl_context_via_aia), no hace falta
    conservarlo mas alla de esa llamada."""

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, encoding="ascii"
    ) as bundle_file:
        with open(certifi.where(), "r", encoding="ascii") as certifi_bundle:
            bundle_file.write(certifi_bundle.read())
        bundle_file.write("\n")
        bundle_file.write(intermediate_pem)
        return bundle_file.name


def _recover_ssl_context_via_aia(
    host: str | None, port: int, timeout: float
) -> ssl.SSLContext | None:
    """Recuperacion AIA de un solo intento. Devuelve ``None`` ante cualquier
    fallo (cryptography ausente, extension AIA ausente, descarga fallida,
    parseo fallido) para que el llamador propague el SSLCertVerificationError
    ORIGINAL sin mascarar -- esta funcion nunca inventa exito."""

    if not host or _x509 is None:
        return None
    try:
        leaf_der = _fetch_leaf_certificate_der(host, port, timeout)
        for uri in _aia_ca_issuer_uris(leaf_der):
            try:
                intermediate_pem = _download_intermediate_pem(uri, timeout)
            except Exception:
                continue
            bundle_path = _write_temp_ca_bundle(intermediate_pem)
            try:
                return ssl.create_default_context(cafile=bundle_path)
            finally:
                try:
                    os.unlink(bundle_path)
                except OSError:
                    pass
    except Exception:
        return None
    return None


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
    def _connection(parsed, timeout_seconds: float, *, ssl_context=None):
        host = parsed.hostname
        if not host:
            raise LiveFetchError("fetch_host_missing")
        port = parsed.port
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                host,
                port=port,
                timeout=timeout_seconds,
                context=ssl_context,
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
        ssl_aia_recovered = False
        for redirect_count in range(self._max_redirects + 1):
            parsed = urlsplit(current_url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            # SUB-CAUSA 2: contexto recuperado via AIA para ESTE host, si
            # hizo falta. None hasta que un SSLCertVerificationError de
            # cadena incompleta dispare la recuperacion (una vez por host,
            # nunca en tests que no la ejercitan -- ver comentario en
            # _connection sobre no pasar el kwarg salvo que sea necesario).
            ssl_context_override = None
            handshake_timeout_retried = False
            transport_retry_used = False
            while True:
                connection = (
                    self._connection(parsed, connect_timeout, ssl_context=ssl_context_override)
                    if ssl_context_override is not None
                    else self._connection(parsed, connect_timeout)
                )
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
                except ssl.SSLCertVerificationError as exc:
                    if ssl_context_override is not None or not _is_incomplete_chain_error(exc):
                        raise
                    recovered_context = _recover_ssl_context_via_aia(
                        parsed.hostname, parsed.port or 443, connect_timeout
                    )
                    if recovered_context is None:
                        raise
                    ssl_context_override = recovered_context
                    ssl_aia_recovered = True
                    continue
                except TimeoutError as exc:
                    if parsed.scheme == "https" and _is_tls_handshake_timeout(exc):
                        if handshake_timeout_retried:
                            raise
                        handshake_timeout_retried = True
                        time.sleep(3.0)
                        continue
                    if transport_retry_used:
                        raise
                    transport_retry_used = True
                    continue
                except OSError:
                    if transport_retry_used:
                        raise
                    transport_retry_used = True
                    continue
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
        header_says_text = "text/html" in content_type or "text/plain" in content_type
        if not header_says_text and content_type.strip():
            # Cabecera PRESENTE y claramente no-texto: rechazo original
            # intacto, sin gastar tiempo en decompress/sniff.
            raise LiveFetchError("response_not_html_or_text")
        content_encoding = str(response.getheader("content-encoding", ""))
        payload = _decompress_payload(payload, content_encoding)
        content_type_sniffed = False
        if not header_says_text:
            # Cabecera AUSENTE (SUB-CAUSA 1): huele el cuerpo ya
            # descomprimido antes de rechazar.
            if not _looks_like_html_or_text(payload[:_CONTENT_TYPE_SNIFF_BYTES]):
                raise LiveFetchError("response_not_html_or_text")
            content_type_sniffed = True
        raw_payload_sha256 = hashlib.sha256(payload).hexdigest()
        declared_charset = response.headers.get_content_charset()
        response_text, decode_diagnostics = _decode_response_payload(
            payload, declared_charset
        )
        if content_type_sniffed:
            decode_diagnostics = ("content_type_sniffed=true", *decode_diagnostics)
        if ssl_aia_recovered:
            decode_diagnostics = ("ssl_aia_recovered=true", *decode_diagnostics)
        # SUB-CAUSA 2c (12-jul): normalizar a NFC de una sola vez, en el unico
        # choke point de decode, para que el snapshot completo (content/html/
        # title, todos derivados de response_text) sea NFC canonico. Con esto
        # la comparacion de citas en snapshot.py solo necesita normalizar el
        # quote foraneo (posible NFD), nunca el texto ya-NFC del snapshot.
        response_text = unicodedata.normalize("NFC", response_text)
        raw_payload_head_b64 = ""
        raw_payload_truncated = False
        if _decode_diagnostics_show_charset_anomaly(decode_diagnostics):
            head = payload[:_RAW_PAYLOAD_HEAD_BYTES]
            raw_payload_head_b64 = base64.b64encode(head).decode("ascii")
            raw_payload_truncated = len(payload) > _RAW_PAYLOAD_HEAD_BYTES
        final_url = current_url
        # content_type_sniffed: cascade._page_from_html tiene su PROPIO gate
        # "text/html" in content_type que no podemos tocar (prohibido editar
        # cascade_municipios.py). Sin esto, un content_type="" real haria que
        # _page_from_html devuelva error="not_html" y page.text/page.title
        # queden vacios pese a que ya decidimos aceptar el body. Reusa el
        # mismo content-type sintetico que RenderFallbackFetcher usa para el
        # mismo problema (ningun header HTTP real en ese punto tampoco).
        page_content_type = (
            _RENDER_REVALIDATION_CONTENT_TYPE if content_type_sniffed else content_type
        )
        page = cascade._page_from_html(
            final_url,
            status,
            page_content_type,
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


# Nav-only shell OBJETIVO (QA 12-jul, palanca render_interactivo): atende.net
# serves a mega-menu (200-450KB of HTML, THOUSANDS of chars of visible text --
# the mega-menu itself) as static HTML, and injects the real editais listing
# by JS/AJAX after boot. _is_thin_shell never fires there because the served
# text is far above _THIN_SHELL_MAX_TEXT_CHARS; it just is not the listing.
# Two content-neutral signals below (no per-municipality hardcode), verified
# live against lagoabonitadosul/camponovo/estrela (holdout 12-jul):
#
#   * atende_shell -- host is on the atende.net delegated-portal platform and
#     the item vocabulary never appears anywhere in the served text.
#   * nav_heavy -- platform-agnostic: the item vocabulary is either absent
#     everywhere, or present only inside a persistent-navigation container
#     (<nav>/<header>/<footer>/<aside>, or a menu/nav/submenu/breadcrumb/
#     sidebar/megamenu class or id
#     -- the same convention atende.net's own "menu-item"/"menu_central"
#     follows, not a hardcode for that one platform).
#
# A page whose real listing lives in the body always keeps its item markers
# outside those containers, so neither signal fires on an already-working
# index page (verified live against chiapetta.rs.gov.br and crissiumal.rs.gov.br).
_BUCKET_KEYWORD_RE = re.compile(
    r"\b(?:concursos?(?:\s+p[u\u00fa]blicos?)?"
    r"|processos?\s+seletivos?(?:\s+simplificados?)?"
    r"|pss|sele[c\u00e7][a\u00e3]o\s+p[u\u00fa]blica)\b",
    re.IGNORECASE,
)
_ITEM_MARKER_RE = re.compile(
    r"\b(?:edital|concursos?(?:\s+p[u\u00fa]blicos?)?"
    r"|processos?\s+seletivos?(?:\s+simplificados?)?|pss"
    r"|sele[c\u00e7][a\u00e3]o\s+p[u\u00fa]blica)\b"
    r"(?:(?!\blei\b)[^\n]){0,80}?"
    r"(?:n(?:\u00c2)?(?:[\u00ba\u00b0o.]|ro\.?)?\s*\d{1,4}\b"
    r"|(?<![\d.])\d{1,4}\s*/\s*\d{4}\b"
    r"|(?<!\d)\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4}\b)",
    re.IGNORECASE,
)

_FORM_OR_SELECT_RE = re.compile(r"<(?:form|select)\b", re.IGNORECASE)
_SNAPSHOT_MIN_TEXT_CHARS = 50
_SNAPSHOT_MIN_HTML_BYTES = 5 * 1024
_WAIT_PLACEHOLDER_RE = re.compile(r"por\s+favor,?\s*aguarde", re.IGNORECASE)

_ATENDE_HOST_SUFFIX = ".atende.net"

_NAV_LANDMARK_TAGS = {"nav", "header", "footer", "aside"}
_SKIP_CONTENT_TAGS = {"script", "style"}
_NAV_HINT_RE = re.compile(r"menu|nav|sidebar|megamenu|breadcrumb", re.IGNORECASE)

# Cap on how much HTML the nav-aware extractor walks: mirrors the same
# safety valve _page_from_html applies to its SPA-marker scan, so a
# pathologically large page cannot make the trigger check itself expensive.
_NAV_EXTRACT_HTML_CHARS = 400_000


def _is_atende_host(url: str) -> bool:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except Exception:
        return False
    return host == "atende.net" or host.endswith(_ATENDE_HOST_SUFFIX)


class _NavAwareTextExtractor(HTMLParser):
    """Visible text like ``cascade.extract_text``, but excludes anything
    inside a persistent-navigation container (landmark tag or menu/nav-ish
    class/id) so the render trigger can tell "the only mention of an item
    is a mega-menu link" apart from a real listing in the page body.

    Best-effort on malformed HTML: an unmatched end tag is tolerated (the
    tag stack is unwound up to the first match, like a browser would), and
    any parse failure yields empty text -- which only makes the trigger
    over-fire (safe: ``RenderFallbackFetcher`` never regresses evidence),
    never under-fire and hide a real listing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._navigation_depth = 0
        self._content_skip_depth = 0
        self._stack: list[tuple[str, bool, bool, bool]] = []
        self._chunks: list[str] = []
        self._navigation_zones: list[list[str]] = []
        self._active_navigation_zone: list[str] | None = None

    def _is_navigation(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _NAV_LANDMARK_TAGS:
            return True
        for name, value in attrs:
            if name in ("class", "id") and value and _NAV_HINT_RE.search(value):
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        navigation = self._is_navigation(tag, attrs)
        content_skip = tag in _SKIP_CONTENT_TAGS
        starts_zone = navigation and self._navigation_depth == 0
        if starts_zone:
            self._active_navigation_zone = []
            self._navigation_zones.append(self._active_navigation_zone)
        if navigation:
            self._navigation_depth += 1
        if content_skip:
            self._content_skip_depth += 1
        self._stack.append((tag, navigation, content_skip, starts_zone))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                popped = self._stack[i:]
                del self._stack[i:]
                for _, navigation, content_skip, starts_zone in reversed(popped):
                    if content_skip and self._content_skip_depth > 0:
                        self._content_skip_depth -= 1
                    if navigation and self._navigation_depth > 0:
                        self._navigation_depth -= 1
                    if starts_zone:
                        self._active_navigation_zone = None
                break

    def handle_data(self, data: str) -> None:
        if self._content_skip_depth > 0:
            return
        if self._navigation_depth == 0:
            self._chunks.append(data)
        elif self._active_navigation_zone is not None:
            self._active_navigation_zone.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()

    def navigation_texts(self) -> tuple[str, ...]:
        return tuple(
            text
            for chunks in self._navigation_zones
            if (text := re.sub(r"\s+", " ", " ".join(chunks)).strip())
        )


def _text_outside_nav(html: str) -> str:
    parser = _NavAwareTextExtractor()
    try:
        parser.feed((html or "")[:_NAV_EXTRACT_HTML_CHARS])
    except Exception:
        return ""
    return parser.text()


def _navigation_zone_texts(html: str) -> tuple[str, ...]:
    """Extract visible texts of structural navigation zones.

    These strings are snapshot metadata only: the snapshot's evidence content
    remains ``page.text`` exactly as before, so existing literal citations and
    character offsets are unchanged. Malformed/oversized HTML fails open to no
    metadata; the certifier then retains its backward-compatible checks.
    """
    parser = _NavAwareTextExtractor()
    try:
        parser.feed((html or "")[:_NAV_EXTRACT_HTML_CHARS])
    except Exception:
        return ()
    return parser.navigation_texts()


def _nav_shell_render_trigger(page: "cascade.Page") -> str:
    """Return ``"atende_shell"``, ``"nav_heavy"``, or ``""`` (no render
    needed) -- see the module comment above for the two signals."""
    if not page.ok:
        return ""
    text = page.text or ""
    has_markers_anywhere = bool(_ITEM_MARKER_RE.search(text))
    if not has_markers_anywhere and not text.strip():
        return ""  # nothing to strip; empty page is _is_thin_shell's job
    body_text = _text_outside_nav(page.html or "")
    if _ITEM_MARKER_RE.search(body_text):
        return ""  # real listing content already present -- never render
    if _is_atende_host(page.url) or _is_atende_host(page.requested_url):
        return "atende_shell"
    if has_markers_anywhere:
        return "nav_heavy"  # markers exist, but only inside nav containers
    if len(text.strip()) >= _THIN_SHELL_MAX_TEXT_CHARS and len(body_text) < _THIN_SHELL_MAX_TEXT_CHARS:
        # Substantial served text overall, yet almost none of it survives
        # stripping nav containers: the page is nav, not content. (Pages
        # thin overall are already _is_thin_shell's responsibility -- this
        # arm requires the raw text to clear that bar first, so the two
        # never double-tag the same page.)
        return "nav_heavy"
    return ""


def _residual_render_trigger(page: "cascade.Page") -> str:
    """Detect generic residual shells missed by SPA/nav heuristics.

    ``snapshot_minimo`` covers tiny successful HTML responses with virtually
    no extracted text. ``form_sin_items`` covers dynamic indexes whose static
    response exposes only a bucket selector/form and relies on XHR for rows.
    Neither signal claims evidence: it only authorizes one render attempt.
    """
    if not page.ok:
        return ""
    text = (page.text or "").strip()
    html = page.html or ""
    if (
        _FORM_OR_SELECT_RE.search(html)
        and _BUCKET_KEYWORD_RE.search(text)
        and not _ITEM_MARKER_RE.search(text)
    ):
        return "form_sin_items"
    if (
        len(text) < _SNAPSHOT_MIN_TEXT_CHARS
        and len(html.encode("utf-8")) < _SNAPSHOT_MIN_HTML_BYTES
    ):
        return "snapshot_minimo"
    return ""


def _poll_rendered_body(
    browser_page: Any, *, attempts: int = 24, interval_ms: int = 500
) -> str:
    """Poll until an item loads or an observed wait placeholder clears.

    Pages without a loading placeholder must keep polling for XHR rows; the
    absence of ``aguarde`` on the first sample is not itself a completion
    signal. The caller still validates the final snapshot fail-closed.
    """
    body_text = ""
    saw_wait_placeholder = False
    for _ in range(attempts):
        body_text = browser_page.locator("body").inner_text()
        stripped = body_text.strip()
        has_wait_placeholder = bool(_WAIT_PLACEHOLDER_RE.search(stripped))
        if _ITEM_MARKER_RE.search(stripped):
            break
        if saw_wait_placeholder and not has_wait_placeholder:
            break
        saw_wait_placeholder = saw_wait_placeholder or has_wait_placeholder
        browser_page.wait_for_timeout(interval_ms)
    return body_text


def render_page_networkidle(url: str):
    """Render-once V2: como ``cascade.render_page_sync`` pero esperando a que
    el SPA cargue datos reales.

    El render de cascade espera 2000ms fijos tras domcontentloaded; los shells
    de atende.net terminan con title correcto y body VACIO (verificado en vivo
    12-jul: Gramado 0 chars). Aqui: networkidle acotado + sondeo del texto del
    body hasta ~12s. Reutiliza el browser singleton y el perfil pt-BR de
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
        # ~12s poll: wait for an item-positive row loaded by XHR, or for an
        # actually observed "Por favor, aguarde..." placeholder to clear.
        # A long selector-only body without the placeholder is not complete.
        body_text = _poll_rendered_body(browser_page)
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
        render_trigger = (
            _residual_render_trigger(page) or _nav_shell_render_trigger(page)
        )
        if not (
            page.is_antibot or page.is_spa or _is_thin_shell(page) or render_trigger
        ):
            return fetched
        if render_trigger:
            fetched = replace(
                fetched,
                decode_diagnostics=(
                    *fetched.decode_diagnostics,
                    f"render_trigger={render_trigger}",
                ),
            )
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
        original_text = (fetched.content or "").strip()
        # El render debe MEJORAR el texto visible: por longitud (regla
        # original), O por contenido -- gana marcadores de item que el shell
        # original no tenia. La segunda regla es necesaria para los shells
        # atende.net: el HTML estatico duplica el mega-menu (mobile+desktop),
        # asi que a veces es MAS LARGO en caracteres que el render que ya
        # cargo el listado real (verificado en vivo 12-jul: lagoabonitadosul,
        # HTTP 5578 chars/0 marcadores vs. render 3388 chars/marcadores
        # reales) -- exigir solo longitud descartaria evidencia util.
        strictly_longer = len(rendered_text) > len(original_text)
        gained_item_markers = (
            bool(_ITEM_MARKER_RE.search(rendered_text))
            and not _ITEM_MARKER_RE.search(original_text)
        )
        if not (strictly_longer or gained_item_markers):
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
                "gemini_free_1", None, model=model, status="error",
                error_class=type(exc).__name__,
            )
            raise
        self._telemetry.record_call("gemini_free_1", response, model=model)
        return response


class _LazyCredentialTransport:
    """Construct a provider only on its first eligible routing attempt."""

    def __init__(
        self, environ: Mapping[str, str], name: str, *, client_factory=None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._environ = environ
        self._name = name
        self._client_factory = client_factory
        self._timeout_seconds = timeout_seconds
        self._transport = None

    def generate(self, model, contents, config):
        if self._transport is None:
            key = self._environ.get(self._name)
            if not isinstance(key, str) or not key.strip():
                raise LiveABCConfigurationError("model_credential_missing")
            self._transport = RealGeminiTransport(
                key,
                client_factory=self._client_factory,
                timeout_seconds=self._timeout_seconds,
            )
        return self._transport.generate(model, contents, config)


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


def _sample_prosecutor(municipio: str, bucket: str, *, seed: int) -> bool:
    """Stable 10% audit sample; this is sampling, never candidate scoring."""

    material = f"{int(seed)}\0{municipio}\0{bucket}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 10 == 0


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
        abc_mode: str = "full",
        seed: int = 0,
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
        if abc_mode not in {"slim", "full"}:
            raise LiveABCConfigurationError("abc_mode_invalid")
        self.abc_mode = abc_mode
        self.seed = int(seed)
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
        abc_mode: str = "full",
        seed: int = 0,
    ) -> "LiveABCAdapter":
        free_key = resolve_free_api_key(environ)
        telemetry = ModelPolicyTelemetry()
        free_1_transport = RealGeminiTransport(
            free_key, client_factory=sdk_client_factory,
        )
        if environ is not None and "GEMINI_API_KEY_FREE_2" in environ:
            free_2_transport = _LazyCredentialTransport(
                environ, "GEMINI_API_KEY_FREE_2", client_factory=sdk_client_factory,
            )
            # Free-only is structural: paid_transport=None makes the paid tier
            # unreachable even after both eligible free-tier failures.
            transport = PolicyTransport(
                free_transport=free_1_transport,
                free_2_transport=free_2_transport,
                paid_transport=None,
                model=RoleModels().certifier_model,
                stage="free_only",
                telemetry=telemetry,
                isolate_calls=False,
            )
        else:
            # Preserve the original public free-only seam when no second free
            # credential is configured: every real request is telemetered and
            # only quota failures receive the bounded local retry.
            transport = _FreeQuotaRetryTransport(
                _FreeTelemetryTransport(free_1_transport, telemetry)
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
            abc_mode=abc_mode,
            seed=seed,
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
        abc_mode: str = "full",
        seed: int = 0,
    ) -> "LiveABCAdapter":
        free_key = environ.get("GEMINI_API_KEY_FREE")
        paid_present = "GEMINI_API_KEY" in environ
        if not isinstance(free_key, str) or not free_key.strip():
            raise LiveABCConfigurationError("free_model_credential_missing")
        if not paid_present:
            raise LiveABCConfigurationError("paid_fallback_credential_missing")
        models = RoleModels()
        telemetry = ModelPolicyTelemetry()
        free_transport = RealGeminiTransport(
            free_key,
            client_factory=sdk_client_factory,
            timeout_seconds=gemini_timeout,
        )
        free_2_transport = None
        if "GEMINI_API_KEY_FREE_2" in environ:
            free_2_transport = _LazyCredentialTransport(
                environ,
                "GEMINI_API_KEY_FREE_2",
                client_factory=sdk_client_factory,
                timeout_seconds=gemini_timeout,
            )
        paid_transport = _LazyCredentialTransport(
            environ,
            "GEMINI_API_KEY",
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
                free_2_transport=free_2_transport,
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
            abc_mode=abc_mode,
            seed=seed,
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
        base_snapshot = build_snapshot((source,))
        model_snapshot = _NavigationEvidenceSnapshot(
            sources=base_snapshot.sources,
            snapshot_sha256=base_snapshot.snapshot_sha256,
            navigation_zone_texts={
                source.source_id: _navigation_zone_texts(fetched.html),
            },
        )
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

        affirmative_a = certified.get("decision") in {
            "indice_oficial", "indice_oficial_combinado", "portal_externo_oficial"
        }
        run_b = affirmative_a and (
            self.abc_mode == "full" or _sample_prosecutor(municipio, bucket, seed=self.seed)
        )
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
            elif affirmative_a:
                prosecuted_result = {
                    "result": "sustain",
                    "reason": "slim_unsampled_sustain",
                    "citations": [],
                    "accusations": [],
                }
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
