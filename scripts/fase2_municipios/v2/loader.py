"""Offline loader for the canonical Fase 2 skills and references.

This module only reads and validates canonical source material. It performs no
network access and has no dependency on Gemini or credential environment values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


SKILL_NAMES = (
    "fase2-conflict-judge",
    "fase2-fp-prosecutor",
    "fase2-resource-certifier",
)
REFERENCE_NAMES = (
    "casebook.jsonl",
    "failure_modes.json",
    "portal_families.json",
    "schema.json",
)


class ResourceLoaderError(Exception):
    """Base error for canonical resource loading."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class RepositoryRootNotFound(ResourceLoaderError):
    """Raised when the bounded marker search cannot identify the repository."""


class ResourceNotFound(ResourceLoaderError):
    """Raised when a required canonical file or directory is absent."""


class ResourceDecodeError(ResourceLoaderError):
    """Raised when UTF-8 or JSON decoding fails."""


class ResourceValidationError(ResourceLoaderError):
    """Raised when a decoded resource violates its minimal contract."""


@dataclass(frozen=True)
class SkillDocument:
    name: str
    path: Path
    content: str


@dataclass(frozen=True)
class CanonicalResources:
    repo_root: Path
    skills: Mapping[str, SkillDocument]
    references: Mapping[str, Any]


def _has_repo_markers(candidate: Path) -> bool:
    return (
        (candidate / "CLAUDE.md").is_file()
        and (candidate / "scripts" / "fase2_municipios").is_dir()
        and (candidate / "skills").is_dir()
    )


def find_repo_root(start: Path | None = None, *, max_parents: int = 8) -> Path:
    """Find the repository using three markers and a bounded parent search."""
    if max_parents < 0:
        raise ValueError("max_parents must be non-negative")
    origin = Path(start) if start is not None else Path(__file__)
    origin = origin.resolve()
    current = origin.parent if origin.is_file() else origin
    candidates = (current, *current.parents)
    for candidate in candidates[: max_parents + 1]:
        if _has_repo_markers(candidate):
            return candidate
    raise RepositoryRootNotFound(
        origin,
        f"repository root not found within {max_parents} parent levels; "
        "required markers are CLAUDE.md, scripts/fase2_municipios/, and skills/",
    )


def _require_directory(path: Path) -> Path:
    if not path.is_dir():
        raise ResourceNotFound(path, "required directory not found")
    return path


def _read_utf8(path: Path) -> str:
    if not path.is_file():
        raise ResourceNotFound(path, "required file not found")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ResourceDecodeError(path, "file is not valid UTF-8") from exc
    except OSError as exc:
        raise ResourceLoaderError(path, f"could not read file ({type(exc).__name__})") from exc


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_fields(path: Path, obj: Mapping[str, Any], fields: Mapping[str, type]) -> None:
    for field_name, expected_type in fields.items():
        if field_name not in obj:
            raise ResourceValidationError(path, f"missing required field {field_name!r}")
        value = obj[field_name]
        valid = _is_int(value) if expected_type is int else isinstance(value, expected_type)
        if not valid:
            raise ResourceValidationError(
                path,
                f"field {field_name!r} must be {expected_type.__name__}",
            )


def _parse_json(path: Path) -> Any:
    try:
        return json.loads(_read_utf8(path))
    except json.JSONDecodeError as exc:
        raise ResourceDecodeError(
            path, f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc


def _validate_schema(path: Path, obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ResourceValidationError(path, "top-level value must be object")
    _require_fields(
        path,
        obj,
        {"$schema": str, "title": str, "type": str, "required": list, "properties": dict},
    )
    if obj["type"] != "object":
        raise ResourceValidationError(path, "field 'type' must equal 'object'")
    if not all(isinstance(item, str) for item in obj["required"]):
        raise ResourceValidationError(path, "field 'required' must contain only strings")
    return obj


def _validate_named_list(
    path: Path, obj: Any, *, list_field: str, required_item_fields: Mapping[str, type]
) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ResourceValidationError(path, "top-level value must be object")
    _require_fields(path, obj, {"version": int, list_field: list})
    for index, item in enumerate(obj[list_field]):
        if not isinstance(item, dict):
            raise ResourceValidationError(
                path, f"{list_field}[{index}] must be object"
            )
        _require_fields(path, item, required_item_fields)
    return obj


def _validate_string_list(path: Path, field_name: str, value: Any, *, line: int) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ResourceValidationError(
            path, f"line {line}: field {field_name!r} must be list[str]"
        )


def _parse_casebook(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    text = _read_utf8(path)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ResourceValidationError(path, f"line {line_number}: blank JSONL record")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResourceDecodeError(
                path,
                f"invalid JSONL at line {line_number}, column {exc.colno}",
            ) from exc
        if not isinstance(row, dict):
            raise ResourceValidationError(path, f"line {line_number}: record must be object")
        string_fields = {
            "case_id": str,
            "municipio": str,
            "family": str,
            "expected": str,
            "bucket": str,
            "lesson": str,
        }
        try:
            _require_fields(path, row, string_fields)
        except ResourceValidationError as exc:
            raise ResourceValidationError(
                path, f"line {line_number}: {str(exc).rsplit(': ', 1)[0]}"
            ) from exc
        if "facts" not in row:
            raise ResourceValidationError(path, f"line {line_number}: missing required field 'facts'")
        _validate_string_list(path, "facts", row["facts"], line=line_number)
        rows.append(row)
    if not rows:
        raise ResourceValidationError(path, "JSONL must contain at least one record")
    return rows


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(value[key]) for key in sorted(value)})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _load_skills(skills_dir: Path) -> Mapping[str, SkillDocument]:
    loaded: dict[str, SkillDocument] = {}
    for name in SKILL_NAMES:
        path = skills_dir / name / "SKILL.md"
        content = _read_utf8(path)
        if not content.strip():
            raise ResourceValidationError(path, "SKILL.md must not be empty")
        if f"name: {name}" not in content:
            raise ResourceValidationError(path, "SKILL.md front matter has unexpected name")
        loaded[name] = SkillDocument(name=name, path=path, content=str(content))
    return MappingProxyType(dict(loaded))


def _load_references(references_dir: Path) -> Mapping[str, Any]:
    paths = {name: references_dir / name for name in REFERENCE_NAMES}
    casebook = _parse_casebook(paths["casebook.jsonl"])
    failure_modes = _validate_named_list(
        paths["failure_modes.json"],
        _parse_json(paths["failure_modes.json"]),
        list_field="failure_modes",
        required_item_fields={"id": str, "fp": str, "action": str},
    )
    portal_families = _validate_named_list(
        paths["portal_families.json"],
        _parse_json(paths["portal_families.json"]),
        list_field="families",
        required_item_fields={"id": str},
    )
    schema = _validate_schema(paths["schema.json"], _parse_json(paths["schema.json"]))
    loaded = {
        "casebook.jsonl": casebook,
        "failure_modes.json": failure_modes,
        "portal_families.json": portal_families,
        "schema.json": schema,
    }
    return MappingProxyType({name: _deep_freeze(loaded[name]) for name in REFERENCE_NAMES})


def load_canonical_resources(
    *,
    repo_root: Path | None = None,
    skills_dir: Path | None = None,
    references_dir: Path | None = None,
) -> CanonicalResources:
    """Load all three skills and four references into immutable structures.

    Paths are injectable for isolated tests. If ``repo_root`` is supplied it
    must itself carry all markers; it is never silently replaced by the CWD.
    """
    if repo_root is None:
        root = find_repo_root()
    else:
        root = Path(repo_root).resolve()
        if not _has_repo_markers(root):
            raise RepositoryRootNotFound(
                root,
                "provided repo_root lacks CLAUDE.md, scripts/fase2_municipios/, or skills/",
            )
    skill_base = _require_directory(
        Path(skills_dir).resolve() if skills_dir is not None else root / "skills"
    )
    reference_base = _require_directory(
        Path(references_dir).resolve()
        if references_dir is not None
        else skill_base / "fase2-resource-certifier" / "references"
    )
    return CanonicalResources(
        repo_root=root,
        skills=_load_skills(skill_base),
        references=_load_references(reference_base),
    )
