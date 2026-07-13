"""Offline coverage for the "transporte" fixes (holdout 12-jul):

SUB-CAUSA 1 -- a 200 with real HTML but no Content-Type header
(canudosdovale, Vercel) must not be rejected outright: sniff the body.

SUB-CAUSA 2 -- an incomplete TLS chain (saovendelino.multi24h.com.br,
missing GlobalSign intermediate) must recover once via AIA (Authority
Information Access) instead of failing closed forever.
"""

from __future__ import annotations

import datetime
from email.message import Message
import http.client
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID
import pytest

from scripts.fase2_municipios.v2.eval import live_abc_adapter as live
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveFetchError,
    OrionHTTPFetcher,
)
from scripts.fase2_municipios.v2.eval.tests import test_live_abc_error_evidence as fx


pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("offline transport-recovery test attempted network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    yield
    assert attempts == []


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_encoding: str = "",
        content_type: str | None = "text/html; charset=utf-8",
        status: int = 200,
    ) -> None:
        self.status = status
        self.payload = payload
        self.headers = Message()
        if content_type is not None:
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

    def connection(parsed, timeout_seconds, ssl_context=None):
        return pending.pop(0)

    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    return connections


def html_bytes() -> bytes:
    return fx.HTML.encode("utf-8")


# ---------------------------------------------------------------------------
# SUB-CAUSA 1: Content-Type ausente
# ---------------------------------------------------------------------------


def test_missing_content_type_with_html_body_is_sniffed_and_accepted(monkeypatch) -> None:
    install_connections(
        monkeypatch,
        FakeResponse(html_bytes(), content_type=None),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    assert fetched.content  # extracted text survived the synthetic content-type
    assert fetched.title
    assert "content_type_sniffed=true" in fetched.decode_diagnostics


def test_missing_content_type_with_leading_whitespace_html_is_sniffed(monkeypatch) -> None:
    payload = b"   \n\t" + html_bytes()
    install_connections(
        monkeypatch,
        FakeResponse(payload, content_type=None),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert "content_type_sniffed=true" in fetched.decode_diagnostics


def test_missing_content_type_with_binary_body_is_rejected(monkeypatch) -> None:
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    install_connections(
        monkeypatch,
        FakeResponse(pdf_bytes, content_type=None),
    )

    with pytest.raises(LiveFetchError, match="response_not_html_or_text"):
        OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)


def test_present_html_content_type_header_is_unchanged(monkeypatch) -> None:
    install_connections(
        monkeypatch,
        FakeResponse(html_bytes(), content_type="text/html; charset=utf-8"),
    )

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    assert "content_type_sniffed=true" not in fetched.decode_diagnostics


def test_present_binary_content_type_header_still_rejected_without_sniffing(
    monkeypatch,
) -> None:
    install_connections(
        monkeypatch,
        FakeResponse(b"%PDF-1.4 real pdf bytes", content_type="application/pdf"),
    )

    with pytest.raises(LiveFetchError, match="response_not_html_or_text"):
        OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)


# ---------------------------------------------------------------------------
# SUB-CAUSA 2: cadena SSL incompleta -- recuperacion AIA
# ---------------------------------------------------------------------------


def _ssl_error(message: str) -> ssl.SSLCertVerificationError:
    exc = ssl.SSLCertVerificationError(1, message)
    exc.verify_message = message
    return exc


def test_incomplete_chain_error_recovers_via_aia_and_retries_once(monkeypatch) -> None:
    success = FakeConnection(FakeResponse(html_bytes()))
    attempts: list[object | None] = []

    def failing_request(*args, **kwargs):
        raise _ssl_error(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate"
        )

    failing = FakeConnection(FakeResponse(b""))
    failing.request = failing_request

    def connection(parsed, timeout_seconds, ssl_context=None):
        attempts.append(ssl_context)
        return failing if len(attempts) == 1 else success

    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    recovered_context = object()
    calls = []

    def fake_recover(host, port, timeout):
        calls.append((host, port, timeout))
        return recovered_context

    monkeypatch.setattr(live, "_recover_ssl_context_via_aia", fake_recover)

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    assert "ssl_aia_recovered=true" in fetched.decode_diagnostics
    assert attempts == [None, recovered_context]
    assert len(calls) == 1
    assert calls[0][0] == "fixture.rs.gov.br"


def test_incomplete_chain_error_propagates_original_when_recovery_fails(
    monkeypatch,
) -> None:
    original = _ssl_error("unable to get local issuer certificate")

    def failing_request(*args, **kwargs):
        raise original

    failing = FakeConnection(FakeResponse(b""))
    failing.request = failing_request

    def connection(parsed, timeout_seconds, ssl_context=None):
        return failing

    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    monkeypatch.setattr(live, "_recover_ssl_context_via_aia", lambda *a, **k: None)

    with pytest.raises(ssl.SSLCertVerificationError) as excinfo:
        OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert excinfo.value is original


def test_incomplete_chain_recovery_is_attempted_only_once(monkeypatch) -> None:
    """If the recovered context STILL fails verification, the second failure
    is not itself retried again -- one AIA recovery attempt, never a loop."""

    def failing_request(*args, **kwargs):
        raise _ssl_error("unable to get local issuer certificate")

    first_failing = FakeConnection(FakeResponse(b""))
    first_failing.request = failing_request
    second_failing = FakeConnection(FakeResponse(b""))
    second_failing.request = failing_request
    connections = [first_failing, second_failing]

    def connection(parsed, timeout_seconds, ssl_context=None):
        return connections.pop(0)

    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    monkeypatch.setattr(live, "_recover_ssl_context_via_aia", lambda *a, **k: object())

    with pytest.raises(ssl.SSLCertVerificationError):
        OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert connections == []


def test_ssl_error_distinct_from_incomplete_chain_never_attempts_recovery(
    monkeypatch,
) -> None:
    """Hostname mismatch (a DIFFERENT SSL verification failure) must never
    trigger AIA recovery -- it propagates untouched, same as before the fix."""

    def failing_request(*args, **kwargs):
        raise _ssl_error(
            "Hostname mismatch, certificate is not valid for 'wrong.example'."
        )

    failing = FakeConnection(FakeResponse(b""))
    failing.request = failing_request

    def connection(parsed, timeout_seconds, ssl_context=None):
        return failing

    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))

    def guard(*a, **k):
        raise AssertionError("AIA recovery must not run for a non-chain SSL error")

    monkeypatch.setattr(live, "_recover_ssl_context_via_aia", guard)

    with pytest.raises(ssl.SSLCertVerificationError, match="Hostname mismatch"):
        OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)


def test_tls_handshake_timeout_waits_three_seconds_and_retries_once(
    monkeypatch,
) -> None:
    """Seberi/PSS: transient _ssl handshake timeout has its own retry path."""

    attempts = []
    success = FakeConnection(FakeResponse(html_bytes()))

    def timeout_request(*args, **kwargs):
        raise TimeoutError("_ssl.c:1063: The handshake operation timed out")

    failing = FakeConnection(FakeResponse(b""))
    failing.request = timeout_request

    def connection(parsed, timeout_seconds, ssl_context=None):
        attempts.append(ssl_context)
        return failing if len(attempts) == 1 else success

    sleeps = []
    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    monkeypatch.setattr(live.time, "sleep", sleeps.append)

    def aia_must_not_run(*args, **kwargs):
        raise AssertionError("handshake timeout must not use AIA recovery")

    monkeypatch.setattr(live, "_recover_ssl_context_via_aia", aia_must_not_run)

    fetched = OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert fetched.html == fx.HTML
    assert attempts == [None, None]
    assert sleeps == [3.0]


def test_tls_handshake_timeout_propagates_after_single_retry(monkeypatch) -> None:
    attempts = []
    original = TimeoutError("_ssl.c:1063: The handshake operation timed out")

    def timeout_request(*args, **kwargs):
        raise original

    def connection(parsed, timeout_seconds, ssl_context=None):
        attempts.append(ssl_context)
        failing = FakeConnection(FakeResponse(b""))
        failing.request = timeout_request
        return failing

    sleeps = []
    monkeypatch.setattr(OrionHTTPFetcher, "_connection", staticmethod(connection))
    monkeypatch.setattr(live.time, "sleep", sleeps.append)

    with pytest.raises(TimeoutError) as raised:
        OrionHTTPFetcher().fetch(fx.URL, timeout_seconds=1)

    assert raised.value is original
    assert attempts == [None, None]
    assert sleeps == [3.0]


# ---------------------------------------------------------------------------
# AIA plumbing: parsing + bundle construction, exercised with real certs
# (generated in-memory, never touching the network -- no_network still holds).
# ---------------------------------------------------------------------------


def _self_signed_cert(
    *, common_name: str, aia_uri: str | None = None
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2035, 1, 1))
    )
    if aia_uri is not None:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess([
                x509.AccessDescription(
                    AuthorityInformationAccessOID.CA_ISSUERS,
                    x509.UniformResourceIdentifier(aia_uri),
                ),
            ]),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    return cert, key


def test_aia_ca_issuer_uris_extracts_the_ca_issuers_uri() -> None:
    cert, _ = _self_signed_cert(
        common_name="leaf.example",
        aia_uri="http://example.invalid/intermediate.crt",
    )
    der = cert.public_bytes(serialization.Encoding.DER)

    uris = live._aia_ca_issuer_uris(der)

    assert uris == ("http://example.invalid/intermediate.crt",)


def test_aia_ca_issuer_uris_returns_empty_without_the_extension() -> None:
    cert, _ = _self_signed_cert(common_name="leaf.example")
    der = cert.public_bytes(serialization.Encoding.DER)

    assert live._aia_ca_issuer_uris(der) == ()


class _FakeAIAHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body


class _FakeAIAHTTPConnection:
    last_instance: "_FakeAIAHTTPConnection | None" = None

    def __init__(self, host, port=None, timeout=None) -> None:
        self.host = host
        self.port = port
        self.requested_path = None
        _FakeAIAHTTPConnection.last_instance = self

    def request(self, method, path, headers=None) -> None:
        self.requested_path = path

    def getresponse(self):
        return _FakeAIAHTTPConnection.response

    def close(self) -> None:
        pass


@pytest.mark.parametrize("encoding", [serialization.Encoding.DER, serialization.Encoding.PEM])
def test_download_intermediate_pem_accepts_der_or_pem_body(monkeypatch, encoding) -> None:
    intermediate, _ = _self_signed_cert(common_name="Intermediate CA")
    body = intermediate.public_bytes(encoding)
    _FakeAIAHTTPConnection.response = _FakeAIAHTTPResponse(body)
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeAIAHTTPConnection)

    pem = live._download_intermediate_pem(
        "http://example.invalid/intermediate.crt", timeout=1
    )

    assert pem == intermediate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    assert _FakeAIAHTTPConnection.last_instance.requested_path == "/intermediate.crt"


def test_download_intermediate_pem_raises_on_http_error_status(monkeypatch) -> None:
    _FakeAIAHTTPConnection.response = _FakeAIAHTTPResponse(b"not found", status=404)
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeAIAHTTPConnection)

    with pytest.raises(LiveFetchError, match="ssl_aia_issuer_fetch_failed"):
        live._download_intermediate_pem(
            "http://example.invalid/intermediate.crt", timeout=1
        )


def test_write_temp_ca_bundle_merges_certifi_and_intermediate() -> None:
    intermediate, _ = _self_signed_cert(common_name="Intermediate CA")
    intermediate_pem = intermediate.public_bytes(serialization.Encoding.PEM).decode("ascii")

    import os

    bundle_path = live._write_temp_ca_bundle(intermediate_pem)
    try:
        bundle_text = open(bundle_path, "r", encoding="ascii").read()
        assert intermediate_pem in bundle_text
        assert bundle_text.count("BEGIN CERTIFICATE") > 1  # certifi's store + intermediate
        # The merged bundle must be loadable as a real trust store.
        context = ssl.create_default_context(cafile=bundle_path)
        assert isinstance(context, ssl.SSLContext)
    finally:
        os.unlink(bundle_path)


def test_recover_ssl_context_via_aia_end_to_end_with_mocked_leaf_and_download(
    monkeypatch,
) -> None:
    leaf, _ = _self_signed_cert(
        common_name="saovendelino.multi24h.com.br",
        aia_uri="http://secure.example.invalid/cacert/intermediate.crt",
    )
    intermediate, _ = _self_signed_cert(common_name="GlobalSign GCC R6 AlphaSSL CA 2025")
    leaf_der = leaf.public_bytes(serialization.Encoding.DER)
    intermediate_pem = intermediate.public_bytes(serialization.Encoding.PEM).decode("ascii")

    monkeypatch.setattr(live, "_fetch_leaf_certificate_der", lambda host, port, timeout: leaf_der)
    monkeypatch.setattr(
        live, "_download_intermediate_pem", lambda uri, timeout: intermediate_pem
    )

    context = live._recover_ssl_context_via_aia("saovendelino.multi24h.com.br", 443, 5.0)

    assert isinstance(context, ssl.SSLContext)


def test_recover_ssl_context_via_aia_returns_none_without_aia_extension(
    monkeypatch,
) -> None:
    leaf, _ = _self_signed_cert(common_name="no-aia.example")
    leaf_der = leaf.public_bytes(serialization.Encoding.DER)

    monkeypatch.setattr(live, "_fetch_leaf_certificate_der", lambda host, port, timeout: leaf_der)

    def unexpected_download(*a, **k):
        raise AssertionError("must not attempt a download without an AIA URI")

    monkeypatch.setattr(live, "_download_intermediate_pem", unexpected_download)

    assert live._recover_ssl_context_via_aia("no-aia.example", 443, 5.0) is None


def test_recover_ssl_context_via_aia_returns_none_when_leaf_fetch_fails(
    monkeypatch,
) -> None:
    def raise_error(host, port, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(live, "_fetch_leaf_certificate_der", raise_error)

    assert live._recover_ssl_context_via_aia("unreachable.example", 443, 5.0) is None
