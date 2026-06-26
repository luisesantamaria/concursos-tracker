from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse, urlunparse

import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


AUTH_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MUNICIPIOS_URL = "https://dados.tce.rs.gov.br/dados/auxiliar/municipios.csv"

FIELDS = [
    "uf",
    "municipio",
    "ibge",
    "site_base",
    "site_status",
    "url_concursos",
    "status_concursos",
    "url_processos_seletivos",
    "status_processos_seletivos",
    "confidence",
    "method",
    "notes",
    "checked_at",
]

COMMON_PATHS = [
    "/concurso",
    "/concursos",
    "/concursos-publicos",
    "/concurso-publico",
    "/modalidade-de-concursos-publicos",
    "/processo-seletivo",
    "/processos-seletivos",
    "/processo_seletivo",
    "/processos_seletivos",
    "/selecao-publica",
    "/selecoes-publicas",
    "/editais/concursos-publicos",
    "/editais/processos-seletivos",
    "/editais/processo-seletivo",
    "/editais/concurso-publico",
    "/transparencia/concursos",
    "/transparencia/processos-seletivos",
    "/portal/concursos",
    "/portal/processos-seletivos",
    "/mapa-do-site",
]

SOFT_404_PATTERNS = [
    "nao encontramos sua pagina",
    "pagina nao encontrada",
    "pagina inexistente",
    "conteudo nao encontrado",
    "erro 404",
    "error 404",
    "not found",
    "oops",
    "esta pagina nao existe",
]

BAD_CONTEXT = [
    "licitacao",
    "licitacoes",
    "pregao",
    "compras",
    "fornecedor",
    "chamamento publico",
    "turismo",
    "soberana",
    "rainha",
    "rodeio",
]


def is_generic_publication_container(url: str) -> bool:
    route_blob = norm(unquote(urlparse(url).path))
    return any(
        signal in route_blob
        for signal in [
            "editais",
            "edital",
            "publicacoes",
            "publicacao",
            "documentos editais",
            "documentos oficiais",
            "documentacoes oficiais",
        ]
    )


def has_bucket_document_listing_signal(text: str, bucket: str) -> bool:
    blob = norm(text)
    if bucket == "processos":
        return any(
            re.search(pattern, blob)
            for pattern in [
                r"\bedital\s+de\s+processo\s+seletivo",
                r"\bprocesso\s+seletivo\s+simplificado",
                r"\bprocessos?\s+seletivos?\b",
                r"\bpss\b",
                r"\bselecoes?\s+publicas?\b",
            ]
        )
    return any(
        re.search(pattern, blob)
        for pattern in [
            r"\bedital\s+de\s+concurso\s+publico",
            r"\bconcursos?\s+publicos?\b",
            r"\bmodalidade\s+de\s+concursos?\s+publicos?\b",
        ]
    )


def compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


RELEVANT_SNIPPET_TERMS = [
    "edital de processo seletivo",
    "processo seletivo simplificado",
    "processo seletivo",
    "processos seletivos",
    "edital de concurso publico",
    "edital de concurso público",
    "concurso publico",
    "concurso público",
    "concursos publicos",
    "concursos públicos",
    "selecoes publicas",
    "seleções públicas",
    "modalidade de concursos publicos",
    "modalidade de concursos públicos",
]


def relevant_text_snippets(text: str, window: int = 650, limit: int = 14) -> str:
    """Pull useful snippets from late-rendered/embedded payloads.

    Several prefeitura sites are Next/React pages where the visible list is
    stored deep inside __NEXT_DATA__. If we keep only the first bytes, we see
    CSS and miss the actual "Edital de Processo Seletivo" cards.
    """
    if not text:
        return ""
    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    for term in RELEVANT_SNIPPET_TERMS:
        start = 0
        while len(spans) < limit:
            idx = lowered.find(term.lower(), start)
            if idx < 0:
                break
            spans.append((max(0, idx - window), min(len(text), idx + len(term) + window)))
            start = idx + len(term)
        if len(spans) >= limit:
            break
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 120:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return " ... ".join(text[start:end] for start, end in merged[:limit])


def repair_text_encoding(value: str) -> str:
    text = str(value or "")
    if not any(marker in text for marker in ("Ã", "Â", "�")):
        return text
    try:
        fixed = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text
    original_noise = sum(text.count(marker) for marker in ("Ã", "Â", "�"))
    fixed_noise = sum(fixed.count(marker) for marker in ("Ã", "Â", "�"))
    return fixed if fixed_noise < original_noise else text


def norm(value: str) -> str:
    value = repair_text_encoding(value)
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def slugify_municipio(name: str) -> str:
    value = norm(name)
    value = re.sub(r"\b(d[aeo]s?|e)\b", "", value)
    return re.sub(r"\s+", "", value)


def title_case_municipio(name: str) -> str:
    small = {"da", "de", "do", "das", "dos", "e"}
    out = []
    for part in compact_space(repair_text_encoding(name)).lower().split():
        out.append(part if part in small else part[:1].upper() + part[1:])
    return " ".join(out)


def clean_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.title_parts: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag.lower() == "a":
            href = attrs_dict.get("href")
            if href:
                self._href = href
                self._text = []
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append({"href": self._href, "text": compact_space(" ".join(self._text))})
            self._href = None
            self._text = []
        elif tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)
        if self._in_title:
            self.title_parts.append(data)


@dataclass
class Page:
    url: str
    status: int
    title: str
    text: str
    links: list[dict[str, str]]
    elapsed_ms: int
    error: str = ""

    @property
    def blob(self) -> str:
        return norm(f"{self.url} {self.title} {self.text}")


def fetch(session: requests.Session, url: str, timeout: int) -> Page:
    start = time.perf_counter()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        elapsed = int((time.perf_counter() - start) * 1000)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400:
            return Page(clean_url(response.url) or url, response.status_code, "", "", [], elapsed)
        if "text/html" not in content_type and response.status_code == 200:
            return Page(clean_url(response.url) or url, response.status_code, "", "", [], elapsed, f"non_html:{content_type}")

        raw = response.text[:500_000]
        parser = LinkParser()
        parser.feed(raw)
        page_text_full = compact_space(re.sub(r"<[^>]+>", " ", raw))
        snippets = relevant_text_snippets(page_text_full)
        page_text = compact_space(f"{snippets} {page_text_full[:20_000]}") if snippets else page_text_full[:20_000]
        return Page(
            clean_url(response.url),
            response.status_code,
            compact_space(" ".join(parser.title_parts)),
            page_text[:40_000],
            parser.links,
            elapsed,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return Page(url, 0, "", "", [], elapsed, type(exc).__name__)


def load_municipios(timeout: int) -> list[dict[str, str]]:
    response = requests.get(DEFAULT_MUNICIPIOS_URL, timeout=timeout)
    response.raise_for_status()
    rows = []
    for row in csv.DictReader(response.text.splitlines()):
        if row.get("UF") == "RS" or row.get("uf") == "RS":
            name = (
                row.get("NOME_MUNICIPIO")
                or row.get("NOME")
                or row.get("nome")
                or row.get("municipio")
                or row.get("Municipio")
                or ""
            )
            ibge = row.get("CD_MUNICIPIO_IBGE") or row.get("CD_IBGE") or row.get("ibge") or row.get("IBGE") or ""
            rows.append({"municipio": title_case_municipio(name), "ibge": ibge})
    return rows


def is_soft_404(page: Page) -> bool:
    blob = page.blob
    return page.status == 0 or any(pattern in blob for pattern in SOFT_404_PATTERNS)


def official_site_score(page: Page, municipio: str) -> int:
    if page.status != 200 or is_soft_404(page):
        return -100
    parsed = urlparse(page.url)
    host = parsed.netloc.lower()
    blob = page.blob
    muni = norm(municipio)
    slug = slugify_municipio(municipio)
    score = 0
    if host.endswith(".rs.gov.br"):
        score += 35
    if slug and slug in re.sub(r"[^a-z0-9]+", "", host):
        score += 25
    if muni in blob:
        score += 20
    if "prefeitura municipal" in blob or "municipio de" in blob:
        score += 10
    if any(x in host for x in ["facebook", "instagram", "youtube", "acheconcursos", "pciconcursos"]):
        score -= 100
    return score


def url_candidates_for_site(municipio: str) -> list[str]:
    slug = slugify_municipio(municipio)
    return [
        f"https://www.{slug}.rs.gov.br/",
        f"https://{slug}.rs.gov.br/",
        f"https://www.prefeitura{slug}.rs.gov.br/",
        f"https://prefeitura{slug}.rs.gov.br/",
    ]


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ["uddg", "u", "url", "q"]:
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if candidate.startswith("http"):
                return candidate
    return url


def parse_result_links(base_url: str, text: str) -> list[str]:
    parser = LinkParser()
    parser.feed(text)
    out = []
    for link in parser.links:
        href = urljoin(base_url, link["href"])
        href = unwrap_search_url(href)
        if href.startswith("http"):
            out.append(clean_url(href))
    return list(dict.fromkeys([x for x in out if x]))


def external_search(session: requests.Session, query: str, timeout: int, limit: int) -> list[str]:
    urls: list[str] = []
    engines = [
        ("https://duckduckgo.com/html/", {"q": query}),
        ("https://www.bing.com/search", {"q": query}),
    ]
    for endpoint, params in engines:
        try:
            response = session.get(endpoint, params=params, timeout=timeout)
            if response.status_code not in {200, 202}:
                continue
            urls.extend(parse_result_links(response.url, response.text))
            if len(urls) >= limit:
                break
        except Exception:
            continue
    filtered = []
    for url in urls:
        host = urlparse(url).netloc.lower()
        if any(bad in host for bad in ["google.", "bing.", "duckduckgo.", "facebook.", "instagram.", "youtube.", "acheconcursos.", "pciconcursos."]):
            continue
        filtered.append(url)
    return list(dict.fromkeys(filtered))[:limit]


def moved_site_candidates(page: Page) -> list[str]:
    blob = page.blob
    if not any(x in blob for x in ["mudamos", "novo site", "nova pagina", "acesse o novo", "novo portal"]):
        return []
    candidates = []
    for link in page.links:
        text_blob = norm(f"{link.get('href', '')} {link.get('text', '')}")
        if "novo" in text_blob or "acesse" in text_blob or "site" in text_blob or "portal" in text_blob:
            href = urljoin(page.url, link["href"])
            if href.startswith("http"):
                candidates.append(clean_url(href))
    return list(dict.fromkeys([x for x in candidates if x]))


def discover_site(session: requests.Session, municipio: str, timeout: int) -> tuple[Page | None, str]:
    tried: list[str] = []
    candidates = url_candidates_for_site(municipio)
    for query in [
        f'Prefeitura Municipal de "{municipio}" RS site oficial',
        f'"{municipio}" RS prefeitura concurso processo seletivo',
    ]:
        candidates.extend(external_search(session, query, timeout, 8))

    best: Page | None = None
    best_score = -100
    for url in list(dict.fromkeys(candidates)):
        if url in tried:
            continue
        tried.append(url)
        page = fetch(session, url, timeout)
        for moved in moved_site_candidates(page):
            moved_page = fetch(session, moved, timeout)
            moved_score = official_site_score(moved_page, municipio) + 15
            if moved_score > best_score:
                best, best_score = moved_page, moved_score
        score = official_site_score(page, municipio)
        if score > best_score:
            best, best_score = page, score

    if best and best_score >= 35:
        return best, f"site_score={best_score}"
    return None, f"site_not_found tried={len(tried)} best_score={best_score}"


def same_host_or_subpath(base: str, url: str) -> bool:
    base_host = urlparse(base).netloc.lower()
    host = urlparse(url).netloc.lower()
    return host == base_host or host.endswith("." + base_host)


def discover_candidate_urls(session: requests.Session, home: Page, municipio: str, timeout: int) -> list[str]:
    urls: list[str] = []
    parsed_home = urlparse(home.url)
    base = f"{parsed_home.scheme}://{parsed_home.netloc}"
    urls.extend(urljoin(base, path) for path in COMMON_PATHS)
    urls.append(home.url)

    queue = [home]
    seen_pages = {home.url}
    for depth in range(2):
        next_queue: list[Page] = []
        for page in queue:
            for link in page.links:
                href = clean_url(urljoin(page.url, link["href"]))
                if not href or not same_host_or_subpath(home.url, href):
                    continue
                blob = norm(f"{href} {link.get('text', '')}")
                if any(key in blob for key in ["concurso", "processo seletivo", "processos seletivos", "selecao", "selecoes", "mapa site", "mapa do site", "edital"]):
                    urls.append(href)
                    if href not in seen_pages and len(seen_pages) < 80:
                        child = fetch(session, href, timeout)
                        seen_pages.add(href)
                        if child.status == 200 and not is_soft_404(child):
                            next_queue.append(child)
        queue = next_queue

    domain = urlparse(home.url).netloc
    for query in [
        f"site:{domain} concurso",
        f"site:{domain} concursos publicos",
        f"site:{domain} processo seletivo",
        f"site:{domain} processos seletivos",
        f'site:{domain} "{municipio}" "concurso"',
    ]:
        urls.extend(external_search(session, query, timeout, 8))

    return list(dict.fromkeys([clean_url(u) for u in urls if clean_url(u) and same_host_or_subpath(home.url, clean_url(u))]))[:140]


def page_signal(page: Page, bucket: str) -> bool:
    if page.status != 200 or is_soft_404(page):
        return False
    blob = page.blob
    if bucket == "concursos":
        return (
            "concurso" in blob
            or "concursos publicos" in blob
            or "modalidade de concursos publicos" in blob
            or has_bucket_document_listing_signal(blob, bucket)
        )
    return any(x in blob for x in ["processo seletivo", "processos seletivos", "selecao publica", "pss"]) or has_bucket_document_listing_signal(blob, bucket)


def score_resource_page(page: Page, bucket: str) -> int:
    if not page_signal(page, bucket):
        return -100
    blob = page.blob
    path = norm(urlparse(page.url).path)
    strong_listing_signal = has_bucket_document_listing_signal(blob, bucket)
    generic_publication_container = is_generic_publication_container(page.url)
    score = 0
    if bucket == "concursos":
        if generic_publication_container and strong_listing_signal:
            score += 55
        if "concurso" in path:
            score += 40
        if "concursos publicos" in blob or "modalidade de concursos publicos" in blob:
            score += 25
    else:
        if generic_publication_container and strong_listing_signal:
            score += 55
        if "processo" in path or "seletivo" in path:
            score += 40
        if "processos seletivos" in blob or "processo seletivo" in blob:
            score += 25
        if "pss" in blob:
            score += 8
    if any(x in path for x in ["noticia", "noticias", "pages", "page"]):
        score -= 15
    if any(x in blob for x in BAD_CONTEXT) and not any(x in blob for x in ["concurso", "processo seletivo", "processos seletivos"]):
        score -= 30
    if len(urlparse(page.url).path.strip("/").split("/")) <= 2:
        score += 10
    return score


def choose_resource(session: requests.Session, urls: list[str], bucket: str, timeout: int) -> tuple[str, str, str, int]:
    best: Page | None = None
    best_score = -100
    checked = 0
    for url in urls:
        page = fetch(session, url, timeout)
        checked += 1
        score = score_resource_page(page, bucket)
        if score > best_score:
            best, best_score = page, score
    if best and best_score >= 35:
        return best.url, "boa", f"{bucket}_score={best_score} checked={checked}", best_score
    if best and best_score >= 15:
        return best.url, "revisar", f"{bucket}_weak_score={best_score} checked={checked}", best_score
    return "", "nao_encontrada", f"{bucket}_not_found checked={checked} best_score={best_score}", best_score


def build_row(args: argparse.Namespace, row: dict[str, str], index: int, total: int) -> dict[str, str]:
    municipio = row["municipio"]
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; concursos-rs-deepsearch/0.1; +research)",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        }
    )
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
        "method": "deepsearch_no_ai",
        "notes": site_note,
        "checked_at": checked_at,
    }
    if not home:
        print(f"[{index}/{total}] {municipio}: site=NA conc=NA pss=NA :: {site_note}", flush=True)
        return out

    candidates = discover_candidate_urls(session, home, municipio, args.timeout)
    conc_url, conc_status, conc_note, conc_score = choose_resource(session, candidates, "concursos", args.timeout)
    pss_url, pss_status, pss_note, pss_score = choose_resource(session, candidates, "processos", args.timeout)

    out["url_concursos"] = conc_url
    out["status_concursos"] = conc_status
    out["url_processos_seletivos"] = pss_url
    out["status_processos_seletivos"] = pss_status
    confidence = 0.35
    if conc_status == "boa":
        confidence += 0.25
    if pss_status == "boa":
        confidence += 0.25
    if conc_url and pss_url and conc_url == pss_url:
        confidence += 0.10
    out["confidence"] = f"{min(confidence, 0.95):.2f}"
    out["notes"] = "; ".join([site_note, f"candidates={len(candidates)}", conc_note, pss_note])

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
    parser.add_argument("--output", type=Path, default=AUTH_ROOT / "data" / "municipios_resources_a_deep_no_ai.csv")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    municipios = [r for r in load_municipios(args.timeout) if norm(r["municipio"]).startswith("a")]
    municipios.sort(key=lambda r: norm(r["municipio"]))
    if args.limit:
        municipios = municipios[: args.limit]
    print(f"MUNICIPIOS_A {len(municipios)} no_ai=true workers={args.workers}", flush=True)

    rows: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(build_row, args, row, idx, len(municipios)): row
            for idx, row in enumerate(municipios, start=1)
        }
        for future in as_completed(futures):
            rows.append(future.result())

    rows.sort(key=lambda r: norm(r["municipio"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"WROTE {args.output}", flush=True)

    site_ok = sum(1 for r in rows if r["site_base"])
    conc_ok = sum(1 for r in rows if r["status_concursos"] == "boa")
    pss_ok = sum(1 for r in rows if r["status_processos_seletivos"] == "boa")
    print(f"SUMMARY rows={len(rows)} site_ok={site_ok} concursos_boa={conc_ok} processos_boa={pss_ok}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
