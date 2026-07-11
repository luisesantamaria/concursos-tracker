"""Opt-in live A/B/C adapter with one fetched snapshot per target.

The public constructor accepts already-built role adapters for offline tests and
for dependency injection.  :meth:`from_free_environment` is the only real-model
factory: it resolves ``GEMINI_API_KEY_FREE`` through the existing client policy,
constructs one transport, and shares the existing project limiter across A/B/C.
No public API in this module accepts an API key, grounding, or native tools.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import gzip
import http.client
import socket
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
import zlib

from pydantic import ValidationError

try:
    import brotli as _brotli
except ImportError:  # Optional: do not advertise br when no decoder is installed.
    _brotli = None

from scripts.fase2_municipios import cascade_municipios as cascade
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
    EvidenceInsufficientError,
    ModelPolicyTelemetry,
    PolicyTransport,
    SemanticModelError,
    classify_error,
)
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

    def __post_init__(self) -> None:
        if not self.requested_url or not self.final_url:
            raise LiveFetchError("fetch_url_missing")
        if self.retrieved_at.tzinfo is None:
            raise LiveFetchError("fetch_timestamp_not_timezone_aware")
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise LiveFetchError("fetch_status_invalid")
        if not isinstance(self.content, str) or not isinstance(self.html, str):
            raise LiveFetchError("fetch_content_invalid")


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
            connection = self._connection(parsed, connect_timeout)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            try:
                connection.request("GET", path, headers=_live_request_headers())
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    sock.settimeout(read_timeout)
                response = connection.getresponse()
                payload = response.read()
            except ExternalAccessBlocked:
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
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            response_text = payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise LiveFetchError("response_decode_failed") from exc
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
    output = getattr(value, "output", value)
    if not isinstance(output, Mapping) or not output:
        raise ModelResponseValidationError(f"{role}_empty_or_non_object")
    return dict(output)


def _stable_exception_text(exc: BaseException) -> str:
    """Return ``Class: message`` without repr-only or multiline variability."""

    message = " ".join(str(exc).split())
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


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
        self._outcomes: dict[tuple[str, str], LiveABCOutcome] = {}

    def set_observer(
        self, observer: Callable[[Mapping[str, Any]], None] | None
    ) -> None:
        self.observer = observer
        if self.telemetry is not None:
            self.telemetry.set_observer(observer)

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
        transport = RealGeminiTransport(
            free_key,
            client_factory=sdk_client_factory,
        )
        shared_limiter = limiter or get_shared_limiter()
        models = RoleModels()
        certifier = build_certifier_agent(
            transport=transport,
            limiter=shared_limiter,
            models=models,
        )
        prosecutor = build_prosecutor_agent(
            transport=transport,
            limiter=shared_limiter,
            models=models,
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
            transport=policies["A"], limiter=shared_limiter, models=models
        )
        prosecutor = build_prosecutor_agent(
            transport=policies["B"], limiter=shared_limiter, models=models
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
        return LiveABCOutcome(
            municipio=municipio,
            bucket=bucket,
            decision="revisar",
            url="",
            cause=LiveCause(kind, code, comments[kind]),
            layer=None,
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
            evidence_state="completa",
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
        short_bucket = "concursos" if bucket == "concurso_publico" else "processos"
        return cascade.build_candidate_record(
            requested_url=fetched.requested_url,
            source="orion_http",
            tier="live",
            municipio=municipio,
            bucket_hint=short_bucket,
            evidence=snapshot,
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
        except Exception as exc:
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
            certified = _role_output(
                self.certifier.certify(
                    snapshot=snapshot,
                    task=self._task(municipio, bucket, candidate),
                ),
                "certifier",
            )
            proposal_a = ABCOrchestrator._proposal_from_certifier(
                certified, (candidate,)
            )
            proposal_a_layer = _proposal_layer(proposal_a)
        except Exception as exc:
            classified = classify_error(exc)
            self._emit(
                stage="A", model=models.certifier_model,
                provider="gemini_policy", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE, code=type(exc).__name__,
                error=exc, phase="A",
            )
            self._outcomes[unit] = outcome
            return outcome
        self._emit(
            stage="A", model=models.certifier_model,
            provider="gemini_policy", status="ok",
        )

        self._bind_stage("B", municipio, bucket)
        self._emit(
            stage="B", model=models.prosecutor_model,
            provider="gemini_policy", status="start",
        )
        try:
            prosecuted = _role_output(
                self.prosecutor.audit(
                    snapshot=snapshot,
                    certifier_output=certified,
                ),
                "prosecutor",
            )
            proposal_b = ABCOrchestrator._proposal_from_prosecutor(
                prosecuted, proposal_a
            )
            proposal_b_layer = _proposal_layer(proposal_b)
        except Exception as exc:
            classified = classify_error(exc)
            self._emit(
                stage="B", model=models.prosecutor_model,
                provider="gemini_policy", status="error",
                error_class=classified.category.value,
                error_message=type(exc).__name__,
            )
            outcome = self._failure(
                municipio, bucket, kind=LiveCauseKind.MODEL_FAILURE, code=type(exc).__name__,
                error=exc, phase="B",
            )
            self._outcomes[unit] = outcome
            return outcome
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
            )
        except Exception as exc:
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.DISAGREEMENT_UNRESOLVED,
                code=type(exc).__name__,
                error=exc,
                phase="judge",
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
        elif judge_outcome.decision is None:
            outcome = self._failure(
                municipio,
                bucket,
                kind=LiveCauseKind.DISAGREEMENT_UNRESOLVED,
                code=judge_outcome.error_code or "judge_unresolved",
                error=judge_outcome.original_exception,
                phase="judge_response",
            )
            self._outcomes[unit] = outcome
            return outcome
        else:
            judge_response = {
                "decision": judge_outcome.decision,
                "reason": judge_outcome.reason,
            }

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
        elif candidate.decision == "nao_encontrado":
            kind = LiveCauseKind.LEGITIMATE_ABSENCE
            comment = "acceso exitoso sin evidencia de concurso para el bucket"
        elif result.judge_invoked:
            kind = LiveCauseKind.DISAGREEMENT_UNRESOLVED
            comment = "desacuerdo A/B/C no resuelto"
        else:
            kind = LiveCauseKind.EVIDENCE_FAILURE
            comment = "evidencia o cita rechazada"
        outcome = LiveABCOutcome(
            municipio=municipio,
            bucket=bucket,
            decision=result.final_decision.decision,
            url=result.final_decision.url,
            cause=LiveCause(kind, result.reason_code, comment),
            layer=layer,
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
]
