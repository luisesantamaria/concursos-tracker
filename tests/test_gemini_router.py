from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as C  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, data: dict | None = None,
                 text: str = ""):
        self.status_code = status_code
        self._data = data if data is not None else {"ok": True}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._data


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, json: dict, timeout: int):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected extra Gemini call")
        return self.responses.pop(0)


class RaisingThenPaidSession:
    def __init__(self):
        self.calls: list[dict] = []

    def post(self, url: str, json: dict, timeout: int):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if len(self.calls) == 1:
            raise requests.exceptions.RequestException(
                f"boom for {url}")
        return FakeResponse(data={"source": "paid"})


@pytest.fixture(autouse=True)
def clean_gemini_env(monkeypatch):
    for name in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_FREE",
        "GEMINI_FREE_MODEL",
        "GEMINI_FREE_FIRST",
        "GEMINI_FREE_RPM_LIMIT",
        "GEMINI_FREE_TPM_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    C.reset_gemini_post_call_count()


def test_gemini_free_first_off_uses_paid_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")
    session = FakeSession([FakeResponse(data={"source": "paid"})])

    data = C.gemini_post(session, "gemini-2.5-flash", {"contents": []})

    assert data == {"source": "paid"}
    assert "/models/gemini-2.5-flash:generateContent" in session.calls[0]["url"]
    assert "key=paid-key" in session.calls[0]["url"]
    assert C.gemini_post_call_counts() == {"total": 1, "free": 0, "paid": 1}


def test_gemini_free_first_uses_free_model_when_available(monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_FIRST", "1")
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "free-key")
    monkeypatch.setenv("GEMINI_FREE_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")
    session = FakeSession([FakeResponse(data={"source": "free"})])

    data = C.gemini_post(session, "gemini-2.5-flash", {"contents": []})

    assert data == {"source": "free"}
    assert "/models/gemini-3.1-flash-lite:generateContent" in session.calls[0]["url"]
    assert "key=free-key" in session.calls[0]["url"]
    assert C.gemini_post_call_counts() == {"total": 1, "free": 1, "paid": 0}


def test_gemini_free_quota_falls_back_to_paid_without_retrying_free(monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_FIRST", "1")
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "free-key")
    monkeypatch.setenv("GEMINI_FREE_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")
    session = FakeSession([
        FakeResponse(status_code=429, text="quota exceeded"),
        FakeResponse(data={"source": "paid"}),
    ])

    data = C.gemini_post(session, "gemini-2.5-flash", {"contents": []})

    assert data == {"source": "paid"}
    assert "key=free-key" in session.calls[0]["url"]
    assert "key=paid-key" in session.calls[1]["url"]
    assert C.gemini_post_call_counts() == {"total": 2, "free": 1, "paid": 1}


def test_gemini_free_limiter_full_goes_paid_direct(monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_FIRST", "1")
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "free-key")
    monkeypatch.setenv("GEMINI_FREE_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")
    monkeypatch.setenv("GEMINI_FREE_RPM_LIMIT", "0")
    session = FakeSession([FakeResponse(data={"source": "paid"})])

    data = C.gemini_post(session, "gemini-2.5-flash", {"contents": []})

    assert data == {"source": "paid"}
    assert len(session.calls) == 1
    assert "key=paid-key" in session.calls[0]["url"]
    assert C.gemini_post_call_counts() == {"total": 1, "free": 0, "paid": 1}


def test_gemini_error_logs_redact_keys(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_FREE_FIRST", "1")
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "free-key")
    monkeypatch.setenv("GEMINI_FREE_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-key")
    session = RaisingThenPaidSession()

    data = C.gemini_post(session, "gemini-2.5-flash", {"contents": []})

    assert data == {"source": "paid"}
    captured = capsys.readouterr()
    assert "free-key" not in captured.out
    assert "paid-key" not in captured.out
    assert "key=<redacted>" in captured.out
    assert C.gemini_post_call_counts() == {"total": 2, "free": 1, "paid": 1}
