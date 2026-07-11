"""Offline-only tests for the canonical V2 resource loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.loader import (
    REFERENCE_NAMES,
    SKILL_NAMES,
    ResourceDecodeError,
    load_canonical_resources,
)


pytestmark = pytest.mark.offline

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_loads_all_real_canonical_resources_as_immutable() -> None:
    resources = load_canonical_resources(repo_root=REPO_ROOT)

    assert tuple(resources.skills) == SKILL_NAMES
    assert tuple(resources.references) == REFERENCE_NAMES
    assert len(resources.references["casebook.jsonl"]) == 19
    assert resources.references["schema.json"]["type"] == "object"

    with pytest.raises(TypeError):
        resources.references["schema.json"]["type"] = "array"
    with pytest.raises(TypeError):
        resources.references["portal_families.json"]["families"][0]["id"] = "changed"


def test_corrupt_jsonl_reports_injected_path_and_line(tmp_path: Path) -> None:
    references_dir = tmp_path / "references"
    references_dir.mkdir()
    (references_dir / "schema.json").write_text(
        json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Fixture",
            "type": "object",
            "required": [],
            "properties": {},
        }),
        encoding="utf-8",
    )
    (references_dir / "portal_families.json").write_text(
        json.dumps({"version": 1, "families": [{"id": "fixture"}]}),
        encoding="utf-8",
    )
    (references_dir / "failure_modes.json").write_text(
        json.dumps({
            "version": 1,
            "failure_modes": [{"id": "fixture", "fp": "fixture", "action": "review"}],
        }),
        encoding="utf-8",
    )
    corrupt = references_dir / "casebook.jsonl"
    corrupt.write_text(
        json.dumps({
            "case_id": "ok",
            "municipio": "fixture",
            "family": "fixture",
            "expected": "review",
            "bucket": "concurso_publico",
            "facts": ["fixture"],
            "lesson": "fixture",
        }) + "\n{" + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ResourceDecodeError) as raised:
        load_canonical_resources(
            repo_root=REPO_ROOT,
            skills_dir=REPO_ROOT / "skills",
            references_dir=references_dir,
        )

    assert raised.value.path == corrupt
    assert "line 2" in str(raised.value)
    assert str(corrupt) in str(raised.value)
    assert "fixture" not in str(raised.value)
