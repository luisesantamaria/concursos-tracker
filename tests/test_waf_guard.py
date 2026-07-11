from __future__ import annotations

import csv
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "shared"))

import cascade_municipios as C  # noqa: E402
import cierre_dataset as Z      # noqa: E402
import waf_guard                # noqa: E402


class _Resp:
    def __init__(self, status_code: int, text: str = "<html><title>x</title>x</html>"):
        self.status_code = status_code
        self.text = text
        self.url = "https://one.test/concursos"
        self.headers = {"content-type": "text/html"}


class _Session:
    def __init__(self, response: _Resp):
        self.response = response
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


def setup_function():
    waf_guard.reset_for_tests()
    waf_guard.reset_clock_for_tests()
    Z._URL_CONTENT_CACHE.clear()
    Z._URL_CACHE_ENABLED = True
    for key in Z._RUN_STATS:
        Z._RUN_STATS[key] = 0


def test_rate_limit_status_does_not_retry_with_impersonate(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(C, "_fetch_browser_impersonate",
                        lambda url, timeout: calls.append(url))

    page = C.fetch_page(_Session(_Resp(429)), "https://one.test/concursos", 1)

    assert calls == []
    assert page.status == 429


def test_frozen_group_cuts_fetch_before_network(monkeypatch):
    monkeypatch.setattr(waf_guard.socket, "gethostbyname", lambda _host: "203.0.113.42")
    waf_guard.freeze("https://one.test/concursos")
    session = _Session(_Resp(200))

    page = C.fetch_page(session, "https://two.test/processos", 1)

    assert page.error == "waf_frozen"
    assert session.calls == 0


def test_multi_tenant_saas_freezes_by_host_not_shared_ip(monkeypatch):
    monkeypatch.setattr(waf_guard.socket, "gethostbyname", lambda _host: "203.0.113.42")

    waf_guard.freeze("https://acegua.atende.net/transparencia/item/processos-seletivos")

    assert waf_guard.is_frozen("https://acegua.atende.net/outra-pagina")
    assert not waf_guard.is_frozen("https://igrejinha.atende.net/transparencia")


def test_challenge_freezes_group_and_second_verdict_skips_fetch(monkeypatch):
    monkeypatch.setattr(waf_guard.socket, "gethostbyname", lambda _host: "203.0.113.77")
    session = _Session(_Resp(200, "<html><title>Concursos</title>Concursos</html>"))

    def fake_render(_url, _timeout):
        return (
            "Verificacao de seguranca",
            "Verificacao de seguranca\nSeu IP fez diversas tentativas de acessos suspeitos",
            [],
        )

    monkeypatch.setattr(Z.A, "render_page", fake_render)

    first = Z.rendered_verdict(
        session, "model", "Teste", "concursos",
        "https://one.test/concursos", 1, "authority")
    second = Z.rendered_verdict(
        session, "model", "Teste", "processos",
        "https://two.test/processos", 1, "authority")

    assert first == (
        "revisar", "revisar_op:incompleto_antibot: diagnostico conservado")
    assert second == ("revisar", "revisar_op:waf_frozen")
    assert session.calls == 1


def test_static_challenge_gets_render_chance_before_freeze(monkeypatch):
    static_challenge = (
        "<html><title>Just a moment</title>"
        "checking your browser cf-browser-verification</html>"
    )
    session = _Session(_Resp(200, static_challenge))
    renders: list[str] = []

    def fake_render(url, _timeout):
        renders.append(url)
        return (
            "Concursos",
            "Concursos\nConcurso Publico 01/2024\nConcurso Publico 02/2024",
            [],
        )

    monkeypatch.setattr(Z.A, "render_page", fake_render)
    monkeypatch.setattr(Z.C, "gemini_api_key", lambda: "fake-key")
    monkeypatch.setattr(Z, "extract_verdict",
                        lambda *_args, **_kwargs: ("confirmado", "extract_confirmar: cert=2"))

    result = Z.rendered_verdict(
        session, "model", "Teste", "concursos",
        "https://one.test/concursos", 1, "authority")

    assert result == ("confirmado", "extract_confirmar: cert=2")
    assert renders == ["https://one.test/concursos"]
    assert waf_guard.snapshot() == {}


def test_render_does_not_replace_static_listing_with_weaker_text(monkeypatch):
    static_listing = (
        "<html><title>Concursos</title>"
        "Concursos\nConcurso Publico 01/2024\nConcurso Publico 02/2024"
        "</html>"
    )
    weak_render = "Home\n" + ("Noticias\n" * 120)
    session = _Session(_Resp(200, static_listing))
    seen: dict[str, str] = {}

    monkeypatch.setattr(Z.A, "render_page", lambda *_args: ("Home", weak_render, []))
    monkeypatch.setattr(Z.C, "gemini_api_key", lambda: "fake-key")

    def fake_extract(_session, _model, _municipio, _bucket, _title, text, _anchors,
                     _timeout, *_args):
        seen["text"] = text
        return "confirmado", "extract_confirmar: cert=2"

    monkeypatch.setattr(Z, "extract_verdict", fake_extract)

    result = Z.rendered_verdict(
        session, "model", "Teste", "concursos",
        "https://one.test/concursos", 1, "authority")

    assert result == ("confirmado", "extract_confirmar: cert=2")
    assert "Concurso Publico 01/2024" in seen["text"]
    assert "Noticias" not in seen["text"]


def test_from_fixtures_avoids_fetch_and_render(monkeypatch, tmp_path):
    input_csv = tmp_path / "in.csv"
    output_csv = tmp_path / "out.csv"
    cols = [
        "municipio", "site_base",
        "url_concursos", "confianza_concursos", "tier_concursos",
        "url_processos_seletivos", "confianza_processos", "tier_processos",
        "notes",
    ]
    with input_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({
            "municipio": "Nova Palma",
            "site_base": "https://example.test",
            "url_concursos": "",
            "confianza_concursos": "",
            "tier_concursos": "",
            "url_processos_seletivos": "https://example.test/never-fetch",
            "confianza_processos": "confirmado",
            "tier_processos": "t1",
            "notes": "",
        })

    monkeypatch.setattr(Z.C, "make_session", lambda: object())
    monkeypatch.setattr(Z.C, "fetch_page",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fetch touched")))
    monkeypatch.setattr(Z.A, "render_page",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("render touched")))
    monkeypatch.setattr(Z.C, "gemini_api_key", lambda: "fake-key")
    monkeypatch.setattr(Z, "extract_verdict",
                        lambda *_args, **_kwargs: ("confirmado", "extract_confirmar: cert=2"))
    monkeypatch.setattr(sys, "argv", [
        "cierre_dataset.py",
        "--input", str(input_csv),
        "--output", str(output_csv),
        "--no-investigate",
        "--no-repair",
        "--extract-authority",
        "--from-fixtures",
    ])

    assert Z.main() == 0
    out = list(csv.DictReader(output_csv.open(encoding="utf-8")))[0]
    assert out["confianza_processos"] == "confirmado"


def test_url_cache_reuses_render_and_items_for_repeated_url(monkeypatch):
    text = "\n".join([
        "Concursos Publicos",
        "Concurso Publico 01/2024",
        "Publicado em 01/01/2024",
        "Concurso Publico 02/2024",
        "Publicado em 02/01/2024",
    ])
    session = _Session(_Resp(200, "<html><title>stub</title>stub</html>"))
    render_calls: list[str] = []
    extract_calls: list[str] = []

    def fake_render(url, _timeout):
        render_calls.append(url)
        return "Concursos Publicos", text, []

    def fake_extract(_text, *_args, **_kwargs):
        extract_calls.append(_text)
        return []

    monkeypatch.setattr(Z.A, "render_page", fake_render)
    monkeypatch.setattr(Z.C, "gemini_api_key", lambda: "fake-key")
    monkeypatch.setattr(Z.V, "extract_items", fake_extract)

    first = Z.rendered_verdict(
        session, "model", "Teste", "concursos",
        "https://one.test/indice", 1, "authority")
    second = Z.rendered_verdict(
        session, "model", "Teste", "concursos",
        "https://one.test/indice", 1, "authority")

    assert first[0] == "confirmado"
    assert second[0] == "confirmado"
    assert session.calls == 1
    assert render_calls == ["https://one.test/indice"]
    assert len(extract_calls) == 1
    assert Z._RUN_STATS["url_cache_hits"] == 1
    assert Z._RUN_STATS["url_cache_gemini_skips"] == 1


def test_waf_prefreeze_file_cuts_before_fetch(monkeypatch, tmp_path):
    input_csv = tmp_path / "in.csv"
    output_csv = tmp_path / "out.csv"
    prefreeze = tmp_path / "waf.txt"
    prefreeze.write_text("one.test\n", encoding="utf-8")
    cols = [
        "municipio", "site_base",
        "url_concursos", "confianza_concursos", "tier_concursos",
        "url_processos_seletivos", "confianza_processos", "tier_processos",
        "notes",
    ]
    with input_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({
            "municipio": "Teste",
            "site_base": "https://one.test",
            "url_concursos": "https://one.test/concursos",
            "confianza_concursos": "confirmado",
            "tier_concursos": "t1",
            "url_processos_seletivos": "",
            "confianza_processos": "",
            "tier_processos": "",
            "notes": "",
        })

    monkeypatch.setattr(Z.C, "make_session", lambda: object())
    monkeypatch.setattr(Z.C, "fetch_page",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fetch touched")))
    monkeypatch.setattr(sys, "argv", [
        "cierre_dataset.py",
        "--input", str(input_csv),
        "--output", str(output_csv),
        "--no-investigate",
        "--no-repair",
        "--extract-authority",
        "--waf-prefreeze", str(prefreeze),
    ])

    assert Z.main() == 0
    out = list(csv.DictReader(output_csv.open(encoding="utf-8")))[0]
    assert out["confianza_concursos"] == "revisar"
    assert "revisar_op:waf_frozen" in out["notes"]
