from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import verdict_extract as V  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "render"


def _decision(fixture: dict) -> tuple[str, dict]:
    text = fixture.get("text") or ""
    if text.count("\n") < 3:
        return "revisar", {"motivo": "texto sin estructura de lineas (render fallido)"}
    return V.adjudicate(
        text,
        fixture["bucket"],
        fixture["municipio"],
        fixture.get("items_llm") or [],
        anchors=fixture.get("anchors") or [],
        title=fixture.get("title") or "",
    )


def _fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def test_fixture_set_is_seeded():
    fixtures = _fixtures()
    assert fixtures, f"no fixtures found in {FIXTURE_DIR}"
    assert any(p.stem == "flat_text" for p in fixtures)


def test_verdict_direction_lock():
    for path in _fixtures():
        fixture = json.loads(path.read_text(encoding="utf-8"))
        decision, ev = _decision(fixture)
        expected = fixture["expected"]
        label = fixture["label"]

        if label == "render_gap" and expected == "revisar":
            assert decision in {"revisar", "confirmar"}, path.name
            continue

        assert decision == expected, (
            f"{path.name}: expected {expected}, got {decision}; "
            f"evidence={ev}"
        )
        if label == "fp":
            assert decision != "confirmar", f"{path.name}: FP fixture promoted"
        if label == "tp":
            assert decision != "revisar", f"{path.name}: TP fixture degraded"


def test_llm_items_are_monotonic_over_deterministic_floor():
    for path in _fixtures():
        fixture = json.loads(path.read_text(encoding="utf-8"))
        text = fixture.get("text") or ""
        if text.count("\n") < 3:
            continue
        empty_decision, empty_ev = V.adjudicate(
            text,
            fixture["bucket"],
            fixture["municipio"],
            [],
            anchors=fixture.get("anchors") or [],
            title=fixture.get("title") or "",
        )
        full_decision, full_ev = _decision(fixture)
        assert not (empty_decision == "confirmar" and full_decision != "confirmar"), (
            f"{path.name}: LLM items broke monotonicity; "
            f"empty={empty_ev}; full={full_ev}"
        )
