from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote_plus, unquote, urlencode, urljoin, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deepsearch_municipios_a_no_ai import (  # noqa: E402
    DEFAULT_MUNICIPIOS_URL,
    FIELDS,
    LinkParser,
    Page,
    clean_url,
    compact_space,
    fetch,
    is_soft_404,
    load_municipios,
    norm,
    official_site_score,
    repair_text_encoding,
    slugify_municipio,
    title_case_municipio,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# <-- AGREGADO: estado, para desambiguar busquedas (evitar municipios homonimos
# de outro estado / Portugal, como senalaste).
UF_SIGLA = "RS"
UF_NOME = "Rio Grande do Sul"

# <-- AGREGADO: precios gemini-2.5-flash (USD por token) para proyectar costo.
PRICE_IN_PER_TOKEN = 0.30 / 1_000_000
PRICE_OUT_PER_TOKEN = 2.50 / 1_000_000
TOTAL_MUNICIPIOS_RS = 497  # aprox., para proyectar costo del run completo
SEARCH_CACHE: dict[str, list[dict[str, str]]] = {}

SOFT_404_TEXT = [
    "nao encontramos sua pagina",
    "pagina nao encontrada",
    "oops",
    "erro 404",
    "not found",
]

BAD_HOSTS = [
    "facebook.",
    "instagram.",
    "youtube.",
    "acheconcursos.",
    "pciconcursos.",
    "google.",
    "bing.",
    "duckduckgo.",
]

RELEVANT_TERMS = [
    "concurso",
    "concursos",
    "processo seletivo",
    "processos seletivos",
    "seleção pública",
    "selecao publica",
    "pss",
    "edital",
    "seleções públicas",
    "selecoes publicas",
    "transparencia",
    "portal da transparencia",
    "publicacoes",
]

EXTRA_RELEVANT_TERMS = [
    "editais",
    "selecao publica",
    "selecoes publicas",
    "portal transparencia",
    "recursos humanos",
    "rh",
    "carreiras",
    "servidor",
    "servidores",
    "vagas",
    "concurso publico",
    "concursos publicos",
    "trabalhe conosco",
    "nomeacao",
    "homologacao",
]

INTERMEDIATE_TERMS = [
    "publicacoes",
    "publicacoes oficiais",
    "publicacao oficial",
    "documentacoes oficiais",
    "documentacao oficial",
    "documentos oficiais",
    "editais diversos",
    "editais",
    "edital",
    "transparencia",
    "portal transparencia",
    "portal da transparencia",
    "mapa do site",
    "acesso a informacao",
    "servidor",
    "servidores",
    "recursos humanos",
    "rh",
]

SECOND_LEVEL_TERMS = [
    "recursos humanos",
    "rh",
    "editais",
    "edital",
    "publicacoes",
    "publicacoes oficiais",
    "documentacoes oficiais",
    "documentos oficiais",
    "transparencia",
    "portal transparencia",
    "portal da transparencia",
    "mapa do site",
    "servidor",
    "servidores",
    "carreiras",
]

SMART_DISCOVERY_PATHS = [
    "/concursos-publicos",
    "/concursos-publico",
    "/concurso-publico",
    "/processos-seletivos",
    "/processo-seletivo",
    "/portal-da-transparencia/concursos-publicos",
    "/portal-da-transparencia/processos-seletivos",
    "/portal-da-transparencia/concurso-publico",
    "/portal-da-transparencia/processo-seletivo",
    "/transparencia/item/concursos-publicos",
    "/transparencia/item/processos-seletivos",
]

# Frases (ya normalizadas) que indican que la prefeitura mudo de site.
# "novo endereco" cubre el caso real de Acegua ("Estamos em novo endereco").
MIGRATION_HINTS = [
    "novo site",
    "novo portal",
    "site novo",
    "novo endereco",
    "em novo endereco",
    "estamos em novo endereco",
    "site foi atualizado",
    "acesse o novo site",
    "acesse nosso novo site",
    "acesse o novo portal",
    "novo site oficial",
    "site em novo endereco",
    "migramos",
    "clique aqui para acessar",
]


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        key = os.popen(
            "powershell -NoProfile -Command \"[Environment]::GetEnvironmentVariable('GEMINI_API_KEY','User')\""
        ).read().strip()
    except Exception:
        key = ""
    return key


def parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


def safe_parse_json_object(raw: str) -> dict:
    try:
        return parse_json_object(raw)
    except Exception:
        cleaned = raw.strip()
        cleaned = re.sub(r"^.*?({)", r"\1", cleaned, flags=re.S)
        cleaned = re.sub(r"(}).*?$", r"\1", cleaned, flags=re.S)
        cleaned = cleaned.replace("None", "null").replace("True", "true").replace("False", "false")
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        return json.loads(cleaned)


def coerce_choice(choice: object) -> dict:
    """Blindaje de tipos de la respuesta de Gemini (evita crashes como
    'bool object is not subscriptable')."""
    if not isinstance(choice, dict):
        return {
            "site_base": "",
            "url_concursos": "",
            "url_processos_seletivos": "",
            "status_concursos": "nao_encontrada",
            "status_processos_seletivos": "nao_encontrada",
            "confidence": "0",
            "reason": "",
            "open_next": [],
        }

    def as_str(value: object) -> str:
        return value if isinstance(value, str) else ""

    raw_next = choice.get("open_next", [])
    if isinstance(raw_next, str):
        open_next = [raw_next]
    elif isinstance(raw_next, list):
        open_next = [u for u in raw_next if isinstance(u, str)]
    else:
        open_next = []

    confidence = choice.get("confidence", "0")
    if isinstance(confidence, bool):
        confidence = "0"
    elif isinstance(confidence, (int, float)):
        confidence = str(confidence)
    elif not isinstance(confidence, str):
        confidence = "0"

    return {
        "site_base": as_str(choice.get("site_base", "")),
        "url_concursos": as_str(choice.get("url_concursos", "")),
        "url_processos_seletivos": as_str(choice.get("url_processos_seletivos", "")),
        "status_concursos": as_str(choice.get("status_concursos", "")) or "nao_encontrada",
        "status_processos_seletivos": as_str(choice.get("status_processos_seletivos", "")) or "nao_encontrada",
        "confidence": confidence,
        "reason": as_str(choice.get("reason", "")),
        "open_next": open_next,
    }


def extract_usage(data: dict) -> dict:
    """<-- AGREGADO: tokens consumidos por una respuesta de Gemini."""
    u = (data or {}).get("usageMetadata", {}) or {}
    inp = int(u.get("promptTokenCount", 0) or 0)
    out = int(u.get("candidatesTokenCount", 0) or 0)
    total = int(u.get("totalTokenCount", 0) or 0)
    # Para 2.5 el total puede incluir thinking tokens; si total>in+out, esa
    # diferencia es pensamiento y la sumamos al output para el costo.
    if total and total > inp + out:
        out = total - inp
    if not total:
        total = inp + out
    return {"input": inp, "output": out, "total": total}


def gemini_post_with_retry(
    session: requests.Session,
    url: str,
    payload: dict,
    timeout: int,
    max_attempts: int = 4,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = session.post(url, json=payload, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"retryable_http_{response.status_code}:{response.text[:200]}")
            return response
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = (2**attempt) + random.uniform(0, 1)
                print(f"    gemini retry {attempt + 1}/{max_attempts} after {delay:.1f}s: {exc}", flush=True)
                time.sleep(delay)
    raise RuntimeError(f"gemini_post_failed_after_{max_attempts}:{last_exc}")


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ["uddg", "u", "url", "q"]:
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if candidate.startswith("http"):
                return candidate
    return url


def parse_search_links(base_url: str, html_text: str) -> list[dict[str, str]]:
    parser = LinkParser()
    parser.feed(html_text)
    out = []
    for link in parser.links:
        href = clean_url(unwrap_search_url(urljoin(base_url, link["href"])))
        text = compact_space(link.get("text", ""))
        if not href or any(bad in urlparse(href).netloc.lower() for bad in BAD_HOSTS):
            continue
        out.append({"url": href, "text": text})
    seen = set()
    deduped = []
    for item in out:
        if item["url"] not in seen:
            seen.add(item["url"])
            deduped.append(item)
    return deduped


def html_text(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return compact_space(html.unescape(value))


def dedupe_search_items(items: list[dict[str, str]], limit: int = 30) -> list[dict[str, str]]:
    seen = set()
    out = []
    for item in items:
        url = clean_url(unwrap_search_url(item.get("url", "")))
        text = compact_space(item.get("text", ""))
        host = urlparse(url).netloc.lower()
        if not url or any(bad in host for bad in BAD_HOSTS):
            continue
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "text": text})
        if len(out) >= limit:
            break
    return out


def parse_duckduckgo_results(base_url: str, html_text_raw: str) -> list[dict[str, str]]:
    out = []
    for match in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text_raw,
        flags=re.I | re.S,
    ):
        href = clean_url(urljoin(base_url, html.unescape(match.group(1))))
        text = html_text(match.group(2))
        out.append({"url": href, "text": text})
    return dedupe_search_items(out)


def parse_bing_results(base_url: str, html_text_raw: str) -> list[dict[str, str]]:
    out = []
    blocks = re.findall(r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>.*?</li>', html_text_raw, flags=re.I | re.S)
    for block in blocks:
        match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        if not match:
            continue
        href = clean_url(urljoin(base_url, html.unescape(match.group(1))))
        text = html_text(match.group(2))
        caption = html_text(" ".join(re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)))
        out.append({"url": href, "text": compact_space(f"{text} {caption}")})
    return dedupe_search_items(out)


def search_web(session: requests.Session, query: str, timeout: int, limit: int = 10) -> list[dict[str, str]]:
    cached = SEARCH_CACHE.get(query)
    if cached is not None:
        return cached[:limit]
    results: list[dict[str, str]] = []
    endpoints = [
        ("https://duckduckgo.com/html/", {"q": query}),
        ("https://www.bing.com/search", {"q": query}),
    ]
    for endpoint, params in endpoints:
        try:
            response = session.get(endpoint, params=params, timeout=timeout)
            if response.status_code not in {200, 202}:
                print(f"      search endpoint {urlparse(endpoint).netloc}: http={response.status_code} parsed=0", flush=True)
                continue
            parsed = parse_search_links(response.url, response.text)
            if "duckduckgo" in endpoint:
                parsed.extend(parse_duckduckgo_results(response.url, response.text))
            if "bing" in endpoint:
                parsed.extend(parse_bing_results(response.url, response.text))
            parsed = dedupe_search_items(parsed, limit)
            print(
                f"      search endpoint {urlparse(endpoint).netloc}: http={response.status_code} parsed={len(parsed)}",
                flush=True,
            )
            results.extend(parsed)
            results = dedupe_search_items(results, limit)
            if len(results) >= limit:
                break
        except Exception:
            print(f"      search endpoint {urlparse(endpoint).netloc}: error", flush=True)
            continue
    SEARCH_CACHE[query] = results[:limit]
    return results[:limit]


def search_result_is_promising(item: dict[str, str], municipio: str) -> bool:
    municipio = repair_text_encoding(municipio)
    blob = norm(f"{item.get('url', '')} {item.get('text', '')}")
    muni = norm(municipio)
    slug = slugify_municipio(municipio)
    has_muni = muni in blob or slug in re.sub(r"[^a-z0-9]+", "", blob)
    has_signal = any(term in blob for term in ["concurso", "processo seletivo", "processos seletivos", "selecao publica", "pss"])
    return has_muni and has_signal


def municipio_queries(municipio: str) -> list[str]:
    """<-- AGREGADO: queries con estado para desambiguar homonimos."""
    municipio = repair_text_encoding(municipio)
    municipio_ascii = compact_space(
        "".join(
            ch for ch in unicodedata.normalize("NFKD", municipio) if not unicodedata.combining(ch)
        )
    )
    names = []
    # Primero sin acentos: buscadores y sites antiguos suelen indexar asi.
    # La forma acentuada queda como respaldo solo cuando realmente difiere.
    for name in [municipio_ascii, municipio]:
        if name and name not in names:
            names.append(name)
    queries = []
    base_templates = [
        "{name} {uf} concursos prefeitura",
        "prefeitura {name} {uf_nome} concurso publico",
        "{name} {uf} processos seletivos",
        "prefeitura municipal de {name} {uf} processo seletivo",
        "{name} {uf} selecoes publicas edital",
        "{name} {uf} portal transparencia concursos",
    ]
    accent_backup_templates = [
        "{name} {uf} concursos prefeitura",
        "{name} {uf} processos seletivos",
    ]
    for idx, name in enumerate(names):
        templates = base_templates if idx == 0 else accent_backup_templates
        for template in templates:
            queries.append(template.format(name=name, uf=UF_SIGLA, uf_nome=UF_NOME))
    seen = set()
    out = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            out.append(query)
    return out


def municipio_name_variants(municipio: str) -> list[str]:
    """Return search-friendly municipality spellings, ASCII first."""
    municipio = repair_text_encoding(municipio)
    municipio_ascii = compact_space(
        "".join(
            ch for ch in unicodedata.normalize("NFKD", municipio) if not unicodedata.combining(ch)
        )
    )
    variants = []
    for name in [municipio_ascii, municipio]:
        if name and name not in variants:
            variants.append(name)
    return variants


def bucket_targeted_queries(municipio: str, bucket: str, site_base: str = "") -> list[str]:
    """Late fallback queries for one missing bucket.

    This is intentionally narrower than the initial discovery. It is used only
    after the normal crawl/Gemini path fails, so the extra search cost is paid
    only for real gaps.
    """
    host = urlparse(clean_url(site_base)).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    names = municipio_name_variants(municipio)
    templates = (
        [
            "{name} RS processo seletivo prefeitura",
            "{name} Rio Grande do Sul processo seletivo prefeitura",
            "{name} RS processos seletivos",
            "{name} RS pss prefeitura",
        ]
        if bucket == "processos"
        else [
            "{name} RS concurso prefeitura",
            "{name} Rio Grande do Sul concurso publico prefeitura",
            "{name} RS concursos publicos",
            "{name} RS edital concurso",
        ]
    )
    host_templates = (
        ["site:{host} processo seletivo", "site:{host} processos seletivos", "site:{host} pss"]
        if bucket == "processos"
        else ["site:{host} concurso", "site:{host} concursos publicos", "site:{host} edital concurso"]
    )
    queries = []
    if host:
        queries.extend(template.format(host=host) for template in host_templates)
    for name in names:
        queries.extend(template.format(name=name) for template in templates)
    seen = set()
    out = []
    for query in queries:
        query = compact_space(query)
        if query and query not in seen:
            seen.add(query)
            out.append(query)
    return out


def official_result_host_matches_municipio(url: str, municipio: str, site_base: str = "") -> bool:
    url = clean_url(url)
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host_plain = host[4:]
    else:
        host_plain = host
    base_host = urlparse(clean_url(site_base)).netloc.lower()
    base_host_plain = base_host[4:] if base_host.startswith("www.") else base_host
    if base_host_plain and (host_plain == base_host_plain or host_plain.endswith("." + base_host_plain)):
        return True
    if not (host_plain.endswith(".rs.gov.br") or host_plain.endswith(".atende.net")):
        return False
    host_slug = re.sub(r"[^a-z0-9]+", "", host_plain.split(".")[0])
    muni_slug = slugify_municipio(municipio)
    relaxed = [muni_slug]
    if muni_slug.startswith("alto") and len(muni_slug) > 4:
        relaxed.append(muni_slug.replace("alto", "", 1))
    if muni_slug.startswith("sao") and len(muni_slug) > 3:
        relaxed.append(muni_slug.replace("sao", "", 1))
    return any(slug and (slug in host_slug or host_slug in slug) for slug in relaxed)


def search_result_bucket_hint(item: dict[str, str], bucket: str) -> bool:
    blob = norm(f"{item.get('url', '')} {item.get('text', '')}")
    if bucket == "processos":
        return any(
            signal in blob
            for signal in [
                "processo seletivo",
                "processos seletivos",
                "pss",
                "selecao publica",
                "selecoes publicas",
            ]
        )
    return "concurso" in blob and not any(
        signal in blob for signal in ["processo seletivo", "processos seletivos", "pss"]
    )


def targeted_official_path_candidates(site_base: str, bucket: str) -> list[str]:
    parsed = urlparse(clean_url(site_base))
    if not parsed.scheme or not parsed.netloc:
        return []
    root = f"{parsed.scheme}://{parsed.netloc}"
    base_path = parsed.path.strip("/")
    bases = [root]
    if base_path:
        bases.insert(0, f"{root}/{base_path}")
    tails = (
        [
            "processos-seletivos",
            "processo-seletivo",
            "selecoes-publicas",
            "selecao-publica",
            "pss",
        ]
        if bucket == "processos"
        else [
            "concursos",
            "concursos-publicos",
            "concurso-publico",
            "concurso",
            "editais-de-concurso",
        ]
    )
    out = []
    seen = set()
    for base in bases:
        for tail in tails:
            url = clean_url(f"{base.rstrip('/')}/{tail}")
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def targeted_official_roots(municipio: str, site_base: str) -> list[str]:
    """Try the known site plus municipal .rs.gov.br hosts derived from name.

    Many municipalities split the main portal, transparency portal, and public
    notices across different hosts. If the first official site is an Atende or
    transparency domain, still probe the municipality's own rs.gov.br host.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        cleaned = clean_url(url)
        parsed = urlparse(cleaned)
        if not parsed.scheme or not parsed.netloc:
            return
        root = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.strip("/")
        variants = [root]
        if path:
            variants.insert(0, f"{root}/{path}")
        for variant in variants:
            if variant not in seen:
                seen.add(variant)
                out.append(variant)

    add(site_base)
    for slug in municipio_host_slugs(municipio):
        for host in (f"www.{slug}.rs.gov.br", f"{slug}.rs.gov.br"):
            add(f"https://{host}/")
            add(f"http://{host}/")
    return out


def is_strong_bucket_route_url(url: str, bucket: str) -> bool:
    route_blob = norm(unquote(f"{urlparse(url).path} {urlparse(url).query}"))
    if any(bad in route_blob for bad in ["licitacao", "licitacoes", "pregao", "compras"]):
        return False
    processo_terms = ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
    if bucket == "processos":
        return any(term in route_blob for term in processo_terms)
    return "concurso" in route_blob and not any(term in route_blob for term in processo_terms)


def best_verified_from_targeted_search(
    session: requests.Session,
    municipio: str,
    site_base: str,
    bucket: str,
    timeout: int,
    limit: int = 8,
) -> tuple[str, str]:
    seen: set[str] = set()
    blocked_candidate: tuple[str, str] = ("", "")
    for root in targeted_official_roots(municipio, site_base):
        for url in targeted_official_path_candidates(root, bucket):
            if url in seen:
                continue
            seen.add(url)
            print(f"      targeted official path {bucket}: {url}", flush=True)
            verified, note = verify_choice(session, url, bucket, timeout)
            if verified:
                return verified, f"targeted_path_{bucket}:{note}"
            if note in {"http_503", "http_429"} and is_strong_bucket_route_url(url, bucket):
                if not blocked_candidate[0]:
                    blocked_candidate = (clean_url(url), f"targeted_path_{bucket}:blocked_{note}_strong_route")
                print(f"      targeted path blocked candidate {bucket}: {note}", flush=True)
                continue
            print(f"      targeted path reject {bucket}: {note}", flush=True)
    for query in bucket_targeted_queries(municipio, bucket, site_base):
        print(f"      targeted search {bucket}: {query}", flush=True)
        results = search_web(session, query, timeout, limit=limit)
        for item in results:
            url = clean_url(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            if not official_result_host_matches_municipio(url, municipio, site_base):
                continue
            if not (search_result_is_promising(item, municipio) or search_result_bucket_hint(item, bucket)):
                continue
            print(f"      targeted open {bucket}: {url}", flush=True)
            verified, note = verify_choice(session, url, bucket, timeout)
            if verified:
                return verified, f"targeted_search_{bucket}:{note}"
            if note in {"http_503", "http_429"} and is_strong_bucket_route_url(url, bucket):
                if not blocked_candidate[0]:
                    blocked_candidate = (clean_url(url), f"targeted_search_{bucket}:blocked_{note}_strong_route")
                print(f"      targeted blocked candidate {bucket}: {note}", flush=True)
                continue
            print(f"      targeted reject {bucket}: {note}", flush=True)
    if blocked_candidate[0]:
        return blocked_candidate
    return "", f"targeted_search_{bucket}:not_found"


def visible_links(page: Page, base_url: str, limit: int = 80) -> list[dict[str, str]]:
    out = []
    strong_terms = [norm(x) for x in RELEVANT_TERMS + EXTRA_RELEVANT_TERMS]
    intermediate_terms = [norm(x) for x in INTERMEDIATE_TERMS]
    for link in page.links:
        href = clean_url(urljoin(base_url, link.get("href", "")))
        if not href:
            continue
        host = urlparse(href).netloc.lower()
        if any(bad in host for bad in BAD_HOSTS):
            continue
        text = compact_space(link.get("text", ""))
        blob = norm(f"{href} {text}")
        text_blob = norm(text)
        has_bucket_signal = any(term in blob for term in strong_terms)
        has_intermediate_signal = any(term in blob for term in intermediate_terms)
        has_button_like_context = any(
            term in text_blob
            for term in [
                "publicacoes oficiais",
                "documentacoes oficiais",
                "editais diversos",
                "concursos e selecoes publicas",
            ]
        )
        if has_bucket_signal or has_intermediate_signal or has_button_like_context:
            out.append({"url": href, "text": text})
    seen = set()
    deduped = []
    for item in out:
        if item["url"] not in seen:
            seen.add(item["url"])
            deduped.append(item)
    return deduped[:limit]


def link_priority(link: dict[str, str]) -> int:
    blob = norm(f"{link.get('url', '')} {link.get('text', '')}")
    score = 0
    if "concurso" in blob:
        score += 50
    if "processo seletivo" in blob or "processos seletivos" in blob or "pss" in blob:
        score += 45
    if "selecao publica" in blob or "selecoes publicas" in blob:
        score += 40
    if "recursos humanos" in blob or re.search(r"\brh\b", blob):
        score += 22
    if "editais" in blob or "edital" in blob:
        score += 18
    if "publicacoes oficiais" in blob or "documentacoes oficiais" in blob or "documentos oficiais" in blob:
        score += 26
    elif "publicacoes" in blob or "publicacao" in blob:
        score += 18
    if "transparencia" in blob:
        score += 14
    if "mapa do site" in blob:
        score += 12
    if "licitacao" in blob or "pregao" in blob or "compras" in blob:
        score -= 30
    return score


def is_generic_discovery_page(url: str) -> bool:
    path = urlparse(url).path.strip("/").lower()
    path = re.sub(r"/+$", "", path)
    generic_paths = {
        "concurso",
        "concursos",
        "concurso-publico",
        "concursos-publicos",
        "modalidade-de-concursos-publicos",
        "portal-da-transparencia",
        "transparencia",
        "publicacoes",
        "publicacoes-oficiais",
        "editais",
        "site",
        "site/editais",
        "site/concursos",
    }
    if path in generic_paths:
        return True
    if path.endswith("/concurso") or path.endswith("/concursos"):
        return True
    if path.endswith("/portal-da-transparencia"):
        return True
    if any(part in path for part in ["/publicacoes", "/editais", "/site/editais", "/site/concursos"]):
        return True
    return False


def is_generic_publication_container(url: str) -> bool:
    """Pages like /documentos/editais can be the right stable resource page.

    Some prefeituras group concursos and processos seletivos under a generic
    "Editais" or "Publicacoes" container. The route alone is not specific, so
    the content must carry the bucket signal before we accept it.
    """
    route_blob = norm(unquote(urlparse(url).path))
    return any(
        signal in route_blob
        for signal in [
            "editais",
            "edital",
            "publicacoes",
            "publicacao",
            "publicacoes oficiais",
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


def has_explicit_event_document_signal(text: str, bucket: str) -> bool:
    blob = norm(text)
    if bucket == "processos":
        return any(
            re.search(pattern, blob)
            for pattern in [
                r"\bedital\s+(?:n[ºo]\s*)?\d+\/\d{4}.*processo\s+seletivo",
                r"\bedital\s+de\s+processo\s+seletivo",
                r"\bprocesso\s+seletivo\s+simplificado\s+(?:n[ºo]\s*)?\d+\/\d{4}",
                r"\bpss\s+(?:n[ºo]\s*)?\d+\/\d{4}",
            ]
        )
    return any(
        re.search(pattern, blob)
        for pattern in [
            r"\bedital\s+(?:n[ºo]\s*)?\d+\/\d{4}.*concurso\s+publico",
            r"\bedital\s+de\s+concurso\s+publico",
            r"\bconcurso\s+publico\s+(?:n[ºo]\s*)?\d+\/\d{4}",
        ]
    )


def should_open_second_level(link: dict[str, str]) -> bool:
    blob = norm(f"{link.get('url', '')} {link.get('text', '')}")
    return link_priority(link) >= 12 or any(term in blob for term in SECOND_LEVEL_TERMS) or is_generic_discovery_page(link.get("url", ""))


def smart_path_candidates(site_url: str) -> list[str]:
    parsed = urlparse(site_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [clean_url(urljoin(root, path)) for path in SMART_DISCOVERY_PATHS]

    current_path = parsed.path.strip("/")
    if current_path and is_generic_discovery_page(site_url):
        parent = f"{root}/{current_path}"
        candidates.extend(clean_url(urljoin(parent + "/", tail)) for tail in ["categoria/24/processo-seletivo/", "categoria/25/concurso/"])

    seen = set()
    out = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def page_snapshot(page: Page, base_url: str, source: str = "page", discovered_text: str = "") -> dict:
    return {
        "url": page.url,
        "status": page.status,
        "title": page.title[:180],
        "soft_404": is_soft_404(page),
        "text": compact_space(page.text)[:2200],
        "relevant_links": visible_links(page, base_url, 60),
        "source": source,
        "discovered_text": compact_space(discovered_text)[:400],
    }


def municipio_host_slugs(municipio: str) -> list[str]:
    """Dominio municipal no es consistente: algunos removem 'do/de/da',
    otros mantienen (ex.: almirantetamandaredosul). Probar ambos patrones
    es general y evita depender de busca externa."""
    municipio = repair_text_encoding(municipio)
    no_particles = slugify_municipio(municipio)
    with_particles = re.sub(r"[^a-z0-9]+", "", norm(municipio))
    hyphen_with_particles = re.sub(r"[^a-z0-9]+", "-", norm(municipio)).strip("-")
    hyphen_no_particles = re.sub(r"[^a-z0-9]+", "-", re.sub(r"\b(d[aeo]s?|e)\b", "", norm(municipio))).strip("-")
    out = []
    for slug in [no_particles, with_particles, hyphen_with_particles, hyphen_no_particles]:
        if slug and slug not in out:
            out.append(slug)
    return out


def candidate_site_urls(session: requests.Session, municipio: str, timeout: int) -> list[str]:
    municipio = repair_text_encoding(municipio)
    raw = []
    for slug in municipio_host_slugs(municipio):
        raw.extend(
            [
                f"https://www.{slug}.rs.gov.br/",
                f"https://{slug}.rs.gov.br/",
                # <-- AGREGADO: variantes http. Muchos sites municipais son http-only
                # (Chrome marca "No seguro"); sin esto no se alcanza la pagina de aviso
                # de mudanza, como pasaba con Acegua.
                f"http://www.{slug}.rs.gov.br/",
                f"http://{slug}.rs.gov.br/",
                f"https://{slug}.atende.net/",
                f"https://www.pm{slug}.rs.gov.br/",
                f"https://pm{slug}.rs.gov.br/",
                f"http://www.pm{slug}.rs.gov.br/",
                f"http://pm{slug}.rs.gov.br/",
                f"https://www.prefeitura{slug}.rs.gov.br/",
                f"https://prefeitura{slug}.rs.gov.br/",
            ]
        )
    for query in municipio_queries(municipio):
        raw.extend([r["url"] for r in search_web(session, query, timeout, 8)])
    seen = set()
    out = []
    for url in raw:
        url = clean_url(url)
        host = urlparse(url).netloc.lower()
        if not url or any(bad in host for bad in BAD_HOSTS):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    # Variants with and without particles/accent normalization can easily
    # exceed 20 candidates for names such as "Almirante Tamandare do Sul".
    # Keep the broader set so the valid official host is not cut off before
    # choose_initial_site has a chance to probe it.
    return out[:40]


def follow_migration_links(
    session: requests.Session,
    probes: list[Page],
    timeout: int,
    max_follow: int = 4,
) -> list[Page]:
    """Caso 'site novo' (p.ej. Acegua -> acegua.atende.net).

    Si una pagina indica mudanza y tiene un link a OTRO dominio, abre ese
    dominio nuevo y lo devuelve como candidato. Solo se dispara cuando hay
    frases de migracion, asi que es inofensivo para municipios sin mudanza.

    Limitacion: solo capta links <a href>. Si la mudanza usa meta-refresh o
    JavaScript, no lo ve; ese caso lo resuelve la Fase 2 (grounding).
    """
    extra: list[Page] = []
    seen_hosts = {urlparse(p.url).netloc.lower() for p in probes}
    followed = 0
    for page in probes:
        if followed >= max_follow:
            break
        page_text = norm(getattr(page, "text", "") or "")
        matched = [h for h in MIGRATION_HINTS if h in page_text]
        if not matched:
            continue
        print(f"    migration hint {matched} on {page.url[:90]}", flush=True)
        for link in getattr(page, "links", []):
            href = clean_url(urljoin(page.url, link.get("href", "")))
            if not href:
                continue
            host = urlparse(href).netloc.lower()
            if not host or host in seen_hosts or any(bad in host for bad in BAD_HOSTS):
                continue
            print(f"    follow migration -> {href[:120]}", flush=True)
            new_page = fetch(session, href, timeout)
            extra.append(new_page)
            seen_hosts.add(host)
            followed += 1
            if followed >= max_follow:
                break
    return extra


def choose_initial_site(session: requests.Session, municipio: str, timeout: int) -> tuple[Page | None, list[Page]]:
    probes = []
    for url in candidate_site_urls(session, municipio, timeout):
        page = fetch(session, url, timeout)
        probes.append(page)
    # Seguir avisos de "site novo" hacia el dominio nuevo.
    migrated = follow_migration_links(session, probes, timeout)
    probes.extend(migrated)

    good_migrated = [p for p in migrated if p.status == 200 and not is_soft_404(p)]
    if good_migrated:
        print(f"    using migrated site as initial: {good_migrated[0].url[:90]}", flush=True)
        return good_migrated[0], probes

    scored = sorted(((official_site_score(p, municipio), p) for p in probes), key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= 25:
        return scored[0][1], probes

    # <-- AGREGADO: si una pagina oficial avisó mudanza y seguimos al dominio
    # nuevo, ese dominio es autoritativo aunque no sea .gov.br. Usarlo como
    # site inicial para que SI se crawlee su pagina de concursos.
    return None, probes


def gemini_pick(session: requests.Session, model: str, municipio: str, evidence: dict, timeout: int) -> tuple[dict, dict]:
    """Devuelve (choice_dict, usage_dict). usage = tokens input/output/total."""
    key = api_key()
    if not key:
        raise RuntimeError("missing GEMINI_API_KEY")
    prompt = {
        "role": "investigador_de_site_municipal_rs",
        "municipio": f"{municipio} ({UF_NOME}, {UF_SIGLA})",
        "goal": "Encontrar a pagina principal oficial da prefeitura e as paginas oficiais/estaveis de concursos publicos e processos seletivos.",
        "rules": [
            "Nao invente URLs. Escolha apenas URLs presentes em evidence.pages[*].url ou evidence.pages[*].relevant_links[*].url ou evidence.search_results[*].url.",
            "Rejeite pagina OOPS/404/nao encontrada mesmo quando HTTP=200.",
            "url_concursos deve ser uma pagina que diga concurso/concursos publicos ou seja uma pagina combinada de concursos e processos seletivos.",
            "url_processos_seletivos deve dizer processo seletivo/processos seletivos/PSS ou ser uma pagina combinada de concursos e processos seletivos.",
            "Se a mesma pagina listar concursos e processos seletivos, use a mesma URL nas duas colunas.",
            "Se a home correta tiver botao/link de concursos nos relevant_links, prefira esse link.",
            "Se o resultado de busca cair direto numa pagina de concursos/processos dentro do site oficial, use essa pagina e derive site_base do mesmo dominio oficial.",
            "Se uma pagina generica como /concurso ou /portal-da-transparencia tiver links especificos para categorias (ex.: Processo Seletivo e Concurso), use os links especificos nas colunas finais, nao a pagina generica.",
            "Nao coloque a mesma URL para concursos e processos seletivos quando evidence mostrar paginas separadas mais especificas para cada categoria.",
            "Links de busca oficiais como /transparencia/item/processos-seletivos, /portal-da-transparencia/processos-seletivos, /pg.php?...subarea=18 ou /concurso/categoria/.../processo-seletivo sao evidencias fortes mesmo se o texto renderizado vier pobre por JavaScript.",
            "Se o site oficial migrou (ex.: 'estamos em novo endereco'), use o dominio novo apontado pela propria prefeitura como site oficial.",
            "Portal da transparencia pode ser usado como caminho intermediario; so use como url final se a propria pagina listar concursos/processos seletivos.",
            "Nao use licitacoes, pregao, compras, fornecedor ou chamamento publico como concursos/processos.",
            "open_next deve ser SEMPRE uma lista de URLs (pode ser lista vazia []), nunca um booleano.",
            "Mantenha reason curto, com no maximo 30 palavras.",
            "Responda somente JSON valido.",
        ],
        "schema": {
            "site_base": "url ou vazio",
            "url_concursos": "url ou vazio",
            "url_processos_seletivos": "url ou vazio",
            "status_concursos": "boa|revisar|nao_encontrada",
            "status_processos_seletivos": "boa|revisar|nao_encontrada",
            "confidence": "0-1",
            "reason": "curto, explique a evidencia",
            "open_next": ["urls opcionais de evidence para abrir se precisar de segunda volta"],
        },
        "evidence": evidence,
    }
    payload = {
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096, "responseMimeType": "application/json"},
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={key}"
    response = gemini_post_with_retry(session, url, payload, timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"gemini_http_{response.status_code}:{response.text[:240]}")
    data = response.json()
    usage = extract_usage(data)
    text = "\n".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
    try:
        return safe_parse_json_object(text), usage
    except Exception:
        repair_prompt = {
            "task": "Converta a resposta abaixo em JSON valido, sem markdown e sem texto extra.",
            "required_keys": [
                "site_base",
                "url_concursos",
                "url_processos_seletivos",
                "status_concursos",
                "status_processos_seletivos",
                "confidence",
                "reason",
                "open_next",
            ],
            "bad_response": text,
        }
        repair_payload = {
            "contents": [{"role": "user", "parts": [{"text": json.dumps(repair_prompt, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1200, "responseMimeType": "application/json"},
        }
        repaired = gemini_post_with_retry(session, url, repair_payload, timeout)
        if repaired.status_code >= 400:
            raise RuntimeError(f"gemini_repair_http_{repaired.status_code}:{repaired.text[:240]}")
        repair_data = repaired.json()
        repair_usage = extract_usage(repair_data)
        usage = {k: usage[k] + repair_usage[k] for k in usage}
        repair_text = "\n".join(part.get("text", "") for part in repair_data["candidates"][0]["content"]["parts"])
        return safe_parse_json_object(repair_text), usage


def allowed_urls(evidence: dict) -> set[str]:
    urls = set()
    for item in evidence.get("search_results", []):
        urls.add(clean_url(item.get("url", "")))
    for item in evidence.get("generated_candidates", []):
        urls.add(clean_url(item.get("url", "")))
    for page in evidence.get("pages", []):
        urls.add(clean_url(page.get("url", "")))
        for link in page.get("relevant_links", []):
            urls.add(clean_url(link.get("url", "")))
    return {u for u in urls if u}


def is_broad_landing_url(url: str) -> bool:
    parsed = urlparse(clean_url(url))
    path = (parsed.path or "/").strip("/").lower()
    return path in {"", "web", "home", "inicio", "index.php"}


def verify_choice(session: requests.Session, url: str, bucket: str, timeout: int) -> tuple[str, str]:
    if not url:
        return "", "empty"
    page = fetch(session, url, timeout)
    if page.status != 200:
        return "", f"http_{page.status}"
    if is_soft_404(page):
        return "", "soft_404"
    blob = page.blob
    requested = clean_url(url)
    final_url = clean_url(page.url)
    if is_broad_landing_url(final_url) and not has_explicit_event_document_signal(blob, bucket):
        if requested != final_url:
            return "", "redirected_to_broad_landing"
        return "", "broad_landing_without_explicit_event"
    if bucket == "concursos" and "concurso" not in blob:
        if not has_bucket_document_listing_signal(blob, bucket):
            return "", "missing_concurso_text"
    if bucket == "processos" and not any(
        x in blob for x in ["processo seletivo", "processos seletivos", "pss", "selecao publica", "selecoes publicas"]
    ):
        if not has_bucket_document_listing_signal(blob, bucket) and "concurso" not in blob:
            return "", "missing_processo_text"
        return clean_url(page.url), "verified_combined"
    return clean_url(page.url), "verified"


def normalize_resource_url(url: str, bucket: str) -> tuple[str, str]:
    """Prefer stable all-year resource list URLs when the site supports them."""
    url = clean_url(url)
    parsed = urlparse(url)
    path = parsed.path.lower()

    # Água Santa uses the same resource page with ano=2026 by default. ano=0
    # shows the all-years list and is the better stable resource URL.
    if path.endswith("/pg.php"):
        qs = parse_qs(parsed.query, keep_blank_values=True)
        area = (qs.get("area", [""])[0] or "").upper()
        subarea = qs.get("subarea", [""])[0] or ""
        has_specific_publication = bool(qs.get("id_pub", [""])[0] or "")
        if area == "PUBLICACOES" and subarea and not has_specific_publication and (qs.get("ano", [""])[0] or "") != "0":
            params = []
            saw_ano = False
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                if key.lower() == "ano":
                    params.append((key, "0"))
                    saw_ano = True
                else:
                    params.append((key, value))
            if not saw_ano:
                params.append(("ano", "0"))
            return clean_url(parsed._replace(query=urlencode(params)).geturl()), "normalized_all_year_ano_0"

    return url, ""


def normalize_verified_resource_url(
    session: requests.Session, url: str, bucket: str, timeout: int
) -> tuple[str, str]:
    normalized, note = normalize_resource_url(url, bucket)
    if not note or normalized == url:
        return url, ""
    verified, verify_note = verify_choice(session, normalized, bucket, timeout)
    if verified:
        return verified, f"{note}:{verify_note}"
    return url, f"{note}_failed:{verify_note}"


def evidence_candidates(evidence: dict) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for item in evidence.get("search_results", []):
        candidates.append(
            {
                "url": clean_url(item.get("url", "")),
                "text": compact_space(item.get("text", "")),
                "source": "search",
                "generated": "0",
            }
        )
    for item in evidence.get("generated_candidates", []):
        candidates.append(
            {
                "url": clean_url(item.get("url", "")),
                "text": compact_space(item.get("text", "")),
                "source": compact_space(item.get("source", "generated_candidate")) or "generated_candidate",
                "generated": "1",
            }
        )
    for page in evidence.get("pages", []):
        page_url = clean_url(page.get("url", ""))
        source = str(page.get("source", "page"))
        page_context = compact_space(
            f"{page.get('discovered_text', '')} {page.get('title', '')} {page.get('text', '')[:900]}"
        )
        generated = "1" if source in {"smart_path", "smart_child"} else "0"
        candidates.append({"url": page_url, "text": page_context, "source": source, "generated": generated})
        for link in page.get("relevant_links", []):
            candidates.append(
                {
                    "url": clean_url(link.get("url", "")),
                    "text": compact_space(f"{link.get('text', '')} {page.get('title', '')}"),
                    "source": "link",
                    "generated": "0",
                }
            )

    merged: dict[str, dict[str, str]] = {}
    for item in candidates:
        url = item.get("url", "")
        if not url:
            continue
        if url not in merged:
            merged[url] = dict(item)
            continue
        previous = merged[url]
        previous["text"] = compact_space(f"{previous.get('text', '')} {item.get('text', '')}")[:5000]
        previous_sources = set(filter(None, previous.get("source", "").split("+")))
        previous_sources.add(item.get("source", ""))
        previous["source"] = "+".join(sorted(source for source in previous_sources if source))[:120]
        if item.get("generated") == "0":
            previous["generated"] = "0"
    return list(merged.values())


def has_visible_bucket_signal(item: dict[str, str], bucket: str) -> bool:
    """Require human-visible context for generated smart paths.

    Smart paths are useful for discovery, but they are guesses. To promote a
    guessed URL we need the fetched page or the discovered link text to confirm
    the bucket. This prevents false positives like accepting a nonexistent
    Acegua concursos page only because the URL pattern looks plausible.
    """
    text_blob = norm(item.get("text", ""))
    if has_bucket_document_listing_signal(item.get("text", ""), bucket):
        return True
    if bucket == "processos":
        return any(
            signal in text_blob
            for signal in [
                "processo seletivo",
                "processos seletivos",
                "pss",
                "selecao publica",
                "selecoes publicas",
            ]
        )
    return ("concurso publico" in text_blob or "concursos publicos" in text_blob or "concurso" in text_blob) and not any(
        signal in text_blob for signal in ["processo seletivo", "processos seletivos", "pss"]
    )


def is_resource_list_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    full = url.lower()
    if path.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")):
        return False
    if ".pdf" in full or ".docx" in full or ".xlsx" in full:
        return False
    if any(part in path for part in ["/detalhe/", "/download/", "/comprovante/", "/lerarquivo/"]):
        return False
    if any(part in path for part in ["/noticia/", "/noticias/", "/print-noticia/", "/conteudo/", "/conteudos/"]):
        return False
    if re.search(r"/site/concursos/\d+", path):
        return False
    if re.search(r"/site/editais/\d+", path):
        return False
    if re.search(r"/concursos?/detalhe/\d+", path):
        return False
    if re.search(r"/concurso/detalhe/\d+", path):
        return False
    return True


def has_structured_route_signal(item: dict[str, str], bucket: str) -> bool:
    url = item.get("url", "")
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    host = parsed.netloc.lower()
    route_blob = norm(unquote(f"{parsed.path} {parsed.query}"))
    text_blob = norm(item.get("text", ""))
    leading_text = text_blob[:240]
    primary_label = text_blob[:120]
    full_blob = compact_space(f"{route_blob} {text_blob}")
    qs = parse_qs(parsed.query, keep_blank_values=True)
    is_publicacoes_pg = (
        path.endswith("/pg.php")
        and (qs.get("area", [""])[0] or "").upper() == "PUBLICACOES"
        and bool(qs.get("subarea", [""])[0] or "")
    )
    if bucket == "processos":
        if is_publicacoes_pg and any(
            signal in primary_label
            for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if is_publicacoes_pg and any(
            signal in leading_text
            for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if is_publicacoes_pg and any(
            signal in full_blob
            for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if any(signal in route_blob for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]):
            return True
        if path in {
            "/processos-seletivos",
            "/processo-seletivo",
            "/portal-da-transparencia/processos-seletivos",
            "/portal-da-transparencia/processo-seletivo",
            "/transparencia/item/processos-seletivos",
        }:
            return True
        if host.endswith(".atende.net") and path == "/transparencia/item/processos-seletivos":
            return True
        if "subarea=18" in url.lower():
            return True
        if re.search(r"/concurso/categoria/\d+/processo-seletivo/?$", path):
            return True
    else:
        if is_publicacoes_pg and "concurso" in primary_label and not any(
            signal in primary_label for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if is_publicacoes_pg and "concurso" in leading_text and not any(
            signal in leading_text for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if is_publicacoes_pg and "concurso" in full_blob and not any(
            signal in full_blob for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if "concurso" in route_blob and not any(
            signal in route_blob for signal in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"]
        ):
            return True
        if path in {
            "/concursos-publicos",
            "/concursos-publico",
            "/concurso-publico",
            "/portal-da-transparencia/concursos-publicos",
            "/portal-da-transparencia/concurso-publico",
            "/transparencia/item/concursos-publicos",
        }:
            return True
        if host.endswith(".atende.net") and path == "/transparencia/item/concursos-publicos":
            return True
        if "subarea=17" in url.lower():
            return True
        if re.search(r"/concurso/categoria/\d+/concurso/?$", path):
            return True
    return False


def evidence_supports_url(evidence: dict, url: str, bucket: str, min_score: int = 70) -> tuple[bool, str]:
    url = clean_url(url)
    if not is_resource_list_url(url):
        return False, "specific_event_url_not_resource_list"
    matching = [item for item in evidence_candidates(evidence) if item.get("url") == url]
    if not matching:
        return False, "url_not_in_evidence_candidates"
    for item in matching:
        score = candidate_bucket_score(item, bucket)
        if item.get("generated") == "1":
            if score >= min_score and (has_visible_bucket_signal(item, bucket) or has_structured_route_signal(item, bucket)):
                return True, f"generated_supported:{item.get('source')}:score_{score}"
            continue
        if score >= min_score:
            return True, f"discovered_supported:{item.get('source')}:score_{score}"
    best = max((candidate_bucket_score(item, bucket) for item in matching), default=0)
    return False, f"insufficient_visible_evidence:best_score_{best}"


def candidate_bucket_score(item: dict[str, str], bucket: str) -> int:
    url = item.get("url", "")
    blob = norm(f"{url} {item.get('text', '')}")
    parsed = urlparse(url)
    route_blob = norm(unquote(f"{parsed.path} {parsed.query}"))
    strong_listing_signal = has_bucket_document_listing_signal(blob, bucket)
    structured_signal = has_structured_route_signal(item, bucket)
    generic_publication_container = is_generic_publication_container(url)
    score = 0
    if not is_resource_list_url(url):
        score -= 180
    if any(bad in blob for bad in ["licitacao", "licitacoes", "pregao", "compras", "fornecedor", "chamamento publico"]):
        score -= 10 if (structured_signal or strong_listing_signal) else 160
    if any(
        derived in blob
        for derived in [
            "convocacao",
            "convocacoes",
            "nomeacao",
            "nomeacoes",
            "resultado",
            "homologacao",
            "classificacao",
            "gabarito",
            "chamamento",
            "credenciamento",
        ]
    ):
        score -= 10 if (structured_signal or strong_listing_signal) else 70

    if bucket == "processos":
        if generic_publication_container and strong_listing_signal:
            score += 135
        if re.search(r"\bprocessos?\s+seletivos?\b", route_blob):
            score += 70
        if structured_signal:
            score += 120
        if "processos seletivos" in blob or "processo seletivo" in blob:
            score += 120
        if "pss" in blob:
            score += 80
        if "selecao publica" in blob or "selecoes publicas" in blob:
            score += 70
        if "subarea 18" in blob:
            score += 90
        if "concurso publico" in blob and not any(x in blob for x in ["processo seletivo", "processos seletivos", "pss"]):
            score -= 10 if structured_signal else 35
    else:
        if generic_publication_container and strong_listing_signal:
            score += 135
        if re.search(r"\bconcursos?\s+publicos?\b", route_blob):
            score += 70
        if structured_signal:
            score += 120
        if "concursos publicos" in blob or "concurso publico" in blob:
            score += 120
        elif "concurso" in blob or "concursos" in blob:
            score += 70
        if "subarea 17" in blob:
            score += 90
        if any(x in blob for x in ["processo seletivo", "processos seletivos", "pss"]):
            score -= 10 if structured_signal else 90
        if "nomeacao" in blob or "nomeacoes" in blob:
            score -= 10 if structured_signal else 35

    if item.get("source") == "link":
        score += 8
    return score


def best_verified_from_evidence(
    session: requests.Session,
    evidence: dict,
    bucket: str,
    timeout: int,
    min_score: int = 70,
    exclude_urls: set[str] | None = None,
) -> tuple[str, str]:
    exclude_urls = {clean_url(url) for url in (exclude_urls or set()) if clean_url(url)}
    scored = sorted(
        ((candidate_bucket_score(item, bucket), item) for item in evidence_candidates(evidence)),
        key=lambda x: x[0],
        reverse=True,
    )
    for score, item in scored[:12]:
        if score < min_score:
            break
        if clean_url(item.get("url", "")) in exclude_urls:
            continue
        if item.get("generated") == "1" and not (
            has_visible_bucket_signal(item, bucket) or has_structured_route_signal(item, bucket)
        ):
            continue
        verified, note = verify_choice(session, item["url"], bucket, timeout)
        if verified:
            return verified, f"fallback_{bucket}:{item['source']}:score_{score}:{note}"
    return "", f"fallback_{bucket}:not_found"


def is_overbroad_resource_choice(url: str, site_base: str) -> bool:
    """Detecta cuando Gemini eligio la home o un contenedor demasiado amplio.

    Si ya tenemos una ruta especifica verificada en la evidencia, estas paginas
    no son el mejor destino final para una columna de concursos/processos.
    """
    cleaned = clean_url(url)
    base = clean_url(site_base)
    if not cleaned:
        return False
    parsed = urlparse(cleaned)
    path = (parsed.path or "/").strip("/").lower()
    if base and cleaned.rstrip("/") == base.rstrip("/"):
        return True
    return path in {"", "web", "home", "inicio", "index.php"}


def prefer_specific_resource_choice(
    session: requests.Session,
    evidence: dict,
    current_url: str,
    bucket: str,
    site_base: str,
    timeout: int,
) -> tuple[str, str]:
    if not is_overbroad_resource_choice(current_url, site_base):
        return current_url, ""
    replacement, note = best_verified_from_evidence(
        session,
        evidence,
        bucket,
        timeout,
        exclude_urls={current_url, site_base},
    )
    if replacement:
        return replacement, f"overbroad_replaced_with_specific:{note}"
    return current_url, "overbroad_no_specific_replacement"


def build_evidence(session: requests.Session, municipio: str, timeout: int) -> tuple[dict, str]:
    search_results = []
    generated_candidates = []
    for query in municipio_queries(municipio):
        print(f"    search: {query}", flush=True)
        search_results.extend(search_web(session, query, timeout, 8))

    print(f"    choose initial site from search_results={len(search_results)}", flush=True)
    initial, probes = choose_initial_site(session, municipio, timeout)
    pages = []
    if initial:
        pages.append(page_snapshot(initial, initial.url, "initial"))
    for probe in probes[:8]:
        if probe.url != (initial.url if initial else ""):
            pages.append(page_snapshot(probe, probe.url, "probe"))

    seen_page_urls = {clean_url(p.get("url", "")) for p in pages}

    smart_roots = []
    if initial:
        smart_roots.append(initial.url)
    for probe in probes:
        if probe.status == 200 and not is_soft_404(probe):
            smart_roots.append(probe.url)

    seen_smart_roots = set()
    filtered_smart_roots = []
    for root in smart_roots:
        cleaned = clean_url(root)
        parsed = urlparse(cleaned)
        host_key = re.sub(r"^www\.", "", parsed.netloc.lower())
        path_key = parsed.path.rstrip("/") or "/"
        key = (host_key, path_key)
        if cleaned and key not in seen_smart_roots:
            seen_smart_roots.add(key)
            filtered_smart_roots.append(cleaned)
    smart_roots = filtered_smart_roots[:4]

    for smart_root in smart_roots:
        for smart_url in smart_path_candidates(smart_root)[:14]:
            if not smart_url or smart_url in seen_page_urls:
                continue
            generated_candidates.append({"url": smart_url, "source": "smart_path_candidate", "text": ""})
            print(f"    open smart path: {smart_url[:120]}", flush=True)
            smart_page = fetch(session, smart_url, timeout)
            if smart_page.status == 200 and not is_soft_404(smart_page):
                pages.append(page_snapshot(smart_page, smart_url, "smart_path"))
                seen_page_urls.add(clean_url(smart_page.url))

    if initial:
        home_links = sorted(visible_links(initial, initial.url, 60), key=link_priority, reverse=True)
        opened_home_pages = []
        for link in home_links[:30]:
            print(f"    open home link: {link['url'][:120]}", flush=True)
            page = fetch(session, link["url"], timeout)
            pages.append(page_snapshot(page, initial.url, "home_link", link.get("text", "")))
            opened_home_pages.append((link, page))
            seen_page_urls.add(clean_url(page.url))

            if page.status == 200 and not is_soft_404(page) and is_generic_discovery_page(page.url):
                for smart_url in smart_path_candidates(page.url)[:6]:
                    if not smart_url or smart_url in seen_page_urls:
                        continue
                    generated_candidates.append({"url": smart_url, "source": "smart_child_candidate", "text": ""})
                    print(f"    open smart child: {smart_url[:120]}", flush=True)
                    smart_page = fetch(session, smart_url, timeout)
                    if smart_page.status == 200 and not is_soft_404(smart_page):
                        pages.append(page_snapshot(smart_page, smart_url, "smart_child", link.get("text", "")))
                        seen_page_urls.add(clean_url(smart_page.url))

        second_level_opened = 0
        seen_second = {clean_url(p.get("url", "")) for p in pages}
        for link, parent_page in opened_home_pages:
            if second_level_opened >= 16:
                break
            if not should_open_second_level(link) or parent_page.status != 200 or is_soft_404(parent_page):
                continue
            child_links = sorted(visible_links(parent_page, parent_page.url, 60), key=link_priority, reverse=True)
            for child_link in child_links[:8]:
                child_url = clean_url(child_link["url"])
                if not child_url or child_url in seen_second:
                    continue
                print(f"    open second-level link: {child_url[:120]}", flush=True)
                child_page = fetch(session, child_url, timeout)
                pages.append(page_snapshot(child_page, parent_page.url, "second_level", child_link.get("text", "")))
                seen_second.add(child_url)
                second_level_opened += 1
                if second_level_opened >= 16:
                    break

    opened_search = 0
    for result in search_results:
        url = clean_url(result.get("url", ""))
        if not url or url in seen_page_urls:
            continue
        if search_result_is_promising(result, municipio):
            print(f"    open search result: {url[:120]}", flush=True)
            page = fetch(session, url, timeout)
            pages.append(page_snapshot(page, url, "search_result", result.get("text", "")))
            seen_page_urls.add(url)
            opened_search += 1
        if opened_search >= 8:
            break

    evidence = {
        "search_results": search_results[:36],
        "pages": pages[:70],
        "generated_candidates": generated_candidates[:120],
    }
    return evidence, initial.url if initial else ""


def build_row(args: argparse.Namespace, municipio_row: dict[str, str], index: int, total: int) -> dict[str, str]:
    municipio = title_case_municipio(repair_text_encoding(municipio_row["municipio"]))
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; concursos-rs-gemini-site-investigator/0.1)",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        }
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    row = {
        "uf": "RS",
        "municipio": municipio,
        "ibge": municipio_row.get("ibge", ""),
        "site_base": "",
        "site_status": "nao_encontrado",
        "url_concursos": "",
        "status_concursos": "nao_encontrada",
        "url_processos_seletivos": "",
        "status_processos_seletivos": "nao_encontrada",
        "confidence": "0",
        "method": f"gemini_guided_site_search:{args.model}",
        "notes": "",
        "checked_at": checked_at,
        # campos internos (no van al CSV, los ignora extrasaction="ignore")
        "_tokens_in": 0,
        "_tokens_out": 0,
        "_tokens_total": 0,
        "_elapsed": 0.0,
    }
    t0 = time.time()
    try:
        print(f"[{index}/{total}] {municipio}: build evidence", flush=True)
        evidence, initial_site = build_evidence(session, municipio, args.timeout)
        print(f"[{index}/{total}] {municipio}: gemini pages={len(evidence.get('pages', []))}", flush=True)

        gemini_notes = []
        try:
            choice_raw, usage = gemini_pick(session, args.model, municipio, evidence, args.ai_timeout)
            choice = coerce_choice(choice_raw)
            row["_tokens_in"] += usage["input"]
            row["_tokens_out"] += usage["output"]
            row["_tokens_total"] += usage["total"]
        except Exception as gemini_exc:
            gemini_notes.append(f"gemini_error:{type(gemini_exc).__name__}:{compact_space(str(gemini_exc))[:160]}")
            traceback.print_exc()
            choice = coerce_choice({})

        opened = 0
        allow = allowed_urls(evidence)
        for next_url in choice["open_next"][: args.max_followups]:
            next_url = clean_url(str(next_url))
            if next_url and next_url in allow:
                page = fetch(session, next_url, args.timeout)
                evidence["pages"].append(page_snapshot(page, next_url, "gemini_followup"))
                opened += 1
        if opened:
            print(f"[{index}/{total}] {municipio}: gemini followup opened={opened}", flush=True)
            try:
                choice_raw, usage2 = gemini_pick(session, args.model, municipio, evidence, args.ai_timeout)
                choice = coerce_choice(choice_raw)
                row["_tokens_in"] += usage2["input"]
                row["_tokens_out"] += usage2["output"]
                row["_tokens_total"] += usage2["total"]
            except Exception as gemini_exc:
                gemini_notes.append(f"gemini_followup_error:{type(gemini_exc).__name__}:{compact_space(str(gemini_exc))[:160]}")
                traceback.print_exc()

        allow = allowed_urls(evidence)
        site_base = clean_url(str(choice.get("site_base", ""))) or initial_site
        conc = clean_url(str(choice.get("url_concursos", "")))
        pss = clean_url(str(choice.get("url_processos_seletivos", "")))
        if site_base and (site_base in allow or official_site_score(fetch(session, site_base, args.timeout), municipio) >= 25):
            row["site_base"] = site_base
            row["site_status"] = "boa"
        if conc in allow:
            supported, support_note = evidence_supports_url(evidence, conc, "concursos")
            if supported:
                verified, verify_note = verify_choice(session, conc, "concursos", args.timeout)
                verify_note = f"{support_note}:{verify_note}"
                row["url_concursos"] = verified
                row["status_concursos"] = "boa" if verified else "nao_encontrada"
            else:
                verify_note = support_note
        else:
            verify_note = "conc_not_in_evidence"
        if not row["url_concursos"]:
            fallback_url, fallback_note = best_verified_from_evidence(session, evidence, "concursos", args.timeout)
            if fallback_url:
                row["url_concursos"] = fallback_url
                row["status_concursos"] = "boa"
            verify_note = f"{verify_note}; {fallback_note}"
        if not row["url_concursos"]:
            targeted_url, targeted_note = best_verified_from_targeted_search(
                session,
                municipio,
                row["site_base"] or initial_site,
                "concursos",
                args.timeout,
            )
            if targeted_url:
                row["url_concursos"] = targeted_url
                row["status_concursos"] = "boa"
            verify_note = f"{verify_note}; {targeted_note}"
        if pss in allow:
            supported, support_note = evidence_supports_url(evidence, pss, "processos")
            if supported:
                verified, verify_pss_note = verify_choice(session, pss, "processos", args.timeout)
                verify_pss_note = f"{support_note}:{verify_pss_note}"
                row["url_processos_seletivos"] = verified
                row["status_processos_seletivos"] = "boa" if verified else "nao_encontrada"
            else:
                verify_pss_note = support_note
        else:
            verify_pss_note = "pss_not_in_evidence"
        if not row["url_processos_seletivos"]:
            fallback_url, fallback_note = best_verified_from_evidence(session, evidence, "processos", args.timeout)
            if fallback_url:
                row["url_processos_seletivos"] = fallback_url
                row["status_processos_seletivos"] = "boa"
            verify_pss_note = f"{verify_pss_note}; {fallback_note}"
        if not row["url_processos_seletivos"]:
            targeted_url, targeted_note = best_verified_from_targeted_search(
                session,
                municipio,
                row["site_base"] or initial_site,
                "processos",
                args.timeout,
            )
            if targeted_url:
                row["url_processos_seletivos"] = targeted_url
                row["status_processos_seletivos"] = "boa"
            verify_pss_note = f"{verify_pss_note}; {targeted_note}"

        specificity_notes = []
        if row["url_concursos"]:
            specific_url, specific_note = prefer_specific_resource_choice(
                session,
                evidence,
                row["url_concursos"],
                "concursos",
                row["site_base"],
                args.timeout,
            )
            if specific_note:
                row["url_concursos"] = specific_url
                specificity_notes.append(f"concursos:{specific_note}")
        if row["url_processos_seletivos"]:
            specific_url, specific_note = prefer_specific_resource_choice(
                session,
                evidence,
                row["url_processos_seletivos"],
                "processos",
                row["site_base"],
                args.timeout,
            )
            if specific_note:
                row["url_processos_seletivos"] = specific_url
                specificity_notes.append(f"processos:{specific_note}")

        normalization_notes = []
        if row["url_concursos"]:
            row["url_concursos"], normalize_note = normalize_verified_resource_url(
                session, row["url_concursos"], "concursos", args.timeout
            )
            if normalize_note:
                normalization_notes.append(f"concursos:{normalize_note}")
        if row["url_processos_seletivos"]:
            row["url_processos_seletivos"], normalize_note = normalize_verified_resource_url(
                session, row["url_processos_seletivos"], "processos", args.timeout
            )
            if normalize_note:
                normalization_notes.append(f"processos:{normalize_note}")

        row["confidence"] = str(choice.get("confidence", "0"))
        row["notes"] = compact_space(
            f"{choice.get('reason','')} | {'; '.join(gemini_notes) if gemini_notes else 'gemini_ok'}; "
            f"verify_conc={verify_note}; verify_pss={verify_pss_note}; "
            f"specificity={';'.join(specificity_notes) or 'none'}; "
            f"normalize={';'.join(normalization_notes) or 'none'}; pages={len(evidence['pages'])}"
        )[:900]
    except Exception as exc:
        row["notes"] = f"error:{type(exc).__name__}:{compact_space(str(exc))[:300]}"
        traceback.print_exc()
    finally:
        row["_elapsed"] = round(time.time() - t0, 1)

    print(
        f"[{index}/{total}] {municipio}: site={row['site_status']} "
        f"conc={row['status_concursos']} pss={row['status_processos_seletivos']} "
        f"| tokens in={row['_tokens_in']} out={row['_tokens_out']} total={row['_tokens_total']} "
        f"| {row['_elapsed']}s :: {row['notes'][:100]}",
        flush=True,
    )
    return row


def load_existing_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def print_run_stats(rows: list[dict]) -> None:
    """<-- AGREGADO: promedios de tokens, tiempo y proyeccion de costo."""
    this_run = [r for r in rows if "_tokens_total" in r]
    if not this_run:
        return
    n = len(this_run)
    sum_in = sum(int(r.get("_tokens_in", 0) or 0) for r in this_run)
    sum_out = sum(int(r.get("_tokens_out", 0) or 0) for r in this_run)
    sum_total = sum(int(r.get("_tokens_total", 0) or 0) for r in this_run)
    sum_time = sum(float(r.get("_elapsed", 0) or 0) for r in this_run)

    cost_run = sum_in * PRICE_IN_PER_TOKEN + sum_out * PRICE_OUT_PER_TOKEN
    avg_cost = cost_run / n if n else 0
    proj_cost = avg_cost * TOTAL_MUNICIPIOS_RS

    print("-" * 70, flush=True)
    print(
        f"AVERAGES/municipio (n={n}): "
        f"tokens in={sum_in / n:.0f} out={sum_out / n:.0f} total={sum_total / n:.0f} "
        f"| tiempo={sum_time / n:.1f}s",
        flush=True,
    )
    print(
        f"COSTO solo-tokens: este run=${cost_run:.4f} | "
        f"promedio/municipio=${avg_cost:.5f} | "
        f"proyeccion {TOTAL_MUNICIPIOS_RS} muni=${proj_cost:.2f} "
        f"(no incluye grounding; este script no usa grounding)",
        flush=True,
    )
    print("-" * 70, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "municipios_resources_a_gemini_guided.csv")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--ai-timeout", type=int, default=45)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-followups", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    municipios = [r for r in load_municipios(args.timeout) if norm(r["municipio"]).startswith("a")]
    municipios.sort(key=lambda r: norm(r["municipio"]))
    if args.offset:
        municipios = municipios[args.offset :]
    if args.limit:
        municipios = municipios[: args.limit]
    print(f"MUNICIPIOS_A {len(municipios)} offset={args.offset} model={args.model}", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = load_existing_rows(args.output) if args.resume else []
    done = {(row.get("municipio", ""), row.get("ibge", "")) for row in rows}
    mode = "a" if args.resume and args.output.exists() and args.output.stat().st_size > 0 else "w"
    with args.output.open(mode, encoding="utf-8-sig", newline="") as handle:
        # extrasaction="ignore" deja pasar los campos internos "_tokens*"/"_elapsed"
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        for idx, row in enumerate(municipios, start=1):
            key = (row["municipio"], row.get("ibge", ""))
            if key in done:
                print(f"[{idx}/{len(municipios)}] {row['municipio']}: skip resume", flush=True)
                continue
            result = build_row(args, row, idx, len(municipios))
            rows.append(result)
            writer.writerow(result)
            handle.flush()
    print(f"WROTE {args.output}", flush=True)
    print(
        f"SUMMARY rows={len(rows)} site_ok={sum(1 for r in rows if r['site_status']=='boa')} "
        f"concursos_boa={sum(1 for r in rows if r['status_concursos']=='boa')} "
        f"processos_boa={sum(1 for r in rows if r['status_processos_seletivos']=='boa')}",
        flush=True,
    )
    print_run_stats(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
