"""Offline tests for the SPA/anti-bot render-once fallback.

QA pre-R3 (12-jul, staging/fase2_v2/eval/fixture_qa_20260712/fixture_qa.json)
found 8/36 fixture units are client-rendered SPA shells with 45-67 chars of
served visible text (Acegua x2, Gramado x2, Bento Goncalves x2, Gravatai x2):
certifier A has nothing to cite there. ``RenderFallbackFetcher`` spends
exactly one headless render only when the plain-HTTP snapshot is objectively
unusable (SPA shell or hard anti-bot challenge), never for a generic HTTP
status or a laxly-detected challenge.

CERO Playwright/red real en este archivo: ``render_once``/``waf`` son siempre
dobles inyectados; el unico test que toca los defaults reales
(``cascade.render_page_sync``/``waf_guard``) nunca llama a ``.fetch()``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveFetchError,
    OrionHTTPFetcher,
    RenderFallbackFetcher,
    render_page_networkidle,
)
from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as fixtures
from scripts.shared import waf_guard


pytestmark = pytest.mark.offline

URL = fixtures.URL

# Shell SPA: markers de framework presentes, casi nada de contenido/links --
# espeja las 8 unidades atende.net/oxy.elotech de la clase 3 del QA (fixture
# real: 45-67 chars de texto visible servido).
SPA_SHELL_HTML = (
    '<html><head><title>Carregando...</title>'
    '<script id="__NEXT_DATA__" type="application/json">{}</script>'
    "</head><body><div id=\"app\"></div></body></html>"
)
SPA_SHELL_TEXT = "Carregando..."

# Hard marker antibot (cascade._page_from_html: dispara sin importar tamano).
HARD_ANTIBOT_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    '<body><div class="ddos-guard">checking your browser</div></body></html>'
)
HARD_ANTIBOT_TEXT = "checking your browser ddos-guard"


class FakeWaf:
    """Test double for the ``waf`` collaborator (module-shaped, not a class)."""

    def __init__(self, *, frozen: bool = False) -> None:
        self.frozen = frozen
        self.is_frozen_calls: list[str] = []
        self.freeze_calls: list[str] = []

    def is_frozen(self, url: str) -> bool:
        self.is_frozen_calls.append(url)
        return self.frozen

    def freeze(self, url: str) -> dict[str, object]:
        self.freeze_calls.append(url)
        return {"group": f"host:{url}", "count": 1, "duration_seconds": 900}


class FakeRenderer:
    """Test double for ``render_once``: records calls, never touches a browser."""

    def __init__(self, result: object = None, *, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.result


def _rendered(
    html: str,
    text: str,
    *,
    title: str = "Rendered",
    final_url: str = URL,
    status: int | None = 200,
):
    return SimpleNamespace(html=html, text=text, title=title, final_url=final_url, status=status)


def _fetcher(*, inner=None, rendered=None, render_error=None, frozen=False):
    renderer = FakeRenderer(result=rendered, error=render_error)
    waf = FakeWaf(frozen=frozen)
    fetcher = RenderFallbackFetcher(
        inner=inner if inner is not None else fixtures.FakeFetcher(),
        render_once=renderer,
        waf=waf,
    )
    return fetcher, renderer, waf


def test_clean_page_never_renders() -> None:
    # fixtures.FakeFetcher() defaults to HAPPY_HTML/HAPPY_TEXT: real content,
    # real links, no SPA/antibot markers.
    fetcher, renderer, waf = _fetcher()

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == []
    assert waf.is_frozen_calls == []
    assert result.evidence_state == "completa"
    assert result.content == fixtures.HAPPY_TEXT
    assert result.html == fixtures.HAPPY_HTML


def test_spa_shell_triggers_render_and_returns_rendered_evidence() -> None:
    inner = fixtures.FakeFetcher(
        html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT,
        raw_payload_sha256="deadbeef",
    )
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert result.evidence_state == "renderizada"
    assert result.content == fixtures.HAPPY_TEXT
    assert result.html == fixtures.HAPPY_HTML
    assert result.decode_diagnostics[-1] == "render_fallback_applied"
    # Original raw-fetch provenance is preserved, not discarded.
    assert result.raw_payload_sha256 == inner.raw_payload_sha256
    assert waf.freeze_calls == []


def test_hard_antibot_marker_triggers_render() -> None:
    inner = fixtures.FakeFetcher(html=HARD_ANTIBOT_HTML, content=HARD_ANTIBOT_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert result.evidence_state == "renderizada"
    assert waf.freeze_calls == []


def test_render_that_does_not_clear_challenge_freezes_and_keeps_original() -> None:
    inner = fixtures.FakeFetcher(html=HARD_ANTIBOT_HTML, content=HARD_ANTIBOT_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(HARD_ANTIBOT_HTML, HARD_ANTIBOT_TEXT),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert waf.freeze_calls == [URL]
    assert result.evidence_state == "completa"
    assert result.content == HARD_ANTIBOT_TEXT
    assert "render_fallback_applied" not in result.decode_diagnostics


def test_render_returning_none_keeps_original_evidence() -> None:
    inner = fixtures.FakeFetcher(html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(inner=inner, rendered=None)

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert waf.freeze_calls == []
    assert result.evidence_state == "completa"
    assert result.content == SPA_SHELL_TEXT


def test_render_raising_keeps_original_evidence() -> None:
    inner = fixtures.FakeFetcher(html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner, render_error=RuntimeError("playwright boom"),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert waf.freeze_calls == []
    assert result.evidence_state == "completa"


def test_inner_fetch_error_bubbles_without_touching_render() -> None:
    error = LiveFetchError("http_status", status_code=503)
    inner = fixtures.FakeFetcher(error=error)
    fetcher, renderer, waf = _fetcher(
        inner=inner, rendered=_rendered(fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT),
    )

    with pytest.raises(LiveFetchError) as raised:
        fetcher.fetch(URL, timeout_seconds=5.0)

    assert raised.value is error
    assert renderer.calls == []
    assert waf.is_frozen_calls == []


def test_frozen_provider_skips_render_entirely() -> None:
    inner = fixtures.FakeFetcher(html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT),
        frozen=True,
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert waf.is_frozen_calls == [URL]
    assert renderer.calls == []
    assert result.evidence_state == "completa"


def test_spa_persists_after_render_keeps_original() -> None:
    inner = fixtures.FakeFetcher(html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner, rendered=_rendered(SPA_SHELL_HTML, SPA_SHELL_TEXT),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert waf.freeze_calls == []
    assert result.evidence_state == "completa"
    assert result.content == SPA_SHELL_TEXT


def test_empty_visible_text_after_render_keeps_original() -> None:
    # SPA markers are gone from the rendered DOM (a real navigation replaced
    # the shell) but the body is still visually empty -- nothing for A to
    # cite, so this must not be accepted as a clean render either.
    clean_but_empty_html = "<html><head><title>Fixture</title></head><body></body></html>"
    inner = fixtures.FakeFetcher(html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner, rendered=_rendered(clean_but_empty_html, ""),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert waf.freeze_calls == []
    assert result.evidence_state == "completa"


def test_default_inner_and_render_once_are_the_real_collaborators() -> None:
    """Constructor defaults resolve to the real transport/renderer/waf --
    never invoked here, since ``.fetch()`` is not called in this test. El
    renderer default es la variante V2 networkidle (el wait fijo de 2000ms de
    cascade.render_page_sync devuelve body vacio en shells atende, verificado
    en vivo 12-jul)."""
    fetcher = RenderFallbackFetcher()

    assert isinstance(fetcher._inner, OrionHTTPFetcher)
    assert fetcher._render_once is render_page_networkidle
    assert fetcher._waf is waf_guard


# Shell delgado SIN markers de framework: texto visible casi nulo + bundles
# JS externos (<script src=...>) -- el caso real atende.net (207KB) y
# oxy.elotech (2.9KB, mount React) del QA: Page.is_spa es False porque no hay
# markers Next/Nuxt/React.
THIN_SHELL_HTML = (
    "<html><head><title>PREFEITURA - Concursos</title>"
    '<script src="/static/js/main.9d18c92e.chunk.js"></script></head><body>'
    "<div id=\"app\"></div><script>/*" + ("x" * 12000) + "*/</script>"
    "</body></html>"
)
THIN_SHELL_TEXT = "Carregando"


def test_thin_shell_without_framework_markers_triggers_render() -> None:
    inner = fixtures.FakeFetcher(html=THIN_SHELL_HTML, content=THIN_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert result.evidence_state == "renderizada"
    assert result.content == fixtures.HAPPY_TEXT
    assert waf.freeze_calls == []


def test_render_without_strict_text_improvement_keeps_original() -> None:
    """Un render que no aporta MAS texto visible que el shell original no
    sustituye la evidencia (fail-closed)."""
    inner = fixtures.FakeFetcher(html=THIN_SHELL_HTML, content=THIN_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(
            "<html><head><title>PREFEITURA - Concursos</title></head>"
            "<body>Carrega</body></html>",
            "Carrega",
        ),
    )

    result = fetcher.fetch(URL, timeout_seconds=5.0)

    assert renderer.calls == [URL]
    assert result.evidence_state == "completa"
    assert result.content == THIN_SHELL_TEXT
    assert waf.freeze_calls == []


def test_snapshot_evidence_state_flows_through_the_adapter() -> None:
    """Integration: LiveABCAdapter._snapshots reads FetchedEvidence.evidence_state
    (not a hardcoded 'completa'), so a render-cleaned unit reaches the
    downstream gate as 'renderizada', mirroring cascade's own contract."""
    inner = fixtures.FakeFetcher(html=SPA_SHELL_HTML, content=SPA_SHELL_TEXT)
    fetcher, renderer, waf = _fetcher(
        inner=inner, rendered=_rendered(fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT),
    )
    adapter = fixtures._adapter(fetcher=fetcher)

    outcome = adapter.request(fixtures.MUNICIPIO, fixtures.BUCKET)

    assert renderer.calls == [URL]
    assert outcome.layer is not None
    assert outcome.layer.candidate.evidence_state == "renderizada"
    assert outcome.decision == "indice_oficial"
