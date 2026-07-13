"""Offline coverage for residual holdout render/XHR behavior.

No test in this module opens a browser or performs network I/O: render and
Playwright page collaborators are injected test doubles.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    RenderFallbackFetcher,
    _poll_rendered_body,
    _residual_render_trigger,
)
from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as fixtures


pytestmark = pytest.mark.offline

URL = "https://fixture.rs.gov.br/editais"
ITEM_TEXT = "Concurso P\u00fablico N\u00ba 02/2022 - 03/06/2022"
ITEM_HTML = f"<html><body><ul><li>{ITEM_TEXT}</li></ul></body></html>"


class FakeWaf:
    def is_frozen(self, url: str) -> bool:
        return False

    def freeze(self, url: str) -> None:
        raise AssertionError("clean rendered fixture must not freeze its provider")


class FakeRenderer:
    def __init__(self, rendered: object) -> None:
        self.rendered = rendered
        self.calls: list[str] = []

    def __call__(self, url: str) -> object:
        self.calls.append(url)
        return self.rendered


def _page(html: str) -> cascade.Page:
    return cascade._page_from_html(
        URL, 200, "text/html; charset=UTF-8", html, requested_url=URL,
    )


def _rendered_item() -> SimpleNamespace:
    return SimpleNamespace(
        html=ITEM_HTML,
        text=ITEM_TEXT,
        title="Editais",
        final_url=URL,
        status=200,
    )


def _fetch(html: str):
    renderer = FakeRenderer(_rendered_item())
    inner = fixtures.FakeFetcher(
        html=html,
        content=cascade.extract_text(html),
    )
    fetcher = RenderFallbackFetcher(inner=inner, render_once=renderer, waf=FakeWaf())
    return fetcher.fetch(URL, timeout_seconds=5.0), renderer


FORM_WITHOUT_ITEMS_HTML = (
    "<html><head><title>Editais</title></head><body>"
    "<form><label>Escolha a categoria de editais de Concurso P\u00fablico</label>"
    '<select name="categoria"><option>Concurso P\u00fablico</option></select>'
    "<small>Lei n\u00ba 13.019/2014</small></form>"
    '<div id="resultados"></div></body></html>'
)


def test_form_sin_items_triggers_render_and_records_diagnostic() -> None:
    result, renderer = _fetch(FORM_WITHOUT_ITEMS_HTML)

    assert renderer.calls == [URL]
    assert result.evidence_state == "renderizada"
    assert list(result.decode_diagnostics[-2:]) == [
        "render_trigger=form_sin_items",
        "render_fallback_applied",
    ]


def test_form_with_static_item_is_not_form_sin_items() -> None:
    html = FORM_WITHOUT_ITEMS_HTML.replace(
        '<div id="resultados"></div>',
        f'<div id="resultados">{ITEM_TEXT} publicado oficialmente</div>',
    )

    assert _residual_render_trigger(_page(html)) == ""
    result, renderer = _fetch(html)
    assert renderer.calls == []
    assert result.evidence_state == "completa"


MINIMAL_HTML = (
    "<html><head><title>Portal</title></head><body>Portal"
    "<!--" + ("x" * 1200) + "--></body></html>"
)


def test_snapshot_minimo_triggers_render_and_records_diagnostic() -> None:
    result, renderer = _fetch(MINIMAL_HTML)

    assert renderer.calls == [URL]
    assert result.evidence_state == "renderizada"
    assert list(result.decode_diagnostics[-2:]) == [
        "render_trigger=snapshot_minimo",
        "render_fallback_applied",
    ]


def test_snapshot_with_fifty_extracted_chars_is_not_minimal() -> None:
    text = "Portal municipal com conteudo estatico suficiente para avaliacao segura."
    html = f"<html><body>{text}</body></html>"

    assert len(text) >= 50
    assert _residual_render_trigger(_page(html)) == ""
    result, renderer = _fetch(html)
    assert renderer.calls == []
    assert result.evidence_state == "completa"


class FakeBrowserPage:
    def __init__(self, body_samples: list[str]) -> None:
        self.body_samples = body_samples
        self.sample_calls = 0
        self.waits: list[int] = []

    def locator(self, selector: str) -> "FakeBrowserPage":
        assert selector == "body"
        return self

    def inner_text(self) -> str:
        index = min(self.sample_calls, len(self.body_samples) - 1)
        self.sample_calls += 1
        return self.body_samples[index]

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


def test_post_render_poll_waits_until_item_marker_appears() -> None:
    browser_page = FakeBrowserPage([
        "Escolha a categoria Concurso P\u00fablico",
        "Escolha a categoria Concurso P\u00fablico",
        ITEM_TEXT,
    ])

    result = _poll_rendered_body(browser_page, attempts=6, interval_ms=500)

    assert result == ITEM_TEXT
    assert browser_page.sample_calls == 3
    assert browser_page.waits == [500, 500]


def test_post_render_poll_stops_when_observed_aguarde_disappears() -> None:
    browser_page = FakeBrowserPage([
        "Por favor, aguarde...",
        "Resultados carregados sem item certificavel",
    ])

    result = _poll_rendered_body(browser_page, attempts=6, interval_ms=500)

    assert result == "Resultados carregados sem item certificavel"
    assert browser_page.sample_calls == 2
    assert browser_page.waits == [500]


def test_post_render_poll_without_completion_exhausts_budget() -> None:
    browser_page = FakeBrowserPage(["Seletor de Processo Seletivo sem linhas"])

    result = _poll_rendered_body(browser_page, attempts=4, interval_ms=500)

    assert result == "Seletor de Processo Seletivo sem linhas"
    assert browser_page.sample_calls == 4
    assert browser_page.waits == [500, 500, 500, 500]
