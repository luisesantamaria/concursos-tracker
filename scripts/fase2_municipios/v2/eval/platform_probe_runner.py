"""Tier 1.5 platform-probe runner: proposes candidate index URLs per CMS template.

Pure HTTP, zero AI. For every (municipio, bucket) pair this module detects the
hosting platform from ``site_base`` and tries a small, ordered list of
known-canonical template paths for that platform, accepting the first response
that passes a content gate (not a soft-404/error stub, and on-topic).

This module only PROPOSES candidate URLs. It never adjudicates a bucket as
confirmed -- that authority belongs to ``cierre_dataset.py`` / V2 (see
``PLAN_MAESTRO.md`` F3.P1 and the "arquitectura de 2 compuertas" note). Nothing
here is imported from or imports ``cascade_municipios.py`` /
``verdict_extract.py`` (both intocables); the content-gate keyword/stub lists
below are an independent, self-contained replica of the same spirit as
cascade's ``_probe_page_is_index_like``.

Two additional guards close the redirect-drift family of false proposals
found in the F3.P1 corrida 1 dictamen
(``staging/fase2_v2/eval/f3p1_probe_20260712/DICTAMEN_wrng.md``): (1) a
redirect-discipline audit in ``probe_unit`` that degrades any proposal whose
final path drifted away from the requested template path -- or landed on the
site root or an error/not-found stub -- to ``probe_result="redirect_drift"``
(never proposable); and (2) a structural-index requirement in
``classify_probe`` that an "ok" page must show at least two distinct
edital/concurso/processo-seletivo item markers (numbered entries or list/table
rows), so a single news article that merely mentions the keyword no longer
qualifies as an index.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import requests


# ---------------------------------------------------------------------------
# Platform detection + templates
# ---------------------------------------------------------------------------
BUCKETS: tuple[str, ...] = ("concurso_publico", "processo_seletivo")
PLATFORMS: tuple[str, ...] = ("atende", "elotech", "rs_gov", "otro")
MAX_TEMPLATES_PER_UNIT = 3

# Verified against the golden fixture (see
# staging/fase2_v2/eval/url_map_golden_fixture_20260712.csv, e.g. Acegua =
# atende, first concurso_publico/processo_seletivo template below).
PLATFORM_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "atende": {
        "concurso_publico": [
            "/transparencia/item/concursos-publicos",
            "/cidadao/pagina/concursos",
            "/transparencia/item/concursos-e-seletivos",
        ],
        "processo_seletivo": [
            "/transparencia/item/processos-seletivos",
            "/cidadao/pagina/processos-seletivos",
            "/transparencia/item/concursos-e-seletivos",
        ],
    },
    "rs_gov": {
        "concurso_publico": [
            "/concursos",
            "/concurso",
            "/portal-da-transparencia/concursos-publicos",
        ],
        "processo_seletivo": [
            "/processos-seletivos",
            "/processo-seletivo",
            "/concursos-e-processos-seletivos",
        ],
    },
    # Elotech (Bento) publicacao IDs are tenant-specific and can vary -- kept
    # as a single best-guess template per bucket, always emitted at confianza
    # "baja" (see PLATFORM_CONFIDENCE below).
    "elotech": {
        "concurso_publico": ["/portaltransparencia/1/publicacoes/28"],
        "processo_seletivo": ["/portaltransparencia/1/publicacoes/96"],
    },
    "otro": {},
}

PLATFORM_CONFIDENCE: dict[str, str] = {
    "atende": "alta",
    "rs_gov": "alta",
    "elotech": "baja",
    "otro": "",
}


def _host(site_base: str) -> str:
    site_base = (site_base or "").strip()
    if not site_base:
        return ""
    candidate = site_base if "://" in site_base else f"http://{site_base}"
    try:
        return (urlsplit(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def detect_platform(site_base: str) -> str:
    """Detect the CMS platform from the host of ``site_base``."""
    host = _host(site_base)
    if not host:
        return "otro"
    if host == "atende.net" or host.endswith(".atende.net"):
        return "atende"
    if "oxy.elotech" in host:
        return "elotech"
    if host == "rs.gov.br" or host.endswith(".rs.gov.br"):
        return "rs_gov"
    return "otro"


def templates_for(platform: str, bucket: str) -> list[str]:
    return list(
        PLATFORM_TEMPLATES.get(platform, {}).get(bucket, [])[:MAX_TEMPLATES_PER_UNIT]
    )


def build_template_urls(site_base: str, platform: str, bucket: str) -> list[tuple[str, str]]:
    """Return ``(template_path, full_url)`` pairs, in try-order, for a unit."""
    templates = templates_for(platform, bucket)
    if not templates:
        return []
    site_base = (site_base or "").strip()
    candidate = site_base if "://" in site_base else f"http://{site_base}"
    parsed = urlsplit(candidate)
    if not parsed.netloc:
        return []
    base = f"{parsed.scheme}://{parsed.netloc}"
    return [(template, base + template) for template in templates]


# ---------------------------------------------------------------------------
# Content gate (independent replica of cascade's probe spirit -- no import)
# ---------------------------------------------------------------------------
REJECT_TITLE_STUBS: tuple[str, ...] = (
    "nao encontrado", "nao encontrada", "404", "erro",
    "indisponivel", "acesso negado",
)
RELEVANT_KEYWORDS: tuple[str, ...] = (
    "concurso", "processo seletivo", "seletivo", "selecao",
)
SPA_SHELL_TEXT_MAX_CHARS = 500
SPA_SHELL_HTML_MIN_CHARS = 2000

# Structural index gate (F3.P1 fix, rule 2): a keyword mention alone is not
# enough for "ok" -- an index page lists multiple editais/certames, a single
# news article reports on one. Accept either >=2 prose markers like "Edital
# 001/2026" / "Concurso Publico no 02/2025", or >=2 list/table rows that each
# look like an item (keyword or number/year present in that row's text).
MIN_STRUCTURAL_MARKERS = 2
ITEM_MARKER_PATTERN = re.compile(
    r"\b(?:edital|concurso(?:\s+publico)?|processo\s+seletivo)\b[^0-9\n]{0,25}"
    r"\d{1,4}\s*/\s*\d{4}"
)
_NUMBER_YEAR_PATTERN = re.compile(r"\d{1,4}\s*/\s*\d{4}")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _has_script_src(html: str) -> bool:
    return bool(re.search(r"<script[^>]+src\s*=", html or "", re.IGNORECASE))


def extract_title_and_text(html: str) -> tuple[str, str]:
    """Parse ``<title>`` and script/style-stripped visible text from HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    if soup.title is not None:
        title = re.sub(r"\s+", " ", soup.title.get_text()).strip()
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return title, text


def _count_item_markers(normalized_text: str) -> int:
    """Count distinct "edital/concurso/processo seletivo + no/ano" markers."""
    return len(ITEM_MARKER_PATTERN.findall(normalized_text))


def _count_list_or_table_items(html: str) -> int:
    """Count ``<li>``/``<tr>`` rows that individually look like index items."""
    soup = BeautifulSoup(html or "", "html.parser")
    count = 0
    for tag in soup.find_all(["li", "tr"]):
        item_text = _norm(tag.get_text(" "))
        if not item_text:
            continue
        has_keyword = any(kw in item_text for kw in RELEVANT_KEYWORDS)
        has_number_year = bool(_NUMBER_YEAR_PATTERN.search(item_text))
        if has_keyword or has_number_year:
            count += 1
    return count


def _has_index_structure(visible_text: str, html: str) -> bool:
    """Structural content gate (F3.P1 fix, rule 2).

    A single news article that happens to mention "processo seletivo" must
    fail this gate (see the F3.P1 dictamen's Bento Goncalves case); a real
    index lists multiple editais/certames, either as prose markers ("Edital
    001/2026") or as list/table rows.
    """
    if _count_item_markers(_norm(visible_text)) >= MIN_STRUCTURAL_MARKERS:
        return True
    return _count_list_or_table_items(html) >= MIN_STRUCTURAL_MARKERS


def classify_probe(
    *, status_code: int, title: str, visible_text: str, html: str,
    is_template_exact: bool = True,
) -> str | None:
    """Return "ok", "spa_shell_probable", or None (rejected).

    Mirrors the spirit of cascade's ``_probe_page_is_index_like``: HTTP 200,
    title isn't an error/soft-404 stub, and either the title or the visible
    text mentions a relevant keyword. A thin-text/large-HTML/script-heavy
    response (JS shell typical of atende/elotech) is accepted as
    "spa_shell_probable" when the title carries the keyword OR the URL probed
    is an exact platform template (always true for this runner, since every
    URL it probes comes straight from PLATFORM_TEMPLATES).

    A non-shell "ok" additionally requires the structural index gate (F3.P1
    fix, rule 2): the keyword hit alone is not enough, the page must show
    >=2 distinct item markers (see ``_has_index_structure``). This rejects a
    single news article that merely mentions the keyword once.
    """
    if status_code != 200:
        return None
    title_n = _norm(title)
    if any(stub in title_n for stub in REJECT_TITLE_STUBS):
        return None
    text_n = _norm(visible_text)
    title_has_keyword = any(kw in title_n for kw in RELEVANT_KEYWORDS)
    text_has_keyword = any(kw in text_n for kw in RELEVANT_KEYWORDS)
    is_thin = len(visible_text.strip()) < SPA_SHELL_TEXT_MAX_CHARS
    is_large_shell = len(html or "") >= SPA_SHELL_HTML_MIN_CHARS and _has_script_src(html)
    if is_thin and is_large_shell:
        if title_has_keyword or is_template_exact:
            return "spa_shell_probable"
        return None
    if not (title_has_keyword or text_has_keyword):
        return None
    if _has_index_structure(visible_text, html):
        return "ok"
    return None


# ---------------------------------------------------------------------------
# Fetching (injectable -- keeps the offline test suite free of real sockets)
# ---------------------------------------------------------------------------
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_SLEEP_SECONDS = 0.3


class ProbeFetchError(Exception):
    """A single HTTP attempt failed (network/timeout/etc)."""

    def __init__(self, cls_name: str, message: str = "") -> None:
        super().__init__(message or cls_name)
        self.cls_name = cls_name


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    html: str
    final_url: str


class Fetcher(Protocol):
    def get(self, url: str, timeout: int) -> FetchResult: ...


class RequestsFetcher:
    """Real HTTP fetcher used in production; never touched by offline tests."""

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        })

    def get(self, url: str, timeout: int) -> FetchResult:
        try:
            response = self._session.get(url, timeout=timeout, allow_redirects=True)
        except requests.exceptions.RequestException as exc:
            raise ProbeFetchError(type(exc).__name__, str(exc)) from exc
        return FetchResult(
            status_code=response.status_code,
            html=response.text or "",
            final_url=response.url or url,
        )


# ---------------------------------------------------------------------------
# Redirect discipline (F3.P1 fix, rules 1 and 3)
# ---------------------------------------------------------------------------
# Paths that must never be proposed as an index, whether reached by redirect
# or requested directly. "" covers the bare site root (empty path).
BLOCKED_INDEX_PATHS: frozenset[str] = frozenset({
    "", "/error", "/404", "/not-found", "/pagina-nao-encontrada",
})


def _normalize_path_for_drift(path: str) -> str:
    return (path or "").strip().rstrip("/").lower()


def _normalize_host_for_drift(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[len("www."):]
    return host


@dataclass(frozen=True)
class RedirectAudit:
    path_drift: bool
    host_changed: bool
    blocked_path: bool

    @property
    def not_proposable(self) -> bool:
        """True if the final URL must never be proposed as an index."""
        return self.path_drift or self.blocked_path


def audit_redirect(requested_url: str, final_url: str) -> RedirectAudit:
    """Compare the requested template URL to where the fetch actually landed.

    Tolerates a trailing slash, a leading "www.", and scheme/case
    differences; anything else in the path is a "redirect_drift" (see the
    F3.P1 dictamen's Bento Goncalves / Pelotas / Porto Alegre cases). Landing
    on the bare site root or an error/not-found stub is blocked outright,
    drift or not.
    """
    requested = urlsplit(requested_url)
    final = urlsplit(final_url or requested_url)
    requested_path = _normalize_path_for_drift(requested.path)
    final_path = _normalize_path_for_drift(final.path)
    requested_host = _normalize_host_for_drift(requested.hostname or "")
    final_host = _normalize_host_for_drift(final.hostname or "")
    return RedirectAudit(
        path_drift=requested_path != final_path,
        host_changed=requested_host != final_host,
        blocked_path=final_path in BLOCKED_INDEX_PATHS,
    )


# ---------------------------------------------------------------------------
# Per-unit probing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProbeProposal:
    municipio: str
    bucket: str
    plataforma: str
    url_propuesta: str
    probe_result: str
    confianza: str
    template_usada: str

    def as_row(self) -> dict[str, str]:
        return {
            "municipio": self.municipio,
            "bucket": self.bucket,
            "plataforma": self.plataforma,
            "url_propuesta": self.url_propuesta,
            "probe_result": self.probe_result,
            "confianza": self.confianza,
            "template_usada": self.template_usada,
        }


def _noop_sleep(_seconds: float) -> None:
    return None


def probe_unit(
    fetcher: Fetcher, *, municipio: str, bucket: str, site_base: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ProbeProposal:
    """Probe every template for one (municipio, bucket); accept the first hit.

    A content-gate hit is only proposed if it also survives the redirect
    audit (F3.P1 fix): the final URL's path must match the requested
    template path (tolerating trailing slash / www. / scheme / case), and it
    must not be the bare site root or an error/not-found stub. A hit that
    fails this audit is recorded as ``probe_result="redirect_drift"`` and the
    runner falls through to the next template, exactly like a rejected
    content classification. ``spa_shell_probable`` additionally loses its
    high confidence (downgrades to "baja") if the redirect crossed to a
    different host, even without path drift.
    """
    platform = detect_platform(site_base)
    candidates = build_template_urls(site_base, platform, bucket)
    if not candidates:
        return ProbeProposal(
            municipio=municipio, bucket=bucket, plataforma=platform,
            url_propuesta="", probe_result="skip", confianza="", template_usada="",
        )

    last_reason = ""
    for template, url in candidates:
        try:
            result = fetcher.get(url, timeout)
        except ProbeFetchError as exc:
            last_reason = f"error:{exc.cls_name}"
            sleep_fn(sleep_seconds)
            continue
        sleep_fn(sleep_seconds)
        title, text = extract_title_and_text(result.html)
        outcome = classify_probe(
            status_code=result.status_code, title=title, visible_text=text,
            html=result.html, is_template_exact=True,
        )
        if outcome is None:
            continue
        final_url = result.final_url or url
        audit = audit_redirect(url, final_url)
        if audit.not_proposable:
            last_reason = "redirect_drift"
            continue
        confianza = PLATFORM_CONFIDENCE.get(platform, "")
        if outcome == "spa_shell_probable" and audit.host_changed:
            confianza = "baja"
        return ProbeProposal(
            municipio=municipio, bucket=bucket, plataforma=platform,
            url_propuesta=final_url, probe_result=outcome,
            confianza=confianza, template_usada=template,
        )

    return ProbeProposal(
        municipio=municipio, bucket=bucket, plataforma=platform,
        url_propuesta="", probe_result=(last_reason or "no_match"),
        confianza="", template_usada="",
    )


def run_probes(
    rows: Sequence[Mapping[str, str]], *, fetcher: Fetcher,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    limit: int | None = None,
) -> list[ProbeProposal]:
    """Probe every (municipio, bucket) pair in ``rows``; one error never stops the run."""
    if limit is not None:
        rows = list(rows)[:limit]
    proposals: list[ProbeProposal] = []
    for row in rows:
        municipio = str(row.get("municipio", "")).strip()
        site_base = str(row.get("site_base", "")).strip()
        for bucket in BUCKETS:
            proposals.append(
                probe_unit(
                    fetcher, municipio=municipio, bucket=bucket, site_base=site_base,
                    timeout=timeout, sleep_seconds=sleep_seconds, sleep_fn=sleep_fn,
                )
            )
    return proposals


# ---------------------------------------------------------------------------
# Comparison mode (--confirmed): match / host_match / wrng
# ---------------------------------------------------------------------------
def _normalize_for_compare(url: str) -> tuple[str, str, str]:
    url = (url or "").strip()
    candidate = url if "://" in url else f"http://{url}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    query = "&".join(sorted(part for part in parsed.query.split("&") if part))
    return host, path, query


def compare_result(proposed_url: str, confirmed_url: str) -> str:
    """Classify a proposal against a known-confirmed URL.

    "wrng" (proposed a URL different from the confirmed one) is the critical
    metric this whole runner is gated on -- it must stay at/near zero.
    """
    if not proposed_url:
        return "sin_propuesta"
    proposed_norm = _normalize_for_compare(proposed_url)
    confirmed_norm = _normalize_for_compare(confirmed_url)
    if proposed_norm == confirmed_norm:
        return "match"
    if proposed_norm[0] and proposed_norm[0] == confirmed_norm[0]:
        return "host_match"
    return "wrng"


def compare_against_confirmed(
    proposals: Iterable[ProbeProposal], confirmed_rows: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    confirmed_map = {
        (str(row.get("municipio", "")).strip(), str(row.get("bucket", "")).strip()):
            str(row.get("url", "")).strip()
        for row in confirmed_rows
    }
    counts: Counter[str] = Counter()
    details: list[dict[str, str]] = []
    for proposal in proposals:
        key = (proposal.municipio, proposal.bucket)
        if key not in confirmed_map:
            continue
        confirmed_url = confirmed_map[key]
        result = compare_result(proposal.url_propuesta, confirmed_url)
        counts[result] += 1
        details.append({
            "municipio": proposal.municipio,
            "bucket": proposal.bucket,
            "url_propuesta": proposal.url_propuesta,
            "url_confirmada": confirmed_url,
            "resultado": result,
        })
    return {"counts": dict(sorted(counts.items())), "details": details}


# ---------------------------------------------------------------------------
# Output: CSV of proposals + JSON summary
# ---------------------------------------------------------------------------
PROPOSAL_FIELDS: tuple[str, ...] = (
    "municipio", "bucket", "plataforma", "url_propuesta", "probe_result",
    "confianza", "template_usada",
)
ACCEPTED_RESULTS = frozenset({"ok", "spa_shell_probable"})


def build_summary(proposals: Sequence[ProbeProposal]) -> dict[str, Any]:
    total_municipios = len({p.municipio for p in proposals})
    accepted = [p for p in proposals if p.probe_result in ACCEPTED_RESULTS]
    municipios_con_propuesta = {p.municipio for p in accepted}
    propuestas_por_plataforma = Counter(p.plataforma for p in accepted)
    resultado_counts = Counter(p.probe_result for p in proposals)
    cobertura_pct = (
        round(100.0 * len(municipios_con_propuesta) / total_municipios, 2)
        if total_municipios else 0.0
    )
    return {
        "schema_version": 1,
        "total_municipios": total_municipios,
        "total_propuestas": len(accepted),
        "propuestas_por_plataforma": dict(sorted(propuestas_por_plataforma.items())),
        "resultado_counts": dict(sorted(resultado_counts.items())),
        "municipios_con_propuesta": len(municipios_con_propuesta),
        "cobertura_pct": cobertura_pct,
    }


def write_proposals_csv(path: Path, proposals: Sequence[ProbeProposal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PROPOSAL_FIELDS), lineterminator="\n")
        writer.writeheader()
        for proposal in proposals:
            writer.writerow(proposal.as_row())


def _read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tier 1.5 platform-probe runner (proposes, never confirms)",
    )
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--confirmed", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None, *, fetcher: Fetcher | None = None) -> int:
    args = _parser().parse_args(argv)
    rows = _read_csv(args.universe)
    proposals = run_probes(
        rows, fetcher=fetcher or RequestsFetcher(), timeout=args.timeout,
        sleep_seconds=args.sleep, limit=args.limit,
    )
    write_proposals_csv(args.output, proposals)
    summary = build_summary(proposals)
    if args.confirmed is not None:
        comparison = compare_against_confirmed(proposals, _read_csv(args.confirmed))
        summary["comparison"] = comparison["counts"]
        print(json.dumps(comparison["counts"], ensure_ascii=False, sort_keys=True))
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
