"""Small fail-closed JSON Schema validator for V2 structured responses.

The project venv does not include ``jsonschema``. This stdlib-only validator
supports the Draft 2020-12 keywords used by the canonical certifier schema and
rejects unsupported schema keywords instead of silently weakening validation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class JsonSchemaValidationError(ValueError):
    """Instance does not satisfy the supplied schema; never contains its value."""

    def __init__(self, path: str, rule: str) -> None:
        self.path = path
        self.rule = rule
        super().__init__(f"schema validation failed at {path}: {rule}")


class UnsupportedJsonSchemaError(ValueError):
    """Schema uses a keyword this minimal validator cannot enforce safely."""

    def __init__(self, path: str, keyword: str) -> None:
        self.path = path
        self.keyword = keyword
        super().__init__(f"unsupported schema keyword at {path}: {keyword}")


_SUPPORTED_KEYWORDS = {
    "$schema",
    "title",
    "description",
    "type",
    "enum",
    "anyOf",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minLength",
}


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _matches_type(instance: Any, expected: str) -> bool:
    checks = {
        "null": instance is None,
        "object": isinstance(instance, Mapping),
        "array": _is_sequence(instance),
        "string": isinstance(instance, str),
        "boolean": isinstance(instance, bool),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
    }
    if expected not in checks:
        raise UnsupportedJsonSchemaError("$", f"type={expected}")
    return checks[expected]


def validate_json_schema(instance: Any, schema: Mapping[str, Any]) -> None:
    """Validate an instance against the strict supported JSON Schema subset."""
    if not isinstance(schema, Mapping):
        raise UnsupportedJsonSchemaError("$", "schema_not_object")
    _validate(instance, schema, path="$")


def _validate(instance: Any, schema: Mapping[str, Any], *, path: str) -> None:
    for keyword in schema:
        if keyword not in _SUPPORTED_KEYWORDS:
            raise UnsupportedJsonSchemaError(path, str(keyword))

    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not _is_sequence(branches):
            raise UnsupportedJsonSchemaError(path, "anyOf_not_array")
        for branch in branches:
            if not isinstance(branch, Mapping):
                raise UnsupportedJsonSchemaError(path, "anyOf_branch_not_object")
            try:
                _validate(instance, branch, path=path)
            except JsonSchemaValidationError:
                continue
            else:
                break
        else:
            raise JsonSchemaValidationError(path, "anyOf")

    if "type" in schema:
        declared = schema["type"]
        types = declared if _is_sequence(declared) else (declared,)
        if not types or not all(isinstance(item, str) for item in types):
            raise UnsupportedJsonSchemaError(path, "type")
        if not any(_matches_type(instance, item) for item in types):
            raise JsonSchemaValidationError(path, "type")

    if "enum" in schema:
        enum_values = schema["enum"]
        if not _is_sequence(enum_values):
            raise UnsupportedJsonSchemaError(path, "enum_not_array")
        if instance not in enum_values:
            raise JsonSchemaValidationError(path, "enum")

    if isinstance(instance, str) and "minLength" in schema:
        minimum = schema["minLength"]
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            raise UnsupportedJsonSchemaError(path, "minLength")
        if len(instance) < minimum:
            raise JsonSchemaValidationError(path, "minLength")

    if isinstance(instance, Mapping):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise UnsupportedJsonSchemaError(path, "properties_not_object")
        required = schema.get("required", ())
        if not _is_sequence(required) or not all(isinstance(item, str) for item in required):
            raise UnsupportedJsonSchemaError(path, "required_not_array")
        for field in required:
            if field not in instance:
                raise JsonSchemaValidationError(f"{path}.{field}", "required")
        if schema.get("additionalProperties") is False:
            extras = set(instance) - set(properties)
            if extras:
                raise JsonSchemaValidationError(path, "additionalProperties")
        for field, value in instance.items():
            if field not in properties:
                continue
            field_schema = properties[field]
            if not isinstance(field_schema, Mapping):
                raise UnsupportedJsonSchemaError(f"{path}.{field}", "property_schema")
            _validate(value, field_schema, path=f"{path}.{field}")

    if _is_sequence(instance) and "items" in schema:
        item_schema = schema["items"]
        if not isinstance(item_schema, Mapping):
            raise UnsupportedJsonSchemaError(path, "items_not_object")
        for index, item in enumerate(instance):
            _validate(item, item_schema, path=f"{path}[{index}]")
