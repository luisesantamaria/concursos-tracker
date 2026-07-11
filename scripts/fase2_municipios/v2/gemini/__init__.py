"""Free-only Gemini client surface for the parallel Fase 2 V2 pipeline."""

from .client import (
    FREE_API_KEY_ENV,
    GeminiClientError,
    GroundingForbiddenError,
    MissingFreeApiKeyError,
    PaidKeyForbiddenError,
    RawResponse,
    RealGeminiTransport,
    RetryExhaustedError,
    RoleModels,
    SchemaValidationError,
    StructuredGeminiClient,
    TokenUsage,
    TransientTransportError,
    Transport,
    UsageInconsistencyError,
    build_certifier_client,
    resolve_free_api_key,
)
from .schema_validation import validate_json_schema

__all__ = [
    "FREE_API_KEY_ENV",
    "GeminiClientError",
    "GroundingForbiddenError",
    "MissingFreeApiKeyError",
    "PaidKeyForbiddenError",
    "RawResponse",
    "RealGeminiTransport",
    "RetryExhaustedError",
    "RoleModels",
    "SchemaValidationError",
    "StructuredGeminiClient",
    "TokenUsage",
    "TransientTransportError",
    "Transport",
    "UsageInconsistencyError",
    "build_certifier_client",
    "resolve_free_api_key",
    "validate_json_schema",
]
