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
    "additionalProperties": False,
    "required": ["source_id", "quote"],
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
        "quote": {"type": "string", "minLength": 1},
    },
}

PROSECUTOR_ACCUSATION_CODES = (
    "wrong_municipality",
    "unproven_authority",
    "news_article",
    "single_event_detail",
    "year_menu_only",
    "licitacao_or_procurement",
    "cultural_contest",
    "appointment_acts",
    "wrong_bucket",
    "generic_repository",
    "antibot_or_shell",
    "unstable_surface",
    "invented_quote",
    "chrome_contamination",
    "refetch_conflict",
)

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
        "confidence",
        "insufficiency",
        "accusations",
        "citations",
        "tool_request",
        "failure_mode_proposal",
    ],
    "properties": {
        "result": {"enum": ["sustain", "block", "review"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
        "confidence": {"enum": ["high", "medium", "low"]},
        "insufficiency": {"enum": [
            "none", "snapshot_incompleto", "antibot",
            "render_requerido", "senal_contradictoria",
        ]},
        "accusations": {
            "type": "array",
            "minItems": 15,
            "maxItems": 15,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "outcome", "citations"],
                "properties": {
                    "code": {"enum": list(PROSECUTOR_ACCUSATION_CODES)},
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
        "tool_request": {"type": "null"},
        "failure_mode_proposal": {"type": ["object", "null"]},
        # Politica 12-jul (Aratiba/CP): observabilidad de citas OPCIONALES
        # (discarded/unresolved, y top-level fuera de result='block') que no
        # anclaron literal-unicamente y fueron descartadas puntualmente por
        # _prepare_prosecutor_output sin tumbar el veredicto ya derivado.
        # Campo NO requerido: solo aparece cuando hubo al menos un descarte.
        "dropped_optional_citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["location", "source_id", "reason"],
                "properties": {
                    "location": {"type": "string", "minLength": 1},
                    "source_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "quote_preview": {"type": "string"},
                },
            },
        },
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
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
    },
}
