"""Offline contract tests for the Tier 1.5 platform-probe runner (F3.P1).

Everything here is offline: FakeFetcher never opens a socket (it satisfies the
same ``Fetcher`` protocol as the real HTTP client via dependency injection), so
these tests are compatible with the session-scoped network guard in
``scripts/fase2_municipios/v2/conftest.py``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval import platform_probe_runner as runner
from scripts.fase2_municipios.v2.eval.platform_probe_runner import (
    FetchResult,
    ProbeFetchError,
    ProbeProposal,
)


pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------
def index_html(*, keyword: str = "Concursos Públicos") -> str:
    return (
        f"<html><head><title>{keyword} - Prefeitura</title></head>"
        f"<body><main><h1>{keyword}</h1>"
        "<p>Listagem de editais e processos seletivos vigentes no município.</p>"
        "<ul><li>Edital 001/2026</li><li>Edital 002/2026</li></ul>"
        "</main></body></html>"
    )


def stub_404_html() -> str:
    return (
        "<html><head><title>Página Não Encontrada</title></head>"
        "<body><main><p>O conteúdo solicitado não está disponível.</p></main></body></html>"
    )


def blank_ok_html() -> str:
    # HTTP 200, real title, but no relevant keyword anywhere -- must be
    # rejected (this is what a generic CMS fallback/home page looks like).
    return (
        "<html><head><title>Prefeitura Municipal</title></head>"
        "<body><main><p>Bem-vindo ao portal oficial.</p></main></body></html>"
    )


def spa_shell_html(*, title: str = "Concursos Públicos") -> str:
    body = "<div id=\"app\"></div>"
    scripts = "".join(
        f'<script src="/static/js/chunk.{i}.js"></script>' for i in range(40)
    )
    padding = "<!-- " + ("x" * 2500) + " -->"
    return f"<html><head><title>{title}</title>{scripts}{padding}</head><body>{body}</body></html>"


class FakeFetcher:
    """Deterministic offline stand-in for RequestsFetcher.

    ``script`` maps URL -> FetchResult | Exception-to-raise. Calls are
    recorded so tests can assert which URLs (and in what order) were tried.
    """

    def __init__(self, script: dict[str, FetchResult | ProbeFetchError]) -> None:
        self.script = script
        self.calls: list[str] = []

    def get(self, url: str, timeout: int) -> FetchResult:
        self.calls.append(url)
        outcome = self.script.get(url)
        if outcome is None:
            raise AssertionError(f"FakeFetcher got an unscripted URL: {url}")
        if isinstance(outcome, ProbeFetchError):
            raise outcome
        return outcome


def result(html: str, *, status_code: int = 200, final_url: str = "") -> FetchResult:
    return FetchResult(status_code=status_code, html=html, final_url=final_url)


NO_SLEEP = lambda seconds: None  # noqa: E731 -- tiny local test double, no real delay


# ---------------------------------------------------------------------------
# 1. Platform detection by host
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("site_base", "expected"),
    [
        ("https://acegua.atende.net", "atende"),
        ("http://www.foo.atende.net/", "atende"),
        ("https://atende.net", "atende"),
        ("http://cliente.oxy.elotech.com.br", "elotech"),
        ("https://sub.cliente.oxy.elotech.com.br/path", "elotech"),
        ("https://www.agudo.rs.gov.br", "rs_gov"),
        ("http://acegua.rs.gov.br", "rs_gov"),
        ("https://rs.gov.br", "rs_gov"),
        ("https://sistema.sinsoft.com.br", "otro"),
        ("http://192.0.2.10", "otro"),
        ("", "otro"),
    ],
)
def test_detect_platform_by_host(site_base: str, expected: str) -> None:
    assert runner.detect_platform(site_base) == expected


# ---------------------------------------------------------------------------
# 2. Templates per platform/bucket, in order, capped at 3
# ---------------------------------------------------------------------------
def test_atende_templates_correct_order_per_bucket() -> None:
    urls = runner.build_template_urls("https://acegua.atende.net", "atende", "concurso_publico")
    assert urls == [
        ("/transparencia/item/concursos-publicos",
         "https://acegua.atende.net/transparencia/item/concursos-publicos"),
        ("/cidadao/pagina/concursos", "https://acegua.atende.net/cidadao/pagina/concursos"),
        ("/transparencia/item/concursos-e-seletivos",
         "https://acegua.atende.net/transparencia/item/concursos-e-seletivos"),
    ]
    processos = runner.build_template_urls("https://acegua.atende.net", "atende", "processo_seletivo")
    assert processos[0] == (
        "/transparencia/item/processos-seletivos",
        "https://acegua.atende.net/transparencia/item/processos-seletivos",
    )


def test_rs_gov_templates_correct_order_per_bucket() -> None:
    urls = runner.build_template_urls("https://www.agudo.rs.gov.br", "rs_gov", "concurso_publico")
    assert [t for t, _ in urls] == ["/concursos", "/concurso", "/portal-da-transparencia/concursos-publicos"]
    assert urls[0][1] == "https://www.agudo.rs.gov.br/concursos"


def test_elotech_single_low_confidence_template_per_bucket() -> None:
    urls = runner.build_template_urls(
        "https://cliente.oxy.elotech.com.br", "elotech", "processo_seletivo"
    )
    assert urls == [(
        "/portaltransparencia/1/publicacoes/96",
        "https://cliente.oxy.elotech.com.br/portaltransparencia/1/publicacoes/96",
    )]
    assert runner.PLATFORM_CONFIDENCE["elotech"] == "baja"


def test_otro_platform_has_no_templates() -> None:
    assert runner.build_template_urls("https://sistema.sinsoft.com.br", "otro", "concurso_publico") == []


def test_templates_are_capped_at_max_per_unit() -> None:
    for platform in ("atende", "rs_gov", "elotech"):
        for bucket in runner.BUCKETS:
            assert len(runner.templates_for(platform, bucket)) <= runner.MAX_TEMPLATES_PER_UNIT


# ---------------------------------------------------------------------------
# 3. Content gate rejects soft-404 stubs served with HTTP 200
# ---------------------------------------------------------------------------
def test_gate_rejects_soft_404_stub_with_status_200() -> None:
    title, text = runner.extract_title_and_text(stub_404_html())
    outcome = runner.classify_probe(
        status_code=200, title=title, visible_text=text, html=stub_404_html(),
    )
    assert outcome is None


def test_gate_rejects_generic_page_without_relevant_keyword() -> None:
    title, text = runner.extract_title_and_text(blank_ok_html())
    outcome = runner.classify_probe(
        status_code=200, title=title, visible_text=text, html=blank_ok_html(),
    )
    assert outcome is None


def test_gate_rejects_non_200_status_even_with_keyword() -> None:
    title, text = runner.extract_title_and_text(index_html())
    outcome = runner.classify_probe(
        status_code=404, title=title, visible_text=text, html=index_html(),
    )
    assert outcome is None


# ---------------------------------------------------------------------------
# 4. Content gate accepts a real page mentioning the relevant keyword
# ---------------------------------------------------------------------------
def test_gate_accepts_real_page_with_keyword_in_body() -> None:
    html = index_html()
    title, text = runner.extract_title_and_text(html)
    assert "processos seletivos" in runner._norm(text)
    outcome = runner.classify_probe(status_code=200, title=title, visible_text=text, html=html)
    assert outcome == "ok"


def test_gate_rejects_title_keyword_alone_without_structural_markers() -> None:
    """Contract change (F3.P1 fix, rule 2): a lone keyword mention -- even in
    the title -- is no longer sufficient on its own. The structural gate
    requires >=2 item markers or list/table rows; this page has exactly one
    ("Processo Seletivo 001/2026" in the title) and must now be rejected.
    """
    html = (
        "<html><head><title>Processo Seletivo 001/2026</title></head>"
        "<body><main><p>Informações administrativas gerais do município.</p></main></body></html>"
    )
    title, text = runner.extract_title_and_text(html)
    outcome = runner.classify_probe(status_code=200, title=title, visible_text=text, html=html)
    assert outcome is None


# ---------------------------------------------------------------------------
# 4b (F3.P1 fix, rule 2). Structural index gate: >=2 item markers required
# ---------------------------------------------------------------------------
def bento_news_article_html() -> str:
    # Real F3.P1 dictamen case: a single WordPress news post that mentions
    # "processo seletivo" but reports on one event, not an index of many.
    return (
        "<html><head><title>Processo Seletivo da Secretaria de Educação em Fase Final</title></head>"
        "<body><main><article>"
        "<h1>Processo Seletivo da Secretaria de Educação em Fase Final</h1>"
        "<p>A Secretaria Municipal de Educação informa que o processo seletivo "
        "simplificado está em fase final de avaliação dos candidatos inscritos.</p>"
        "<p>Publicado em 10 de julho de 2026 pela Assessoria de Imprensa.</p>"
        "</article></main></body></html>"
    )


def test_gate_rejects_single_news_article_with_keyword_but_no_index_structure() -> None:
    """F3.P1 dictamen case 1 (Bento Goncalves): a single news post mentioning
    'processo seletivo' must fail the structural gate -- an index lists
    multiple editais/certames, a news article reports on one.
    """
    html = bento_news_article_html()
    title, text = runner.extract_title_and_text(html)
    outcome = runner.classify_probe(status_code=200, title=title, visible_text=text, html=html)
    assert outcome is None


def test_gate_accepts_prose_listing_with_two_distinct_edital_markers() -> None:
    """A real index doesn't need <li>/<tr> markup: >=2 distinct "keyword +
    no/ano" mentions in plain prose is enough."""
    html = (
        "<html><head><title>Concursos e Processos Seletivos</title></head>"
        "<body><main>"
        "<p>Concurso Público nº 001/2026 - Auxiliar Administrativo - inscrições abertas.</p>"
        "<p>Concurso Público nº 002/2025 - Motorista - resultado final publicado.</p>"
        "</main></body></html>"
    )
    title, text = runner.extract_title_and_text(html)
    outcome = runner.classify_probe(status_code=200, title=title, visible_text=text, html=html)
    assert outcome == "ok"


# ---------------------------------------------------------------------------
# 5. SPA shell (thin text, big script-heavy HTML) -> spa_shell_probable
# ---------------------------------------------------------------------------
def test_gate_accepts_spa_shell_via_exact_template_even_without_title_keyword() -> None:
    html = spa_shell_html(title="Carregando...")
    title, text = runner.extract_title_and_text(html)
    assert len(text) < runner.SPA_SHELL_TEXT_MAX_CHARS
    assert len(html) >= runner.SPA_SHELL_HTML_MIN_CHARS
    outcome = runner.classify_probe(
        status_code=200, title=title, visible_text=text, html=html, is_template_exact=True,
    )
    assert outcome == "spa_shell_probable"


def test_gate_accepts_spa_shell_via_title_keyword_when_not_exact_template() -> None:
    html = spa_shell_html(title="Concursos Públicos")
    title, text = runner.extract_title_and_text(html)
    outcome = runner.classify_probe(
        status_code=200, title=title, visible_text=text, html=html, is_template_exact=False,
    )
    assert outcome == "spa_shell_probable"


def test_gate_rejects_thin_shell_without_keyword_and_without_exact_template() -> None:
    html = spa_shell_html(title="Carregando...")
    title, text = runner.extract_title_and_text(html)
    outcome = runner.classify_probe(
        status_code=200, title=title, visible_text=text, html=html, is_template_exact=False,
    )
    assert outcome is None


def test_probe_unit_end_to_end_accepts_atende_spa_shell() -> None:
    url = "https://acegua.atende.net/transparencia/item/concursos-publicos"
    fetcher = FakeFetcher({url: result(spa_shell_html(title="Carregando..."), final_url=url)})
    proposal = runner.probe_unit(
        fetcher, municipio="Aceguá", bucket="concurso_publico",
        site_base="https://acegua.atende.net", sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "spa_shell_probable"
    assert proposal.url_propuesta == url
    assert proposal.plataforma == "atende"
    assert proposal.confianza == "alta"
    assert proposal.template_usada == "/transparencia/item/concursos-publicos"
    assert fetcher.calls == [url]


# ---------------------------------------------------------------------------
# probe_unit: template fallthrough, otro skip, error resilience
# ---------------------------------------------------------------------------
def test_probe_unit_falls_through_rejected_templates_to_the_accepted_one() -> None:
    base = "https://www.agudo.rs.gov.br"
    first = f"{base}/concursos"
    second = f"{base}/concurso"
    fetcher = FakeFetcher({
        first: result(blank_ok_html()),
        second: result(index_html(), final_url=second),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Agudo", bucket="concurso_publico", site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "ok"
    assert proposal.url_propuesta == second
    assert proposal.template_usada == "/concurso"
    assert fetcher.calls == [first, second]


def test_probe_unit_no_match_when_all_templates_rejected() -> None:
    base = "https://www.agudo.rs.gov.br"
    fetcher = FakeFetcher({
        f"{base}/concursos": result(blank_ok_html()),
        f"{base}/concurso": result(stub_404_html()),
        f"{base}/portal-da-transparencia/concursos-publicos": result(blank_ok_html()),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Agudo", bucket="concurso_publico", site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "no_match"
    assert proposal.url_propuesta == ""
    assert proposal.confianza == ""
    assert len(fetcher.calls) == 3


def test_probe_unit_skips_otro_platform_without_any_fetch() -> None:
    fetcher = FakeFetcher({})
    proposal = runner.probe_unit(
        fetcher, municipio="Custom City", bucket="concurso_publico",
        site_base="https://sistema.sinsoft.com.br", sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "skip"
    assert proposal.plataforma == "otro"
    assert fetcher.calls == []


# ---------------------------------------------------------------------------
# 6b (F3.P1 fix, rules 1 and 3). Redirect discipline
# ---------------------------------------------------------------------------
def test_probe_unit_redirect_to_unrelated_news_post_is_flagged_as_redirect_drift() -> None:
    """F3.P1 dictamen case 1 (Bento Goncalves): the rs_gov '/processo-seletivo'
    template redirected (WordPress redirect-guess) to an unrelated news post
    about a different secretaria. Even served content that would otherwise
    pass the structural gate must never be proposed once its path drifted.
    """
    base = "https://www.bentogoncalves.rs.gov.br"
    first = f"{base}/processos-seletivos"
    second = f"{base}/processo-seletivo"
    third = f"{base}/concursos-e-processos-seletivos"
    drifted = f"{base}/processo-seletivo-da-secretaria-de-educacao-em-fase-final/"
    fetcher = FakeFetcher({
        first: result(stub_404_html()),
        second: result(index_html(), final_url=drifted),
        third: result(stub_404_html()),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Bento Gonçalves", bucket="processo_seletivo",
        site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "redirect_drift"
    assert proposal.url_propuesta == ""
    assert fetcher.calls == [first, second, third]


def test_probe_unit_redirect_to_site_root_is_never_proposed() -> None:
    """F3.P1 dictamen case 2 (Pelotas x2): every rs_gov template redirected to
    the bare domain root. The root must never be proposed as an index, even
    with otherwise-valid content.
    """
    base = "https://www.pelotas.rs.gov.br"
    root = "https://www.pelotas.rs.gov.br"
    fetcher = FakeFetcher({
        f"{base}/concursos": result(index_html(), final_url=root),
        f"{base}/concurso": result(index_html(), final_url=root),
        f"{base}/portal-da-transparencia/concursos-publicos": result(index_html(), final_url=root),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Pelotas", bucket="concurso_publico", site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "redirect_drift"
    assert proposal.url_propuesta == ""
    assert proposal.confianza == ""


def test_probe_unit_redirect_crossing_into_a_different_bucket_path_is_flagged() -> None:
    """F3.P1 dictamen case 3 (Porto Alegre): the PSS template redirected --
    with a host change www -> www2 -- to '/concursos/', the concurso_publico
    bucket's own template path. Cross-bucket contamination via redirect must
    never be proposed.
    """
    base = "https://www.portoalegre.rs.gov.br"
    first = f"{base}/processos-seletivos"
    second = f"{base}/processo-seletivo"
    third = f"{base}/concursos-e-processos-seletivos"
    drifted = "https://www2.portoalegre.rs.gov.br/concursos/"
    fetcher = FakeFetcher({
        first: result(stub_404_html()),
        second: result(stub_404_html()),
        third: result(index_html(), final_url=drifted),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Porto Alegre", bucket="processo_seletivo",
        site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "redirect_drift"
    assert proposal.url_propuesta == ""


def test_probe_unit_redirect_to_error_path_is_never_proposed_even_as_spa_shell() -> None:
    """F3.P1 dictamen case 4 (Porto Lucena): a rs_gov template's app shell
    redirected to /error and used to be accepted as spa_shell_probable at
    confianza=alta just because the requested URL was an exact template. A
    redirect landing on an error stub must never be proposed, at any
    confidence.
    """
    base = "https://www.portolucena.rs.gov.br"
    first = f"{base}/concursos"
    second = f"{base}/concurso"
    third = f"{base}/portal-da-transparencia/concursos-publicos"
    error_url = f"{base}/error"
    fetcher = FakeFetcher({
        first: result(spa_shell_html(title="Carregando..."), final_url=error_url),
        second: result(spa_shell_html(title="Carregando..."), final_url=error_url),
        third: result(spa_shell_html(title="Carregando..."), final_url=error_url),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Porto Lucena", bucket="concurso_publico",
        site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "redirect_drift"
    assert proposal.url_propuesta == ""
    assert proposal.confianza == ""


def test_probe_unit_benign_www_and_trailing_slash_redirect_is_still_accepted() -> None:
    """The tolerance in rule 1: a redirect that only adds/drops 'www.', a
    trailing slash, or changes scheme is benign and must still be accepted."""
    base = "https://www.agudo.rs.gov.br"
    requested = f"{base}/concursos"
    benign_final = "https://agudo.rs.gov.br/concursos/"
    fetcher = FakeFetcher({requested: result(index_html(), final_url=benign_final)})
    proposal = runner.probe_unit(
        fetcher, municipio="Agudo", bucket="concurso_publico", site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "ok"
    assert proposal.url_propuesta == benign_final
    assert proposal.confianza == "alta"


def test_probe_unit_spa_shell_confidence_downgraded_when_redirect_changes_host() -> None:
    """Rule 3: even without path drift, a spa_shell_probable classification
    following a redirect to a different host is inherently less certain and
    must not keep confianza=alta.
    """
    base = "https://acegua.atende.net"
    requested = f"{base}/transparencia/item/concursos-publicos"
    same_path_diff_host = "https://acegua2.atende.net/transparencia/item/concursos-publicos"
    fetcher = FakeFetcher({
        requested: result(spa_shell_html(title="Carregando..."), final_url=same_path_diff_host),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Aceguá", bucket="concurso_publico", site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "spa_shell_probable"
    assert proposal.confianza == "baja"
    assert proposal.url_propuesta == same_path_diff_host


def test_audit_redirect_reports_path_drift_host_change_and_blocked_path() -> None:
    requested = "https://www.agudo.rs.gov.br/concursos"
    assert runner.audit_redirect(requested, "https://agudo.rs.gov.br/concursos/") == runner.RedirectAudit(
        path_drift=False, host_changed=False, blocked_path=False,
    )
    assert runner.audit_redirect(requested, "https://agudo.rs.gov.br/outra-pagina") == runner.RedirectAudit(
        path_drift=True, host_changed=False, blocked_path=False,
    )
    assert runner.audit_redirect(requested, "https://agudo.rs.gov.br/").not_proposable
    assert runner.audit_redirect(requested, "https://agudo.rs.gov.br/error").not_proposable
    assert runner.audit_redirect(
        requested, "https://www2.agudo.rs.gov.br/concursos"
    ) == runner.RedirectAudit(path_drift=False, host_changed=True, blocked_path=False)


# ---------------------------------------------------------------------------
# 7 (spec item). Network errors are audited per unit, never crash the run
# ---------------------------------------------------------------------------
def test_network_error_on_every_template_yields_error_row_not_a_crash() -> None:
    base = "https://www.agudo.rs.gov.br"
    fetcher = FakeFetcher({
        f"{base}/concursos": ProbeFetchError("ConnectionError"),
        f"{base}/concurso": ProbeFetchError("Timeout"),
        f"{base}/portal-da-transparencia/concursos-publicos": ProbeFetchError("Timeout"),
    })
    proposal = runner.probe_unit(
        fetcher, municipio="Agudo", bucket="concurso_publico", site_base=base, sleep_fn=NO_SLEEP,
    )
    assert proposal.probe_result == "error:Timeout"  # last attempt's error class
    assert proposal.url_propuesta == ""


def test_run_probes_continues_past_a_broken_municipio_to_the_next_one() -> None:
    broken_base = "https://broken.rs.gov.br"
    ok_base = "https://www.agudo.rs.gov.br"
    fetcher = FakeFetcher({
        f"{broken_base}/concursos": ProbeFetchError("ConnectionError"),
        f"{broken_base}/concurso": ProbeFetchError("ConnectionError"),
        f"{broken_base}/portal-da-transparencia/concursos-publicos": ProbeFetchError("ConnectionError"),
        f"{broken_base}/processos-seletivos": ProbeFetchError("ConnectionError"),
        f"{broken_base}/processo-seletivo": ProbeFetchError("ConnectionError"),
        f"{broken_base}/concursos-e-processos-seletivos": ProbeFetchError("ConnectionError"),
        f"{ok_base}/concursos": result(index_html(), final_url=f"{ok_base}/concursos"),
        f"{ok_base}/processos-seletivos": result(index_html(), final_url=f"{ok_base}/processos-seletivos"),
    })
    rows = [
        {"municipio": "Broken", "site_base": broken_base},
        {"municipio": "Agudo", "site_base": ok_base},
    ]
    proposals = runner.run_probes(rows, fetcher=fetcher, sleep_fn=NO_SLEEP)

    assert len(proposals) == 4  # 2 municipios x 2 buckets, no exception raised
    broken = [p for p in proposals if p.municipio == "Broken"]
    assert all(p.probe_result == "error:ConnectionError" for p in broken)
    agudo = [p for p in proposals if p.municipio == "Agudo"]
    assert all(p.probe_result == "ok" for p in agudo)


def test_run_probes_respects_limit() -> None:
    rows = [
        {"municipio": f"M{i}", "site_base": "https://sistema.sinsoft.com.br"}
        for i in range(10)
    ]
    proposals = runner.run_probes(rows, fetcher=FakeFetcher({}), limit=3, sleep_fn=NO_SLEEP)
    assert len(proposals) == 3 * len(runner.BUCKETS)
    assert {p.municipio for p in proposals} == {"M0", "M1", "M2"}


# ---------------------------------------------------------------------------
# 6 (spec item). URL normalization in comparison mode
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("proposed", "confirmed", "expected"),
    [
        (
            "https://www.agudo.rs.gov.br/concursos/",
            "https://agudo.rs.gov.br/concursos",
            "match",
        ),
        (
            "http://agudo.rs.gov.br/concursos?ano=0&tipo=1",
            "http://agudo.rs.gov.br/concursos?tipo=1&ano=0",
            "match",
        ),
        (
            "https://agudo.rs.gov.br/processos-seletivos",
            "https://agudo.rs.gov.br/editais",
            "host_match",
        ),
        (
            "https://outro-portal.com.br/concursos",
            "https://agudo.rs.gov.br/concursos",
            "wrng",
        ),
        ("", "https://agudo.rs.gov.br/concursos", "sin_propuesta"),
    ],
)
def test_compare_result_classifies_match_host_match_and_wrng(
    proposed: str, confirmed: str, expected: str
) -> None:
    assert runner.compare_result(proposed, confirmed) == expected


def test_compare_against_confirmed_aggregates_counts_and_details() -> None:
    proposals = [
        ProbeProposal(
            municipio="Aceguá", bucket="concurso_publico", plataforma="atende",
            url_propuesta="https://acegua.atende.net/transparencia/item/concursos-publicos",
            probe_result="ok", confianza="alta",
            template_usada="/transparencia/item/concursos-publicos",
        ),
        ProbeProposal(
            municipio="Agudo", bucket="concurso_publico", plataforma="rs_gov",
            url_propuesta="https://www.agudo.rs.gov.br/concurso",
            probe_result="ok", confianza="alta", template_usada="/concurso",
        ),
        ProbeProposal(
            municipio="Semmatch", bucket="concurso_publico", plataforma="rs_gov",
            url_propuesta="", probe_result="no_match", confianza="", template_usada="",
        ),
    ]
    confirmed_rows = [
        {"municipio": "Aceguá", "bucket": "concurso_publico",
         "url": "https://acegua.atende.net/transparencia/item/concursos-publicos"},
        {"municipio": "Agudo", "bucket": "concurso_publico",
         "url": "https://www.agudo.rs.gov.br/portal-da-transparencia/concursos-publicos"},
        {"municipio": "Semmatch", "bucket": "concurso_publico",
         "url": "https://semmatch.rs.gov.br/concursos"},
    ]
    comparison = runner.compare_against_confirmed(proposals, confirmed_rows)
    assert comparison["counts"] == {"host_match": 1, "match": 1, "sin_propuesta": 1}
    assert len(comparison["details"]) == 3


def test_compare_against_confirmed_ignores_units_without_confirmed_url() -> None:
    proposals = [
        ProbeProposal(
            municipio="Nobody", bucket="concurso_publico", plataforma="rs_gov",
            url_propuesta="https://nobody.rs.gov.br/concursos",
            probe_result="ok", confianza="alta", template_usada="/concursos",
        ),
    ]
    comparison = runner.compare_against_confirmed(proposals, [])
    assert comparison["counts"] == {}
    assert comparison["details"] == []


# ---------------------------------------------------------------------------
# Summary / CSV output
# ---------------------------------------------------------------------------
def test_build_summary_computes_coverage_and_platform_breakdown() -> None:
    proposals = [
        ProbeProposal("A", "concurso_publico", "atende", "https://a/x", "ok", "alta", "/x"),
        ProbeProposal("A", "processo_seletivo", "atende", "", "no_match", "", ""),
        ProbeProposal("B", "concurso_publico", "rs_gov", "https://b/x", "spa_shell_probable", "alta", "/x"),
        ProbeProposal("B", "processo_seletivo", "rs_gov", "https://b/y", "ok", "alta", "/y"),
        ProbeProposal("C", "concurso_publico", "otro", "", "skip", "", ""),
        ProbeProposal("C", "processo_seletivo", "otro", "", "skip", "", ""),
    ]
    summary = runner.build_summary(proposals)
    assert summary["total_municipios"] == 3
    assert summary["municipios_con_propuesta"] == 2  # A (partial) and B (full)
    assert summary["propuestas_por_plataforma"] == {"atende": 1, "rs_gov": 2}
    assert summary["cobertura_pct"] == pytest.approx(round(200 / 3, 2), rel=1e-6)
    assert summary["resultado_counts"]["skip"] == 2
    assert summary["resultado_counts"]["no_match"] == 1


def test_write_proposals_csv_matches_the_spec_schema(tmp_path: Path) -> None:
    proposals = [
        ProbeProposal("A", "concurso_publico", "atende", "https://a/x", "ok", "alta", "/x"),
    ]
    out = tmp_path / "propuestas.csv"
    runner.write_proposals_csv(out, proposals)
    with out.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{
        "municipio": "A", "bucket": "concurso_publico", "plataforma": "atende",
        "url_propuesta": "https://a/x", "probe_result": "ok", "confianza": "alta",
        "template_usada": "/x",
    }]
    assert list(rows[0]) == list(runner.PROPOSAL_FIELDS)


# ---------------------------------------------------------------------------
# CLI wiring end-to-end (still offline: fetcher is injected into main())
# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_main_writes_output_and_summary_and_prints_comparison(tmp_path: Path, capsys) -> None:
    universe = tmp_path / "universe.csv"
    _write_csv(universe, [
        {"uf": "RS", "municipio": "Aceguá", "site_base": "https://acegua.atende.net"},
    ])
    confirmed = tmp_path / "confirmed.csv"
    _write_csv(confirmed, [
        {"municipio": "Aceguá", "bucket": "concurso_publico",
         "url": "https://acegua.atende.net/transparencia/item/concursos-publicos"},
    ])
    output_csv = tmp_path / "out" / "propuestas.csv"
    summary_json = tmp_path / "out" / "resumen.json"

    concurso_url = "https://acegua.atende.net/transparencia/item/concursos-publicos"
    processo_url = "https://acegua.atende.net/transparencia/item/processos-seletivos"
    fetcher = FakeFetcher({
        concurso_url: result(spa_shell_html(title="Carregando..."), final_url=concurso_url),
        processo_url: result(spa_shell_html(title="Carregando..."), final_url=processo_url),
    })

    code = runner.main(
        [
            "--universe", str(universe),
            "--output", str(output_csv),
            "--summary", str(summary_json),
            "--confirmed", str(confirmed),
            "--sleep", "0",
        ],
        fetcher=fetcher,
    )

    assert code == 0
    assert output_csv.exists()
    with output_csv.open(encoding="utf-8") as handle:
        out_rows = list(csv.DictReader(handle))
    assert len(out_rows) == 2
    assert {r["probe_result"] for r in out_rows} == {"spa_shell_probable"}

    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary["total_municipios"] == 1
    assert summary["comparison"] == {"match": 1}

    printed = capsys.readouterr().out
    assert "match" in printed


def test_cli_help_works() -> None:
    with pytest.raises(SystemExit) as raised:
        runner.main(["--help"])
    assert raised.value.code == 0
