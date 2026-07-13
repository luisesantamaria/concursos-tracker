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
    _nav_shell_render_trigger,
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


def test_render_shorter_but_with_new_item_markers_still_replaces_evidence() -> None:
    """atende.net shells verified live (12-jul, lagoabonitadosul): the raw
    static HTML duplicates its mega-menu (mobile+desktop) so it can be
    LONGER in raw chars than the render that already loaded the real
    listing. A render that is shorter but gains genuine item markers the
    original never had must still win -- content quality, not char count.
    Uses the atende_shell trigger (see the nav-only-shell fixtures below)
    since the fixture needs the host signal, not length, to fire the render."""
    shorter_but_real_text = "Processo Seletivo Simplificado Nº 01/2026"
    raw_menu_text = cascade.extract_text(ATENDE_SHELL_HTML)
    inner = fixtures.FakeFetcher(html=ATENDE_SHELL_HTML, content=raw_menu_text)
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(
            f"<html><body>{shorter_but_real_text}</body></html>",
            shorter_but_real_text,
            final_url=ATENDE_URL,
        ),
    )
    assert len(shorter_but_real_text) < len(raw_menu_text)  # sanity: render IS shorter

    result = fetcher.fetch(ATENDE_URL, timeout_seconds=5.0)

    assert renderer.calls == [ATENDE_URL]
    assert result.evidence_state == "renderizada"
    assert result.content == shorter_but_real_text
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


# ---------------------------------------------------------------------------
# Nav-only shell trigger (palanca render_interactivo, holdout 12-jul): atende.net
# mega-menus (200-450KB, thousands of chars of *menu* text) and generic
# nav-heavy pages defeat _is_thin_shell because the served text is far above
# _THIN_SHELL_MAX_TEXT_CHARS -- it just is not the listing. Fixtures mirror the
# real structure verified live against lagoabonitadosul/camponovo/estrela
# (atende.net's own "menu-item"/"menu_central" convention) and against
# chiapetta.rs.gov.br/crissiumal.rs.gov.br (real listings never regress).
# ---------------------------------------------------------------------------
ATENDE_URL = "https://fixture.atende.net/cidadao/pagina/processos-seletivos-2026"

_MEGA_MENU_ITEMS = "".join(
    f'<li><a class="menu-item" href="/cidadao/pagina/item-{i}">'
    f"SECRETARIA {i} DE ADMINISTRACAO E RECURSOS HUMANOS PUBLICACOES LEGAIS</a></li>"
    for i in range(20)
)

# (a) Shell atende.net: mega-menu (slug present as a menu entry), zero item
# markers anywhere -- the real listing is injected by JS/AJAX after boot.
ATENDE_SHELL_HTML = (
    "<html><head><title>PROCESSOS SELETIVOS 2026</title></head><body>"
    f'<nav class="menu_central"><ul>{_MEGA_MENU_ITEMS}'
    '<li><a class="menu-item" href="/cidadao/pagina/processos-seletivos-2026">'
    "PROCESSOS SELETIVOS 2026</a></li></ul></nav>"
    '<div id="conteudo-pagina"></div>'
    "</body></html>"
)

# (b) Same atende.net shell, but the served HTML already carries the real
# listing in the page body (outside the nav) -- must NOT render again.
ATENDE_WITH_LISTING_HTML = ATENDE_SHELL_HTML.replace(
    '<div id="conteudo-pagina"></div>',
    '<div id="conteudo-pagina"><ul class="lista-arquivos">'
    "<li>PROCESSO SELETIVO SIMPLIFICADO Nº 01/2026</li>"
    "<li>PROCESSO SELETIVO SIMPLIFICADO Nº 02/2026</li>"
    "</ul></div>",
)

# (d) Generic nav-heavy shell on a NON-atende host: a large persistent menu
# and a near-empty body, no item markers anywhere.
NAV_HEAVY_URL = "https://fixture.rs.gov.br/portal"
NAV_HEAVY_HTML = (
    "<html><head><title>Portal Municipal</title></head><body>"
    f'<nav class="menu-principal"><ul>{_MEGA_MENU_ITEMS}</ul></nav>'
    '<div id="rodape">Prefeitura Municipal - Todos os direitos reservados</div>'
    "</body></html>"
)


def _revalidated_page(html: str, url: str) -> cascade.Page:
    return cascade._page_from_html(
        url, 200, "text/html; charset=UTF-8", html, requested_url=url,
    )


def test_atende_shell_without_items_triggers_render() -> None:
    page = _revalidated_page(ATENDE_SHELL_HTML, ATENDE_URL)

    assert _nav_shell_render_trigger(page) == "atende_shell"


def test_atende_with_real_listing_does_not_trigger() -> None:
    page = _revalidated_page(ATENDE_WITH_LISTING_HTML, ATENDE_URL)

    assert _nav_shell_render_trigger(page) == ""


def test_normal_non_atende_page_with_items_does_not_trigger() -> None:
    # fixtures.HAPPY_HTML: real municipal page, item markers in the body,
    # no atende.net host, no nav-heavy structure.
    page = _revalidated_page(fixtures.HAPPY_HTML, URL)

    assert _nav_shell_render_trigger(page) == ""


def test_generic_nav_heavy_shell_without_items_triggers_render() -> None:
    page = _revalidated_page(NAV_HEAVY_HTML, NAV_HEAVY_URL)

    assert _nav_shell_render_trigger(page) == "nav_heavy"


def test_markers_only_inside_nav_trigger_nav_heavy_on_any_host() -> None:
    # The item vocabulary appears, but only as menu link text -- never in
    # the page body -- so it is not usable evidence either.
    html = (
        "<html><head><title>Portal</title></head><body>"
        '<nav class="menu-principal"><ul>'
        '<li><a href="/x">Processo Seletivo Nº 01/2025</a></li>'
        f"{_MEGA_MENU_ITEMS}</ul></nav>"
        '<div id="rodape">Prefeitura Municipal</div>'
        "</body></html>"
    )
    page = _revalidated_page(html, NAV_HEAVY_URL)

    assert _nav_shell_render_trigger(page) == "nav_heavy"


def test_ok_status_required_before_any_nav_signal() -> None:
    error_page = cascade.Page(url=ATENDE_URL, status=500, error="boom")

    assert _nav_shell_render_trigger(error_page) == ""


def test_atende_shell_fetcher_renders_and_tags_diagnostics() -> None:
    """Integration: RenderFallbackFetcher wires the atende_shell signal in,
    and tags render_trigger=... BEFORE the fixed render_fallback_applied
    tail (so the existing ``[-1]`` assertions elsewhere keep working)."""
    inner = fixtures.FakeFetcher(html=ATENDE_SHELL_HTML, content="menu only")
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(
            fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT, final_url=ATENDE_URL,
        ),
    )

    result = fetcher.fetch(ATENDE_URL, timeout_seconds=5.0)

    assert renderer.calls == [ATENDE_URL]
    assert result.evidence_state == "renderizada"
    assert list(result.decode_diagnostics[-2:]) == [
        "render_trigger=atende_shell", "render_fallback_applied",
    ]
    assert waf.freeze_calls == []


def test_atende_with_real_listing_fetcher_never_renders() -> None:
    inner = fixtures.FakeFetcher(
        html=ATENDE_WITH_LISTING_HTML,
        content="Processo Seletivo Simplificado n. 01/2026",
    )
    fetcher, renderer, waf = _fetcher(inner=inner)

    result = fetcher.fetch(ATENDE_URL, timeout_seconds=5.0)

    assert renderer.calls == []
    assert result.evidence_state == "completa"


def test_generic_nav_heavy_fetcher_renders_and_tags_diagnostics() -> None:
    inner = fixtures.FakeFetcher(html=NAV_HEAVY_HTML, content="menu only, no items")
    fetcher, renderer, waf = _fetcher(
        inner=inner,
        rendered=_rendered(
            fixtures.HAPPY_HTML, fixtures.HAPPY_TEXT, final_url=NAV_HEAVY_URL,
        ),
    )

    result = fetcher.fetch(NAV_HEAVY_URL, timeout_seconds=5.0)

    assert renderer.calls == [NAV_HEAVY_URL]
    assert result.evidence_state == "renderizada"
    assert list(result.decode_diagnostics[-2:]) == [
        "render_trigger=nav_heavy", "render_fallback_applied",
    ]
