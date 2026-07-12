from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import verdict_extract as V  # noqa: E402


def _fake_post(calls: list[str]):
    def post(_session, _model, payload, _timeout):
        calls.append(payload["contents"][0]["parts"][0]["text"])
        return {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"items":[{"cita":"Edital 01/2024","emissor":""}]}'
                    }]
                }
            }]
        }
    return post


def test_extract_items_uses_one_window_for_short_text():
    calls: list[str] = []
    items = V.extract_items("x" * 14000, None, _fake_post(calls), "model", 1)
    assert len(calls) == 1
    assert items == [{"cita": "Edital 01/2024", "emissor": ""}]


def test_extract_items_windows_long_real_fixture_and_dedupes():
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "render" / "nova_boa_vista_c.json")
        .read_text(encoding="utf-8")
    )
    assert len(fixture["text"]) > 14000

    calls: list[str] = []
    items = V.extract_items(fixture["text"], None, _fake_post(calls), "model", 1)

    assert len(calls) == 4
    assert items == [{"cita": "Edital 01/2024", "emissor": ""}]
