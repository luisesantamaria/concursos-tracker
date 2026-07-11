"""Offline coverage for V2 HTTP content codings and gentle free isolation."""

from __future__ import annotations

from email.message import Message
import gzip
import os
import socket
import zlib

import pytest

from scripts.fase2_municipios.v2.eval import live_abc_adapter as live
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCAdapter,
    LiveCauseKind,
    LiveFetchError,
    OrionHTTPFetcher,
)
from scripts.fase2_municipios.v2.eval.run_golden_live import main as golden_live_main
from scripts.fase2_municipios.v2.eval.tests import test_live_abc_error_evidence as fx
from scripts.fase2_municipios.v2.gemini import gentle_free_only_environment


pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("offline content-encoding test attempted network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    yield
    assert attempts == []


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_encoding: str = "",
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
    ) -> None:
        self.status = status
        self.payload = payload
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding

    def read(self) -> bytes:
        return self.payload

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, path, headers) -> None:
        self.requests.append((method, path, dict(headers)))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def install_connections(monkeypatch, *responses: FakeResponse):
    connections = [FakeConnection(response) for response in responses]
    pending = list(connections)

    def connection(parsed, timeout_seconds):
        return pending.pop(0)

    monkeypatch.setattr(
        OrionHTTPFetcher, "_connection", staticmethod(connection)
    )
    return connections


def html_bytes() -> bytes:
    return fx.HTML.encode("utf-8")


@pytest.mark.parametrize(
    ("content_encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
    ],
)
def test_declared_content_encoding_is_decompressed(
    monkeypatch, content_encoding, compress
) -> None:
    connections = install_connections(
        monkeypatch,
        FakeResponse(compress(html_bytes()), content_encoding=content_encoding),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    assert fetched.content
    assert connections[0].closed is True
    advertised = connections[0].requests[0][2]["Accept-Encoding"]
    assert "gzip" in advertised and "deflate" in advertised


def test_gzip_magic_without_header_is_decompressed(monkeypatch) -> None:
    install_connections(monkeypatch, FakeResponse(gzip.compress(html_bytes())))

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML


def test_brotli_is_decoded_when_available_or_fails_closed_with_evidence(
    monkeypatch,
) -> None:
    if live._brotli is not None:
        install_connections(
            monkeypatch,
            FakeResponse(live._brotli.compress(html_bytes()), content_encoding="br"),
        )
        assert OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1).html == fx.HTML
        return

    install_connections(
        monkeypatch,
        FakeResponse(b"encoded without optional decoder", content_encoding="br"),
    )
    adapter = LiveABCAdapter(
        fetcher=OrionHTTPFetcher(),
        target_urls={("Fixture", fx.BUCKET): fx.URL},
        certifier=fx.ValidCertifier(),
        prosecutor=fx.ValidProsecutor(),
        judge=fx.UnusedJudge(),
    )

    outcome = adapter.request("Fixture", fx.BUCKET)

    assert outcome.decision == "revisar"
    assert outcome.cause.kind is LiveCauseKind.ACCESS_FAILURE
    assert outcome.audit_events[0].phase == "fetch"
    assert "LiveFetchError: brotli_decoder_unavailable" in outcome.audit_events[0].errors


def test_corrupt_gzip_is_audited_and_next_unit_continues(monkeypatch) -> None:
    first = ("Broken", fx.BUCKET)
    second = ("Fixture", fx.BUCKET)
    first_url = "https://broken.rs.gov.br/concursos"
    install_connections(
        monkeypatch,
        FakeResponse(b"\x1f\x8b\x08\x00truncated", content_encoding="gzip"),
        FakeResponse(html_bytes()),
    )
    adapter = LiveABCAdapter(
        fetcher=OrionHTTPFetcher(),
        target_urls={first: first_url, second: fx.URL},
        certifier=fx.ValidCertifier(),
        prosecutor=fx.ValidProsecutor(),
        judge=fx.UnusedJudge(),
    )

    failed = adapter.request(*first)
    succeeded = adapter.request(*second)

    assert failed.decision == "revisar"
    assert failed.cause.kind is LiveCauseKind.ACCESS_FAILURE
    errors = failed.audit_events[0].errors
    assert errors[0] == "LiveFetchError: response_decompression_failed"
    assert any("EOFError" in error or "BadGzipFile" in error for error in errors[1:])
    assert succeeded.cause.kind is LiveCauseKind.SUCCESS
    assert succeeded.decision == "indice_oficial"


def test_uncompressed_body_is_unchanged(monkeypatch) -> None:
    connections = install_connections(monkeypatch, FakeResponse(html_bytes()))

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    advertised = connections[0].requests[0][2]["Accept-Encoding"]
    if live._brotli is None:
        assert "br" not in {item.strip() for item in advertised.split(",")}


def test_gentle_free_environment_preserves_network_and_locale_without_reading_paid() -> None:
    class GuardedEnvironment(dict):
        def __getitem__(self, name):
            if name in {"GEMINI_API_KEY", "VERTEX_TOKEN", "MY_SERVICE_ACCOUNT_JSON"}:
                raise AssertionError("paid credential value must not be read")
            return super().__getitem__(name)

    source = GuardedEnvironment({
        "GEMINI_API_KEY_FREE": "free-fixture",
        "GEMINI_API_KEY": "paid-secret",
        "VERTEX_TOKEN": "vertex-secret",
        "MY_SERVICE_ACCOUNT_JSON": "service-account-secret",
        "HTTPS_PROXY": "http://proxy.invalid",
        "SSL_CERT_FILE": "/fixture/ca.pem",
        "LANG": "pt_BR.UTF-8",
    })

    sanitized = gentle_free_only_environment(source)

    assert sanitized == {
        "GEMINI_API_KEY_FREE": "free-fixture",
        "HTTPS_PROXY": "http://proxy.invalid",
        "SSL_CERT_FILE": "/fixture/ca.pem",
        "LANG": "pt_BR.UTF-8",
    }


def test_turnkey_cli_removes_only_paid_credentials_from_real_child_environment(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "free-fixture")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-fixture")
    monkeypatch.setenv("VERTEX_TOKEN", "vertex-fixture")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/fixture/ca.pem")
    staging = tmp_path / "staging"

    code = golden_live_main(
        [
            "--provider", "gemini_free",
            "--tools", "none",
            "--grounding", "off",
            "--golden", str(tmp_path / "missing-golden.csv"),
            "--url-map", str(tmp_path / "missing-map.csv"),
            "--v1-corpus-dir", str(tmp_path / "missing-v1"),
            "--output-dir", str(staging / "run"),
        ],
        staging_root=staging,
    )

    assert code == 2
    assert "GEMINI_API_KEY" not in os.environ
    assert "VERTEX_TOKEN" not in os.environ
    assert os.environ["GEMINI_API_KEY_FREE"] == "free-fixture"
    assert os.environ["HTTPS_PROXY"] == "http://proxy.invalid"
    assert os.environ["SSL_CERT_FILE"] == "/fixture/ca.pem"
