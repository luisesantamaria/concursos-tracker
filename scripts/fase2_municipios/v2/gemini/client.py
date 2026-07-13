"""Gemini client with free-only credentials, grounding guards and strict JSON.

All provider I/O is behind an injected :class:`Transport`. Importing this
module performs no SDK import, network access or credential lookup. The real
adapter imports the SDK only when explicitly constructed with a free API key.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from scripts.fase2_municipios.v2.gemini.schema_validation import (
    JsonSchemaValidationError,
    UnsupportedJsonSchemaError,
    validate_json_schema,
)
from scripts.fase2_municipios.v2.loader import load_canonical_resources


LOGGER = logging.getLogger(__name__)
FREE_API_KEY_ENV = "GEMINI_API_KEY_FREE"
SECOND_FREE_API_KEY_ENV = "GEMINI_API_KEY_FREE_2"
FORBIDDEN_ENV_NAMES = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)
FORBIDDEN_CONFIG_KEYS = frozenset({
    "tools",
    "googlesearch",
    "googlesearchretrieval",
    "grounding",
    "retrieval",
})
SAFE_CONFIG_KEYS = frozenset({
    "temperature",
    "max_output_tokens",
    "candidate_count",
    "top_p",
    "top_k",
    "stop_sequences",
    "seed",
})
MAX_RAW_RESPONSE_CHARS = 100_000


class GeminiClientError(RuntimeError):
    """Base class for secret-free, auditable client failures."""


class UnauthorizedCredentialError(GeminiClientError):
    def __init__(self, variable_name: str) -> None:
        self.variable_name = variable_name
        super().__init__(f"credencial no autorizada presente: {variable_name}")


class MissingFreeApiKeyError(GeminiClientError):
    def __init__(self, variable_name: str = FREE_API_KEY_ENV) -> None:
        self.variable_name = variable_name
        super().__init__(f"credencial libre requerida ausente o vacía: {variable_name}")


class GroundingForbiddenError(GeminiClientError):
    def __init__(self, path: str, key: str) -> None:
        self.path = path
        self.key = key
        super().__init__(f"grounding/tool configuration forbidden at {path}: key={key}")


class UnsafeConfigurationError(GeminiClientError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"configuration key is not allowlisted: {key}")


class SchemaValidationError(GeminiClientError):
    def __init__(
        self, *, reason: str, location: str = "$", raw: str | None = None
    ) -> None:
        self.reason = reason
        self.location = location
        self.raw = raw[:MAX_RAW_RESPONSE_CHARS] if isinstance(raw, str) else None
        super().__init__(f"structured response rejected: reason={reason}, location={location}")


class UsageInconsistencyError(GeminiClientError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"token usage rejected: reason={reason}")


class RetryExhaustedError(GeminiClientError):
    def __init__(self, attempts: int, last_error_type: str) -> None:
        self.attempts = attempts
        self.last_error_type = last_error_type
        super().__init__(
            f"transient transport attempts exhausted: attempts={attempts}, "
            f"last_error_type={last_error_type}"
        )


class TransportConfigurationError(GeminiClientError):
    """Real transport could not be configured without implicit credentials."""


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    candidate_tokens: int
    total_tokens: int
    # Campos opcionales de usage_metadata que Gemini 3.x ('thinking') reporta
    # ademas de prompt/candidates/total. Default 0 preserva la compatibilidad
    # posicional con llamadas existentes que solo pasan los primeros 3 campos.
    thoughts_tokens: int = 0
    cached_tokens: int = 0
    tool_use_prompt_tokens: int = 0


@dataclass(frozen=True)
class RawResponse:
    text: str
    usage: TokenUsage | None


class TransientTransportError(GeminiClientError):
    """Retryable transport failure, optionally carrying billed token usage."""

    def __init__(self, *, usage: TokenUsage | None = None, code: str = "transient") -> None:
        self.usage = usage
        self.code = code
        super().__init__(f"transient transport failure: code={code}")


@runtime_checkable
class Transport(Protocol):
    def generate(self, model: str, contents: Any, config: Mapping[str, Any]) -> RawResponse:
        """Generate without resolving or receiving credential environment names."""


class ReservationLike(Protocol):
    def reconcile(self, actual_tokens: int) -> None: ...


class RateLimiterLike(Protocol):
    def acquire(self, estimated_tokens: int) -> ReservationLike: ...


@dataclass(frozen=True)
class RoleModels:
    certifier_model: str = "gemini-3.1-flash-lite"
    prosecutor_model: str = "gemini-3.1-flash-lite"
    judge_model: str = "gemini-3.5-flash"


def assert_no_forbidden_credentials(environ: Mapping[str, str]) -> None:
    """Apply fixed policy precedence without reading forbidden values."""
    for name in FORBIDDEN_ENV_NAMES:
        if name in environ:
            raise UnauthorizedCredentialError(name)


def gentle_free_only_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Preserve runtime networking/locale state while removing paid credentials.

    Forbidden values are never read.  The turnkey CLI uses this instead of an
    empty environment so proxy, resolver, CA bundle, SSL and locale variables
    remain available to HTTP and Gemini transports.
    """

    sanitized: dict[str, str] = {}
    for name in environ:
        normalized = name.upper()
        is_paid = (
            name in FORBIDDEN_ENV_NAMES
            or normalized.startswith("VERTEX")
            or "SERVICE_ACCOUNT" in normalized
        )
        if not is_paid:
            sanitized[name] = environ[name]
    return sanitized


def resolve_free_api_key(environ: Mapping[str, str] | None = None) -> str:
    """Resolve only ``GEMINI_API_KEY_FREE`` after name-only forbidden guards."""
    environment = os.environ if environ is None else environ
    assert_no_forbidden_credentials(environment)
    if FREE_API_KEY_ENV not in environment:
        raise MissingFreeApiKeyError()
    free_key = environment[FREE_API_KEY_ENV]
    if not isinstance(free_key, str) or not free_key.strip():
        raise MissingFreeApiKeyError()
    return free_key


class RealGeminiTransport:
    """Lazy SDK adapter configured only with an explicit free API key.

    Vertex, ADC and gcloud credential discovery have no constructor seam. The
    V2 factory applies credential policy before constructing this adapter.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client_factory=None,
        timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise MissingFreeApiKeyError()
        if timeout_seconds is not None and (
            not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0
        ):
            raise TransportConfigurationError("Gemini timeout must be positive")
        http_options = None
        if client_factory is None:
            try:
                from google import genai  # type: ignore[import-not-found]
                from google.genai import types  # type: ignore[import-not-found]
            except ImportError as exc:
                raise TransportConfigurationError("Gemini SDK is not installed") from exc
            client_factory = genai.Client
            if timeout_seconds is not None:
                # google-genai HttpOptions.timeout is a native transport deadline
                # in milliseconds; it bounds connect and response read in the SDK.
                http_options = types.HttpOptions(timeout=int(timeout_seconds * 1000))
        elif timeout_seconds is not None:
            # Injection seam for offline SDK fakes without importing SDK types.
            http_options = {"timeout": int(timeout_seconds * 1000)}
        kwargs = {"api_key": api_key, "vertexai": False}
        if http_options is not None:
            kwargs["http_options"] = http_options
        self._client = client_factory(**kwargs)

    def generate(self, model: str, contents: Any, config: Mapping[str, Any]) -> RawResponse:
        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=dict(config),
        )
        metadata = getattr(response, "usage_metadata", None)
        usage = None
        if metadata is not None:
            usage = TokenUsage(
                prompt_tokens=getattr(metadata, "prompt_token_count", -1),
                candidate_tokens=getattr(metadata, "candidates_token_count", -1),
                total_tokens=getattr(metadata, "total_token_count", -1),
                thoughts_tokens=_optional_usage_field(metadata, "thoughts_token_count"),
                cached_tokens=_optional_usage_field(metadata, "cached_content_token_count"),
                tool_use_prompt_tokens=_optional_usage_field(
                    metadata, "tool_use_prompt_token_count"
                ),
            )
        return RawResponse(text=getattr(response, "text", ""), usage=usage)


def _optional_usage_field(metadata: Any, name: str) -> Any:
    """Read a non-billed usage_metadata field, tolerant to absence or None.

    ``thoughts_token_count``/``cached_content_token_count``/
    ``tool_use_prompt_token_count`` are newer SDK fields that may be entirely
    absent (older SDK/model) or explicitly ``None``; both collapse to 0 so a
    perfectly valid 'thinking' response is not rejected for missing metadata
    the caller never asked to be mandatory.
    """
    value = getattr(metadata, name, 0)
    return 0 if value is None else value


def _normalized_key(key: Any) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def _guard_grounding(value: Any, *, path: str = "$", seen: set[int] | None = None) -> None:
    seen = set() if seen is None else seen
    if isinstance(value, (str, bytes, bytearray, int, float, bool, type(None))):
        return
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)

    if isinstance(value, Mapping):
        items = value.items()
    elif is_dataclass(value) and not isinstance(value, type):
        items = ((field.name, getattr(value, field.name)) for field in fields(value))
    elif hasattr(value, "__dict__"):
        items = vars(value).items()
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _guard_grounding(item, path=f"{path}[{index}]", seen=seen)
        return
    else:
        return

    for key, item in items:
        normalized = _normalized_key(key)
        if (
            normalized in FORBIDDEN_CONFIG_KEYS
            or "grounding" in normalized
            or "googlesearch" in normalized
            or "retrieval" in normalized
        ):
            raise GroundingForbiddenError(f"{path}.{key}", str(key))
        _guard_grounding(item, path=f"{path}.{key}", seen=seen)


def _validate_usage(usage: TokenUsage | None) -> TokenUsage:
    if usage is None:
        raise UsageInconsistencyError("missing")
    values = (
        usage.prompt_tokens,
        usage.candidate_tokens,
        usage.total_tokens,
        usage.thoughts_tokens,
        usage.cached_tokens,
        usage.tool_use_prompt_tokens,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise UsageInconsistencyError("non_integer")
    if any(value < 0 for value in values):
        raise UsageInconsistencyError("negative")
    # Gemini 3.x 'thinking' reports total = prompt + candidates + thoughts (and
    # may also add cached_content/tool_use_prompt tokens), so strict equality
    # against prompt+candidates alone rejects valid responses (Aratiba/PS live
    # failure, 12-jul). Still fail closed: an undercount below the billed
    # prompt+candidates floor is real corruption, and any excess beyond the
    # sum of every known component is unexplained, not "generously accepted".
    billed_floor = usage.prompt_tokens + usage.candidate_tokens
    known_ceiling = (
        billed_floor
        + usage.thoughts_tokens
        + usage.cached_tokens
        + usage.tool_use_prompt_tokens
    )
    if usage.total_tokens < billed_floor or usage.total_tokens > known_ceiling:
        raise UsageInconsistencyError("total_mismatch")
    return usage


class StructuredGeminiClient:
    """Role-agnostic structured-output client over an injected transport."""

    def __init__(
        self,
        *,
        transport: Transport,
        limiter: RateLimiterLike,
        model: str,
        response_schema: Mapping[str, Any],
        max_attempts: int = 3,
    ) -> None:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.transport = transport
        self.limiter = limiter
        self.model = model
        self.response_schema = response_schema
        self.max_attempts = max_attempts

    def _build_config(self, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = {} if overrides is None else dict(overrides)
        _guard_grounding(supplied)
        for key in supplied:
            if key not in SAFE_CONFIG_KEYS:
                raise UnsafeConfigurationError(key)
        config = {
            "temperature": 0.0,
            "response_mime_type": "application/json",
            "response_json_schema": self.response_schema,
            **supplied,
        }
        _guard_grounding(config)
        return config

    @staticmethod
    def serialize_request_payload(contents: Any, config: Mapping[str, Any]) -> str:
        """Serialize exactly the context-bearing request body, deterministically."""

        return json.dumps(
            {"contents": contents, "config": dict(config)},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def estimate_request_tokens(
        self, contents: Any, config: Mapping[str, Any] | None = None
    ) -> int:
        """Conservative offline estimator over the exact serialized request body."""

        checked_config = self._build_config(None) if config is None else dict(config)
        serialized = self.serialize_request_payload(contents, checked_config)
        return max(1, (len(serialized) + 3) // 4)

    def _attempt(
        self, *, contents: Any, config: Mapping[str, Any], estimated_tokens: int
    ) -> RawResponse:
        reservation = self.limiter.acquire(estimated_tokens)
        response: RawResponse | None = None
        transient: TransientTransportError | None = None
        usage: TokenUsage | None = None
        try:
            try:
                response = self.transport.generate(self.model, contents, config)
                usage = response.usage
            except TransientTransportError as exc:
                transient = exc
                usage = exc.usage
        finally:
            if response is not None or transient is not None:
                checked_usage = _validate_usage(usage)
                reservation.reconcile(checked_usage.total_tokens)
                LOGGER.info(
                    "gemini_usage_reconciled",
                    extra={
                        "gemini_event": "usage_reconciled",
                        "model": self.model,
                        "prompt_tokens": checked_usage.prompt_tokens,
                        "candidate_tokens": checked_usage.candidate_tokens,
                        "total_tokens": checked_usage.total_tokens,
                    },
                )
        if transient is not None:
            raise transient
        if response is None:
            raise GeminiClientError("transport returned no response")
        return response

    def generate_structured(
        self,
        contents: Any,
        *,
        estimated_tokens: int | None = None,
        config_overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        config = self._build_config(config_overrides)
        checked_estimate = (
            self.estimate_request_tokens(contents, config)
            if estimated_tokens is None else estimated_tokens
        )
        response: RawResponse | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._attempt(
                    contents=contents,
                    config=config,
                    estimated_tokens=checked_estimate,
                )
                break
            except TransientTransportError as exc:
                LOGGER.warning(
                    "gemini_transient_retry",
                    extra={
                        "gemini_event": "transient_retry",
                        "model": self.model,
                        "attempt": attempt,
                        "max_attempts": self.max_attempts,
                        "error_type": type(exc).__name__,
                    },
                )
                if attempt == self.max_attempts:
                    raise RetryExhaustedError(attempt, type(exc).__name__) from exc

        assert response is not None
        try:
            parsed = json.loads(response.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SchemaValidationError(
                reason="invalid_json", raw=response.text
            ) from exc
        try:
            validate_json_schema(parsed, self.response_schema)
        except JsonSchemaValidationError as exc:
            raise SchemaValidationError(
                reason="schema_mismatch", location=exc.path, raw=response.text
            ) from exc
        except UnsupportedJsonSchemaError as exc:
            raise SchemaValidationError(
                reason="unsupported_schema", location=exc.path, raw=response.text
            ) from exc
        return parsed


def build_gemini_client(
    *,
    limiter: RateLimiterLike,
    model: str,
    response_schema: Mapping[str, Any],
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
    sdk_client_factory=None,
    max_attempts: int = 3,
) -> StructuredGeminiClient:
    """The single V2 client choke point; environment is resolved per call."""
    selected_transport = transport
    if selected_transport is None:
        api_key = resolve_free_api_key(environ)
        selected_transport = RealGeminiTransport(
            api_key, client_factory=sdk_client_factory
        )
    return StructuredGeminiClient(
        transport=selected_transport,
        limiter=limiter,
        model=model,
        response_schema=response_schema,
        max_attempts=max_attempts,
    )


def build_certifier_client(
    *,
    transport: Transport,
    limiter: RateLimiterLike,
    repo_root: Path | None = None,
    skills_dir: Path | None = None,
    references_dir: Path | None = None,
    models: RoleModels | None = None,
    max_attempts: int = 3,
) -> StructuredGeminiClient:
    """Load canonical ``Fase2CertifierOutput`` and build the generic client."""
    resources = load_canonical_resources(
        repo_root=repo_root,
        skills_dir=skills_dir,
        references_dir=references_dir,
    )
    role_models = models or RoleModels()
    return build_gemini_client(
        transport=transport,
        limiter=limiter,
        model=role_models.certifier_model,
        response_schema=resources.references["schema.json"],
        max_attempts=max_attempts,
    )


def build_judge_client(
    *,
    transport: Transport,
    limiter: RateLimiterLike,
    models: RoleModels | None = None,
    max_attempts: int = 3,
) -> StructuredGeminiClient:
    """Build the closed-output judge client from the shared free role config."""
    from scripts.fase2_municipios.v2.agents.schemas import JUDGE_OUTPUT_SCHEMA

    role_models = models or RoleModels()
    return build_gemini_client(
        transport=transport,
        limiter=limiter,
        model=role_models.judge_model,
        response_schema=JUDGE_OUTPUT_SCHEMA,
        max_attempts=max_attempts,
    )
