from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deepsearch_municipios_a_no_ai import (
    PROJECT_ROOT,
    FIELDS,
    Page,
    clean_url,
    compact_space,
    discover_candidate_urls,
    discover_site,
    external_search,
    fetch,
    is_soft_404,
    load_municipios,
    norm,
    page_signal,
    score_resource_page,
    slugify_municipio,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


CLICK_SELECTORS = [
    "button",
    "[role=button]",
    ".navbar-toggler",
    ".menu",
    ".hamburger",
    ".dropdown-toggle",
    "text=Transparência",
    "text=Prefeitura",
    "text=Cidadão",
    "text=Servidor",
    "text=Empresa",
    "text=Mapa do Site",
]

TEXT_PATTERNS = [
    "concurso",
    "concursos",
    "processo seletivo",
    "processos seletivos",
    "seleção pública",
    "selecao publica",
    "pss",
    "edital",
]


def same_host(base: str, url: str) -> bool:
    base_host = urlparse(base).netloc.lower()
    host = urlparse(url).netloc.lower()
    return host == base_host or host.endswith("." + base_host)


def browser_links(page, base_url: str) -> list[dict[str, str]]:
    try:
        raw = page.locator("a").evaluate_all(
            """els => els.map(a => ({
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || a.textContent || a.getAttribute('aria-label') || '').trim()
            }))"""
        )
    except Exception:
        raw = []
    out = []
    for item in raw:
        href = clean_url(urljoin(base_url, item.get("href") or ""))
        if href and same_host(base_url, href):
            out.append({"href": href, "text": compact_space(item.get("text") or "")})
    return out


def click_reveal_menus(page) -> int:
    clicks = 0
    for selector in CLICK_SELECTORS:
        try:
            loc = page.locator(selector)
            count = min(loc.count(), 8)
            for idx in range(count):
                item = loc.nth(idx)
                try:
                    if item.is_visible(timeout=300):
                        label = compact_space(item.inner_text(timeout=300) if selector != "[role=button]" else "")
                        if selector in {"button", "[role=button]"} and label and not any(p in norm(label) for p in TEXT_PATTERNS + ["menu", "prefeitura", "cidadao", "servidor", "transparencia"]):
                            continue
                        item.click(timeout=600, force=True)
                        page.wait_for_timeout(250)
                        clicks += 1
                except Exception:
                    continue
        except Exception:
            continue
    return clicks


def page_text(page) -> str:
    try:
        return compact_space(page.locator("body").inner_text(timeout=1500))[:30_000]
    except Exception:
        try:
            return compact_space(page.content())[:30_000]
        except Exception:
            return ""


def soft_404_text(url: str, title: str, text: str) -> bool:
    probe = Page(url=url, status=200, title=title, text=text, links=[], elapsed_ms=0)
    return is_soft_404(probe)


def browser_probe(browser_page, url: str, timeout_ms: int) -> Page:
    start = time.perf_counter()
    try:
        response = browser_page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        browser_page.wait_for_timeout(450)
        title = browser_page.title()
        text = page_text(browser_page)
        links = browser_links(browser_page, url)
        status = response.status if response else 0
        elapsed = int((time.perf_counter() - start) * 1000)
        return Page(clean_url(browser_page.url), status, title, text, links, elapsed)
    except PlaywrightTimeoutError:
        elapsed = int((time.perf_counter() - start) * 1000)
        return Page(url, 0, "", "", [], elapsed, "timeout")
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return Page(url, 0, "", "", [], elapsed, type(exc).__name__)


def browser_candidate_urls(browser_page, home: Page, municipio: str, session, timeout: int, timeout_ms: int) -> tuple[list[str], str]:
    notes = []
    urls = discover_candidate_urls(session, home, municipio, timeout)
    home_page = browser_probe(browser_page, home.url, timeout_ms)
    if home_page.status == 200 and not soft_404_text(home_page.url, home_page.title, home_page.text):
        clicks = click_reveal_menus(browser_page)
        notes.append(f"browser_home_links={len(home_page.links)} clicks={clicks}")
        for link in browser_links(browser_page, home.url):
            blob = norm(f"{link['href']} {link['text']}")
            if any(term in blob for term in TEXT_PATTERNS + ["mapa site", "mapa do site"]):
                urls.append(link["href"])

    domain = urlparse(home.url).netloc
    for query in [
        f'site:{domain} "concursos públicos"',
        f'site:{domain} "processos seletivos"',
        f'site:{domain} "{municipio}" "processo seletivo"',
        f'site:{domain} "{municipio}" "concurso"',
    ]:
        urls.extend(external_search(session, query, timeout, 10))

    return list(dict.fromkeys([u for u in urls if u and same_host(home.url, u)]))[:180], "; ".join(notes)


def choose_resource_browser(browser_page, urls: list[str], bucket: str, timeout_ms: int) -> tuple[str, str, str, int]:
    best: Page | None = None
    best_score = -100
    checked = 0
    rejected_soft = 0
    for url in urls:
        checked += 1
        page = browser_probe(browser_page, url, timeout_ms)
        if page.status != 200 or soft_404_text(page.url, page.title, page.text):
            rejected_soft += 1
            continue
        score = score_resource_page(page, bucket)
        # Rendered pages with clickable cards often use one combined page for both buckets.
        if bucket == "processos" and "processo seletivo" in page.blob and "concurso" in page.blob:
            score += 8
        if score > best_score:
            best, best_score = page, score
        if best_score >= 75:
            break
    if best and best_score >= 35:
        return best.url, "boa", f"{bucket}_browser_score={best_score} checked={checked} soft404={rejected_soft}", best_score
    if best and best_score >= 15:
        return best.url, "revisar", f"{bucket}_browser_weak_score={best_score} checked={checked} soft404={rejected_soft}", best_score
    return "", "nao_encontrada", f"{bucket}_not_found checked={checked} soft404={rejected_soft} best_score={best_score}", best_score


def build_row(args, row: dict[str, str], browser_page, session, index: int, total: int) -> dict[str, str]:
    municipio = row["municipio"]
    checked_at = datetime.now(timezone.utc).isoformat()
    home, site_note = discover_site(session, municipio, args.timeout)
    out = {
        "uf": "RS",
        "municipio": municipio,
        "ibge": row.get("ibge", ""),
        "site_base": home.url if home else "",
        "site_status": "boa" if home else "nao_encontrado",
        "url_concursos": "",
        "status_concursos": "nao_encontrada",
        "url_processos_seletivos": "",
        "status_processos_seletivos": "nao_encontrada",
        "confidence": "0",
        "method": "browser_deepsearch_no_ai",
        "notes": site_note,
        "checked_at": checked_at,
    }
    if not home:
        print(f"[{index}/{total}] {municipio}: site=NA", flush=True)
        return out

    candidates, browser_note = browser_candidate_urls(browser_page, home, municipio, session, args.timeout, args.browser_timeout_ms)
    conc_url, conc_status, conc_note, conc_score = choose_resource_browser(browser_page, candidates, "concursos", args.browser_timeout_ms)
    pss_url, pss_status, pss_note, pss_score = choose_resource_browser(browser_page, candidates, "processos", args.browser_timeout_ms)

    out["url_concursos"] = conc_url
    out["status_concursos"] = conc_status
    out["url_processos_seletivos"] = pss_url
    out["status_processos_seletivos"] = pss_status
    confidence = 0.35 + (0.25 if conc_status == "boa" else 0) + (0.25 if pss_status == "boa" else 0)
    if conc_url and conc_url == pss_url:
        confidence += 0.10
    out["confidence"] = f"{min(confidence, 0.95):.2f}"
    out["notes"] = "; ".join([site_note, browser_note, f"candidates={len(candidates)}", conc_note, pss_note])
    print(
        f"[{index}/{total}] {municipio}: site=OK conc={conc_status} pss={pss_status} "
        f"cands={len(candidates)} conc_score={conc_score} pss_score={pss_score}",
        flush=True,
    )
    if args.debug:
        print(f"    site: {home.url}", flush=True)
        print(f"    concursos: {conc_url or '-'}", flush=True)
        print(f"    processos: {pss_url or '-'}", flush=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "municipios_resources_a_browser_no_ai.csv")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--browser-timeout-ms", type=int, default=9000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--chrome-path", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args = parser.parse_args()

    import requests

    municipios = [r for r in load_municipios(args.timeout) if norm(r["municipio"]).startswith("a")]
    municipios.sort(key=lambda r: norm(r["municipio"]))
    if args.limit:
        municipios = municipios[: args.limit]
    print(f"MUNICIPIOS_A {len(municipios)} browser_no_ai=true", flush=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; concursos-rs-browser-deepsearch/0.1)",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        }
    )

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chrome_path, headless=True)
        context = browser.new_context(locale="pt-BR", ignore_https_errors=True)
        page = context.new_page()
        for idx, row in enumerate(municipios, start=1):
            rows.append(build_row(args, row, page, session, idx, len(municipios)))
        browser.close()

    rows.sort(key=lambda r: norm(r["municipio"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"WROTE {args.output}", flush=True)
    print(
        "SUMMARY "
        f"rows={len(rows)} "
        f"site_ok={sum(1 for r in rows if r['site_base'])} "
        f"concursos_boa={sum(1 for r in rows if r['status_concursos'] == 'boa')} "
        f"processos_boa={sum(1 for r in rows if r['status_processos_seletivos'] == 'boa')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
