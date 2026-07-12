"""Offline coverage for V2 HTTP content codings and gentle free isolation."""

from __future__ import annotations

import base64
from email.message import Message
import gzip
import hashlib
import os
import re
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


def test_utf8_bom_precedes_conflicting_header_and_is_diagnosed(monkeypatch) -> None:
    payload = b"\xef\xbb\xbf" + fx.HTML.encode("utf-8")
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=iso-8859-1"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    assert any("charset_conflict" in item for item in fetched.decode_diagnostics)


def test_declared_non_utf8_charset_decodes_strictly(monkeypatch) -> None:
    html = fx.HTML.replace("Fixture", "Ação")
    install_connections(
        monkeypatch,
        FakeResponse(
            html.encode("iso-8859-1"),
            content_type="text/html; charset=iso-8859-1",
        ),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == html
    assert "Ação" in fetched.content


def test_document_charset_is_used_without_header_charset(monkeypatch) -> None:
    html = fx.HTML.replace(
        "<head>", '<head><meta charset="windows-1252">'
    ).replace("Fixture", "Ação €")
    install_connections(
        monkeypatch,
        FakeResponse(html.encode("cp1252"), content_type="text/html"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == html
    assert "Ação €" in fetched.content
    assert any("document_charset" in item for item in fetched.decode_diagnostics)


def test_controlled_cp1252_fallback_without_charset(monkeypatch) -> None:
    html = fx.HTML.replace("Fixture", "Ação €")
    install_connections(
        monkeypatch,
        FakeResponse(html.encode("cp1252"), content_type="text/html"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == html
    assert "Ação €" in fetched.content
    assert "fallback:cp1252" in fetched.decode_diagnostics


def test_declared_charset_mismatch_falls_back_with_diagnostics(monkeypatch) -> None:
    """Charset DECLARADO que miente (caso Porto Alegre: header utf-8, bytes
    latin-1/cp1252 en el body): no se falla cerrado; se registra el conflicto
    como diagnostico auditable y se continua la cadena determinista de decode.
    El BOM corrupto sigue siendo estricto (evidencia binaria)."""
    install_connections(
        monkeypatch,
        FakeResponse(
            b"<html>Sele\xe7\xe3o e provimento</html>",
            content_type="text/html; charset=utf-8",
        ),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "Seleção e provimento" in fetched.content
    assert any(
        item.startswith("declared_charset_decode_failed:")
        for item in fetched.decode_diagnostics
    )
    assert "fallback:cp1252" in fetched.decode_diagnostics


def test_invalid_bytes_for_declared_charset_still_decode_deterministically(
    monkeypatch,
) -> None:
    install_connections(
        monkeypatch,
        FakeResponse(b"<html>inv\xfflido</html>", content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "invÿlido" in fetched.content
    assert any(
        item.startswith("declared_charset_decode_failed:")
        for item in fetched.decode_diagnostics
    )


def test_unicode_evidence_offsets_reconstruct_exact_snapshot_slice(monkeypatch) -> None:
    html = fx.HTML.replace("Fixture", "Ação 😀")
    install_connections(monkeypatch, FakeResponse(html.encode("utf-8")))

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)
    quote = "Ação 😀"
    start = fetched.content.index(quote)

    assert fetched.content[start:start + len(quote)] == quote
    assert len(quote) == 6


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


def test_turnkey_cli_loads_only_explicit_credential_file_without_adc_environment(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY_FREE", "free-fixture")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-fixture")
    monkeypatch.setenv("VERTEX_TOKEN", "vertex-fixture")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/fixture/ca.pem")
    staging = tmp_path / "staging"
    credentials = tmp_path / "gemini.env"
    credentials.write_text(
        "GEMINI_API_KEY_FREE=file-free\n"
        "IGNORED_SECRET=ignored\n"
        "GEMINI_API_KEY=file-paid\n",
        encoding="utf-8",
    )

    code = golden_live_main(
        [
            "--provider", "gemini_free",
            "--tools", "none",
            "--grounding", "off",
            "--golden", str(tmp_path / "missing-golden.csv"),
            "--url-map", str(tmp_path / "missing-map.csv"),
            "--v1-corpus-dir", str(tmp_path / "missing-v1"),
            "--output-dir", str(staging / "run"),
            "--credentials-file", str(credentials),
        ],
        staging_root=staging,
    )

    assert code == 2
    # Ambient credentials are neither selected nor rewritten; the runner's
    # credential mapping comes solely from the explicit two-name file parser.
    assert os.environ["GEMINI_API_KEY"] == "paid-fixture"
    assert os.environ["VERTEX_TOKEN"] == "vertex-fixture"
    assert os.environ["GEMINI_API_KEY_FREE"] == "free-fixture"
    assert os.environ["HTTPS_PROXY"] == "http://proxy.invalid"
    assert os.environ["SSL_CERT_FILE"] == "/fixture/ca.pem"


def test_mostly_utf8_with_stray_byte_recovers_as_utf8_replace(monkeypatch) -> None:
    """Caso Porto Alegre (golden36): header declara utf-8, body es utf-8 GENUINO
    (acentos = secuencias 0xC3+continuacion) con 1 byte suelto invalido. El
    fallback total a cp1252 destruia todo el acentuado (mojibake) y rompia las
    citas. Con el fingerprint estructural (0xC3 [0x80-0xBF] presente + charset
    declarado utf-8) se recupera con errors='replace': el texto acentuado queda
    intacto y solo el byte roto se marca U+FFFD."""
    payload = "<html>Seleção e Provimento — Administração</html>".encode("utf-8")
    payload = payload.replace(b"</html>", b"\xe9</html>")  # byte suelto invalido
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "Seleção e Provimento" in fetched.content
    assert "Administração" in fetched.content
    assert "utf8_replace_recovered_declared_charset" in fetched.decode_diagnostics
    assert "�" in fetched.content  # el byte roto queda marcado, no oculto


def test_fully_valid_utf8_declared_utf8_decodes_clean_without_replace(
    monkeypatch,
) -> None:
    """Control negativo (a): utf-8 100% valido declarado utf-8 nunca pasa por
    el fingerprint -- el decode estricto del charset declarado ya tiene
    exito, asi que el diagnostico de replace ni se evalua."""
    payload = "<html>Ação legítima e válida</html>".encode("utf-8")
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "Ação legítima e válida" in fetched.content
    assert "utf8_replace_recovered_declared_charset" not in fetched.decode_diagnostics


def test_cp1252_selecao_declared_utf8_falls_back_without_replace(monkeypatch) -> None:
    """Control negativo (b): cp1252 genuino con 'SELEÇÃO' (bytes 0xC7 0xC3
    seguidos de 'O' ASCII, fuera del rango de continuacion) declarado utf-8.
    0 matches del fingerprint incluso antes del umbral -- cae a cp1252 y la
    palabra queda intacta."""
    payload = "<html>SELEÇÃO e provimento</html>".encode("cp1252")
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "SELEÇÃO e provimento" in fetched.content
    assert any(
        item.startswith("declared_charset_decode_failed:")
        for item in fetched.decode_diagnostics
    )
    assert "fallback:cp1252" in fetched.decode_diagnostics
    assert "utf8_replace_recovered_declared_charset" not in fetched.decode_diagnostics


def test_cp1252_with_incidental_fingerprint_matches_below_threshold_still_falls_back(
    monkeypatch,
) -> None:
    """Control negativo (c) -- el caso que el fix corrige: el mismo documento
    cp1252 de (b), pero con 1-2 coincidencias INCIDENTALES de 0xC3 +
    continuacion metidas en un comentario HTML (no texto utf-8 real). Antes
    del umbral, una sola coincidencia ya disparaba errors='replace' sobre TODO
    el documento y mutilaba el acentuado cp1252 genuino. Por debajo del
    umbral (3) debe seguir cayendo a cp1252 intacto."""
    # 0xA0/0xA9 son bytes cp1252 ASIGNADOS (>=0xA0 coincide con Latin-1) para
    # que el decode cp1252 del documento ENTERO tenga exito -- a diferencia
    # de los huecos no asignados de cp1252 (0x81/0x8D/0x8F/0x90/0x9D).
    comment = b"<!-- \xc3\xa0\xc3\xa9 -->"  # 2 matches incidentales, no utf-8 real
    payload = b"<html>" + comment + "SELEÇÃO e provimento".encode("cp1252") + b"</html>"
    assert len(re.findall(rb"\xc3[\x80-\xbf]", payload)) == 2
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "SELEÇÃO e provimento" in fetched.content
    assert any(
        item.startswith("declared_charset_decode_failed:")
        for item in fetched.decode_diagnostics
    )
    assert "fallback:cp1252" in fetched.decode_diagnostics
    assert "utf8_replace_recovered_declared_charset" not in fetched.decode_diagnostics


def test_raw_payload_sha256_is_always_captured(monkeypatch) -> None:
    payload = html_bytes()
    install_connections(monkeypatch, FakeResponse(payload))

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.raw_payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert fetched.raw_payload_head_b64 == ""
    assert fetched.raw_payload_truncated is False


def test_raw_payload_head_b64_present_only_on_charset_anomaly(monkeypatch) -> None:
    payload = b"<html>Sele\xe7\xe3o e provimento</html>"
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert any(
        item.startswith("declared_charset_decode_failed:")
        for item in fetched.decode_diagnostics
    )
    assert fetched.raw_payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert fetched.raw_payload_head_b64 == base64.b64encode(payload).decode("ascii")
    assert fetched.raw_payload_truncated is False


def test_raw_payload_head_b64_absent_on_clean_declared_charset_decode(
    monkeypatch,
) -> None:
    payload = html_bytes()
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.decode_diagnostics == ("header_charset:utf-8",)
    assert fetched.raw_payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert fetched.raw_payload_head_b64 == ""
    assert fetched.raw_payload_truncated is False
