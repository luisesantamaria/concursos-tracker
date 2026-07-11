"""V2-local structured schemas for the application-level agent loop.

``PROSECUTOR_OUTPUT_SCHEMA`` is a proposal for Orion derived from the canonical
false-positive prosecutor skill. It is not promoted to canonical references.
"""

AGENT_STEP_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Fase2AgentStepV2",
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"enum": ["tool", "final"]},
        "tool": {"type": "string", "minLength": 1},
        "args": {"type": "object"},
        "output": {"type": "object"},
    },
}

OFFSET_CITATION_SCHEMA = {
    "type": "object",
    "required": ["source_id", "quote"],
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
        "quote": {"type": "string", "minLength": 1},
    },
}

PROSECUTOR_OUTPUT_SCHEMA_NAME = "Fase2ProsecutorOutputV2ProposalForOrion"
PROSECUTOR_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": PROSECUTOR_OUTPUT_SCHEMA_NAME,
    "description": "V2-local proposal for Orion; derived from fase2-fp-prosecutor.",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "result",
        "reason",
        "accusations",
        "citations",
        "tool_request",
        "failure_mode_proposal",
    ],
    "properties": {
        "result": {"enum": ["sustain", "block", "needs_tool", "review"]},
        "reason": {"type": "string", "minLength": 1},
        "accusations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "outcome", "citations"],
                "properties": {
                    "code": {"type": "string", "minLength": 1},
                    "outcome": {"enum": ["proved", "discarded", "unresolved"]},
                    "citations": {
                        "type": "array",
                        "items": OFFSET_CITATION_SCHEMA,
                    },
                },
            },
        },
        "citations": {
            "type": "array",
            "items": OFFSET_CITATION_SCHEMA,
        },
        "tool_request": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["tool", "args", "question"],
                    "properties": {
                        "tool": {"type": "string", "minLength": 1},
                        "args": {"type": "object"},
                        "question": {"type": "string", "minLength": 1},
                    },
                },
            ],
        },
        "failure_mode_proposal": {"type": ["object", "null"]},
    },
}

JUDGE_OUTPUT_SCHEMA_NAME = "Fase2ConflictJudgeClosedOutputV2"
JUDGE_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": JUDGE_OUTPUT_SCHEMA_NAME,
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason"],
    "properties": {
        "decision": {"enum": ["aceptar_A", "aceptar_B", "revisar"]},
        "reason": {"type": "string", "minLength": 1},
    },
}
