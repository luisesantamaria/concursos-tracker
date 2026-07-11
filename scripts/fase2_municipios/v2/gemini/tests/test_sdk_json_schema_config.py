"""Offline regression for passing full JSON Schema through google-genai."""

from __future__ import annotations

from typing import Any

import pytest
from google.genai import models

from scripts.fase2_municipios.v2.gemini.client import StructuredGeminiClient


pytestmark = pytest.mark.offline


CITATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_id", "start", "end", "quote"],
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
        "quote": {"type": "string", "minLength": 1},
    },
}

PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "citations"],
    "properties": {
        "decision": {"enum": ["indice_oficial", "revisar"]},
        "citations": {"type": "array", "items": CITATION_SCHEMA},
    },
}

ABC_RESPONSE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "OfflineABCContractRegression",
    "type": "object",
    "additionalProperties": False,
    "required": ["proposal_a", "proposal_b", "judge", "note"],
    "properties": {
        "proposal_a": PROPOSAL_SCHEMA,
        "proposal_b": PROPOSAL_SCHEMA,
        "judge": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "reason"],
            "properties": {
                "decision": {"enum": ["aceptar_A", "aceptar_B", "revisar"]},
                "reason": {"type": "string", "minLength": 1},
            },
        },
        "note": {"type": ["string", "null"]},
    },
}


class NeverTransport:
    def generate(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("offline schema test must not invoke transport")


class NeverLimiter:
    def acquire(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("offline schema test must not invoke limiter")


def test_full_json_schema_reaches_sdk_unchanged_via_real_config_helper() -> None:
    client = StructuredGeminiClient(
        transport=NeverTransport(),
        limiter=NeverLimiter(),
        model="offline-schema-model",
        response_schema=ABC_RESPONSE_SCHEMA,
    )

    config = client._build_config(None)
    wire_config = models._GenerateContentConfig_to_mldev(None, config)

    assert "response_schema" not in config
    assert config["response_json_schema"] == ABC_RESPONSE_SCHEMA
    assert wire_config["responseJsonSchema"] == ABC_RESPONSE_SCHEMA

    delivered = wire_config["responseJsonSchema"]
    assert delivered["required"] == ["proposal_a", "proposal_b", "judge", "note"]
    assert delivered["additionalProperties"] is False
    assert delivered["properties"]["note"]["type"] == ["string", "null"]
    for proposal_name in ("proposal_a", "proposal_b"):
        proposal = delivered["properties"][proposal_name]
        assert proposal["required"] == ["decision", "citations"]
        assert proposal["additionalProperties"] is False
        assert proposal["properties"]["decision"]["enum"] == [
            "indice_oficial",
            "revisar",
        ]
        citations = proposal["properties"]["citations"]
        assert citations["type"] == "array"
        citation = citations["items"]
        assert citation["required"] == ["source_id", "start", "end", "quote"]
        assert citation["additionalProperties"] is False
        assert citation["properties"]["source_id"]["type"] == "string"
        assert citation["properties"]["start"]["type"] == "integer"
        assert citation["properties"]["end"]["type"] == "integer"
        assert citation["properties"]["quote"]["type"] == "string"
    judge = delivered["properties"]["judge"]
    assert judge["required"] == ["decision", "reason"]
    assert judge["additionalProperties"] is False
    assert judge["properties"]["decision"]["enum"] == [
        "aceptar_A",
        "aceptar_B",
        "revisar",
    ]
