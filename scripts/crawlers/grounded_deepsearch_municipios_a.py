"""Descubrimiento de URLs de concursos/processos seletivos con GROUNDING.

Script APARTE del crawler actual (no lo toca). Arquitectura en cascada para
gastar grounding solo cuando hace falta:

    TIER 1 (gratis):    adivina el DOMINIO oficial (.rs.gov.br/.atende.net),
                        sigue avisos de mudanza, y LEE los links reales del
                        home (NO adivina subrutas). Verifica por contenido.
    TIER 2 (grounding): UNA llamada con google_search SOLO si Tier 1 no
                        completó ambos buckets. Gemini busca en Google las
                        URLs reales indexadas.
    TIER 3 (gratis):    verifica por contenido lo que devolvió el grounding.

Principios:
    - El DESCUBRIMIENTO lo hace Gemini/Google (no adivinamos slugs).
    - La VERIFICACION la hace este código (contenido manda sobre el slug).
    - Nunca se emite una URL sin verificar contra el contenido real.
    - El ESTADO (RS / Rio Grande do Sul) va SIEMPRE, porque hay municipios
      homonimos en otros estados / Portugal.

Reutiliza primitivos del modulo base (fetch, Page, etc.). La logica de
grounding, cascada y verificacion es propia y autocontenida.
"""
from __future__ import annotations

import argparse
import csv
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
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deepsearch_municipios_a_no_ai import (  # noqa: E402
    FIELDS,
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

# Estado: SIEMPRE presente para evitar municipios homonimos de otro estado.
UF_SIGLA = "RS"
UF_NOME = "Rio Grande do Sul"

# Precios gemini-2.5-flash (USD/token) para proyectar costo de tokens.
PRICE_IN_PER_TOKEN = 0.30 / 1_000_000
PRICE_OUT_PER_TOKEN = 2.50 / 1_000_000
# Grounding 2.5: cobrado POR PROMPT (no por query). ~$35 / 1.000 prompts.
PRICE_GROUNDING_PER_PROMPT = 35.0 / 1_000
TOTAL_MUNICIPIOS_RS = 497  # aprox., para proyectar el run completo.

# Contador global de prompts de grounding (para el guard de presupuesto).
_GROUNDING_CALLS = 0
# Contador global de validaciones IA de rutas ambiguas (guard de presupuesto).
_ROUTE_AI_CALLS = 0
_RENDER_CACHE: dict[str, Page] = {}

BAD_HOSTS = [
    "facebook.",
    "instagram.",
    "youtube.",
    "twitter.",
    "x.com",
    "linkedin.",
    "acheconcursos.",
    "pciconcursos.",
    "qconcursos.",
    "google.",
    "bing.",
    "duckduckgo.",
]

# Frases (normalizadas) que indican que la prefeitura mudo de site.
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

# Links "intermedios" que pueden contener concursos/PSS adentro
# (la pagina puede llamarse "documentos" pero listar PSS -> contenido manda).
INTERMEDIATE_TERMS = [
    "editais",
    "edital",
    "documentos",
    "documentacoes",
    "publicacoes",
    "publicacao",
    "transparencia",
    "portal da transparencia",
    "recursos humanos",
    "rh",
    "concursos e selecoes",
    "selecoes publicas",
]

# Contexto de listado: palabras que aparecen en una pagina que REALMENTE
# lista editais/eventos, para distinguir de un catch-all vacio que solo
# tiene el termino en el menu (caso Alto Feliz).
LISTING_CONTEXT = [
    "edital",
    "editais",
    "inscricoes",
    "inscricao",
    "publicado",
    "homologacao",
    "resultado",
    "prova",
    "cronograma",
    "retificacao",
    "abertura",
    "anexo",
    "classificacao",
]


# --------------------------------------------------------------------------- #
# Gemini: helpers comunes
# --------------------------------------------------------------------------- #
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
                print(f"      gemini retry {attempt + 1}/{max_attempts} after {delay:.1f}s: {exc}", flush=True)
                time.sleep(delay)
    raise RuntimeError(f"gemini_post_failed_after_{max_attempts}:{last_exc}")


def extract_usage(data: dict) -> dict:
    u = (data or {}).get("usageMetadata", {}) or {}
    inp = int(u.get("promptTokenCount", 0) or 0)
    out = int(u.get("candidatesTokenCount", 0) or 0)
    total = int(u.get("totalTokenCount", 0) or 0)
    if total and total > inp + out:  # 2.5 puede incluir thinking en el total
        out = total - inp
    if not total:
        total = inp + out
    return {"input": inp, "output": out, "total": total}


def add_usage(row: dict | None, usage: dict) -> None:
    if row is None:
        return
    row["_tokens_in"] = int(row.get("_tokens_in", 0) or 0) + int(usage.get("input", 0) or 0)
    row["_tokens_out"] = int(row.get("_tokens_out", 0) or 0) + int(usage.get("output", 0) or 0)
    row["_tokens_total"] = int(row.get("_tokens_total", 0) or 0) + int(usage.get("total", 0) or 0)


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
    """Blindaje de tipos (evita crashes tipo 'bool object is not subscriptable')."""
    if not isinstance(choice, dict):
        choice = {}

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
        "confidence": confidence,
        "reason": as_str(choice.get("reason", "")),
        "open_next": open_next,
    }


def resolve_redirect(session: requests.Session, url: str, timeout: int) -> str:
    """Resuelve los redirects de grounding (vertexaisearch...redirect) a la URL
    final real. Esas URIs expiran; hay que guardar el destino."""
    try:
        resp = session.get(url, allow_redirects=True, timeout=timeout)
        return clean_url(resp.url)
    except Exception:
        return clean_url(url)


# --------------------------------------------------------------------------- #
# Dominio / estado
# --------------------------------------------------------------------------- #
def municipio_ascii(municipio: str) -> str:
    municipio = repair_text_encoding(municipio)
    return compact_space(
        "".join(ch for ch in unicodedata.normalize("NFKD", municipio) if not unicodedata.combining(ch))
    )


def municipio_host_slugs(municipio: str) -> list[str]:
    """El dominio municipal no es consistente: algunos quitan 'do/de/da',
    otros los mantienen. Probar ambos patrones."""
    municipio = repair_text_encoding(municipio)
    no_particles = slugify_municipio(municipio)
    with_particles = re.sub(r"[^a-z0-9]+", "", norm(municipio))
    out = []
    for slug in [no_particles, with_particles]:
        if slug and slug not in out:
            out.append(slug)
    return out


def domain_candidates(municipio: str) -> list[str]:
    urls: list[str] = []
    for slug in municipio_host_slugs(municipio):
        for host in (f"www.{slug}.rs.gov.br", f"{slug}.rs.gov.br"):
            # http y https: muchos sites municipais son http-only (Chrome "No seguro").
            urls.extend([f"https://{host}/", f"http://{host}/"])
        urls.append(f"https://{slug}.atende.net/")
    seen = set()
    out = []
    for u in urls:
        u = clean_url(u)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def host_matches_municipio_rs(url: str, municipio: str, site_base: str = "") -> bool:
    """Backstop de estado: acepta una URL solo si es del municipio correcto en RS.

    Acepta: (1) mismo dominio que el site oficial ya verificado; (2) dominio
    .rs.gov.br / .atende.net cuyo slug matchea el municipio; (3) portal de
    terceros que contenga el slug del municipio Y un indicador 'rs'.
    """
    cleaned = clean_url(url)
    parsed = urlparse(cleaned)
    host = parsed.netloc.lower()
    route = norm(unquote(f"{parsed.path} {parsed.query}"))
    host_p = host[4:] if host.startswith("www.") else host
    base = urlparse(clean_url(site_base)).netloc.lower()
    base_p = base[4:] if base.startswith("www.") else base

    if base_p and (host_p == base_p or host_p.endswith("." + base_p)):
        return True

    slugs = [re.sub(r"[^a-z0-9]+", "", s) for s in municipio_host_slugs(municipio)]
    if host_p.endswith(".rs.gov.br") or host_p.endswith(".atende.net"):
        host_slug = re.sub(r"[^a-z0-9]+", "", host_p.split(".")[0])
        if any(s and (s in host_slug or host_slug in s) for s in slugs):
            return True

    # Algunas prefeituras enlazan desde el menú oficial hacia un portal delegado
    # de transparência en host/IP externo. No lo tratamos como válido por dominio:
    # solo lo dejamos pasar al validador de contenido, que debe confirmar bucket.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?", host_p):
        if "multi24" in route and "transparencia" in route:
            return True

    path_blob = (host_p + " " + parsed.path).lower()
    compact = re.sub(r"[^a-z0-9]+", "", path_blob)
    if "rs" in path_blob and any(s and s in compact for s in slugs):
        return True
    return False


# --------------------------------------------------------------------------- #
# Verificacion: CONTENIDO manda sobre el slug
# --------------------------------------------------------------------------- #
def is_broad_landing(url: str) -> bool:
    path = (urlparse(clean_url(url)).path or "/").strip("/").lower()
    return path in {"", "web", "home", "inicio", "index.php", "index.html"}


def is_detail_or_news_url(url: str) -> bool:
    """Pages about one notice/event are evidence, but not the preferred base URL."""
    parsed = urlparse(clean_url(url))
    route = norm(unquote(f"{parsed.path} {parsed.query}"))
    return bool(
        re.search(
            r"\b(noticia|noticias|news|materia|post|posts|detalhe|detalles|view|visualizar|exibir)\b",
            route,
        )
    )


def normalize_year_filter_url(session: requests.Session, url: str, bucket: str, timeout: int) -> str:
    """Prefer pages that show all years when the same verified resource supports ano=0."""
    parsed = urlparse(clean_url(url))
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k.lower() == "ano" and v and v != "0" for k, v in query):
        return clean_url(url)
    new_query = [(k, "0" if k.lower() == "ano" else v) for k, v in query]
    candidate = clean_url(urlunparse(parsed._replace(query=urlencode(new_query))))
    if candidate == clean_url(url):
        return clean_url(url)
    page = fetch(session, candidate, timeout)
    if should_try_rendered(page, bucket):
        rendered = fetch_rendered(session, candidate, timeout)
        if rendered.status == 200 and rendered.text:
            page = rendered
    if page.status != 200 or is_soft_404(page):
        return clean_url(url)
    text = f"{getattr(page, 'text', '') or ''} {getattr(page, 'title', '') or ''}"
    blob = norm(text)
    has_context = any(c in blob for c in [norm(x) for x in LISTING_CONTEXT])
    if bucket_event_signal(text, bucket) and (strong_bucket_signal(text, bucket) or (bucket_term_count(blob, bucket) >= 2 and has_context)):
        return clean_url(page.url)
    return clean_url(url)


def bucket_term_count(blob: str, bucket: str) -> int:
    if bucket == "processos":
        return len(re.findall(r"processos?\s+seletivos?|selec[ao]es?\s+publicas?|\bpss\b", blob))
    return len(re.findall(r"concursos?\s+publicos?|\bconcurso\b", blob))


def has_public_selection_signal(blob: str, bucket: str) -> bool:
    """Evita confundir concursos culturales/eventos con selecao publica."""
    if bucket == "processos":
        if re.search(r"\bconcurso\s+soberanas?\b|\brainhas?\b|\bprincesas?\b|\bcorte\b|\bbeleza\b", blob) and not re.search(
            r"processo\s+seletivo\s+(?:simplificado|n|0?\d)|\bpss\b|selec[ao]\s+publica\s+(?:n|0?\d)",
            blob,
        ):
            return False
        return bool(re.search(r"processos?\s+seletivos?|selec[ao]es?\s+publicas?|\bpss\b", blob))
    if re.search(r"licitac(?:ao|oes)|chamamento\s+publico|credenciamento|pregao|dispensa", blob) and not re.search(
        r"concurso\s+publico\s+(?:n|0?\d)|concurso\s+\d{1,3}\s*[-/]\s*\d{4}",
        blob,
    ):
        return False
    if re.search(r"\bsoberanas?\b|\brainhas?\b|\bprincesas?\b|\bcorte\b|\bbeleza\b", blob) and not re.search(
        r"concursos?\s+publicos?", blob
    ):
        return False
    return bool(
        re.search(r"concursos?\s+publicos?", blob)
        or (
            re.search(r"\bconcurso\b", blob)
            and re.search(r"\bedital\b|\binscric(?:ao|oes)\b|\bcargos?\b|\bvagas?\b|\bprovas?\b", blob)
        )
    )


def strong_bucket_signal(text: str, bucket: str) -> bool:
    """Senal FUERTE: la pagina realmente lista editais/eventos del bucket.
    Esto distingue una pagina real de un catch-all vacio que solo tiene la
    palabra en el menu (Alto Feliz)."""
    blob = norm(text)
    if bucket == "processos":
        patterns = [
            r"edital\s+(?:n[ºo°]?\s*)?\d+\s*[/-]\s*\d{2,4}[^.]{0,60}processo\s+seletivo",
            r"processo\s+seletivo[^.]{0,60}edital\s+(?:n[ºo°]?\s*)?\d+",
            r"processo\s+seletivo\s+simplificado",
            r"\bpss\s+(?:n[ºo°]?\s*)?\d+\s*[/-]\s*\d{2,4}",
            r"\bprocessos\s+seletivos\b",  # forma plural = encabezado de listado
        ]
    else:
        patterns = [
            r"edital\s+(?:n[ºo°]?\s*)?\d+\s*[/-]\s*\d{2,4}[^.]{0,60}concurso\s+publico",
            r"concurso\s+publico[^.]{0,60}edital\s+(?:n[ºo°]?\s*)?\d+",
            r"\bconcursos\s+publicos\b",  # forma plural = encabezado de listado
        ]
    return any(re.search(p, blob) for p in patterns)


def bucket_event_signal(text: str, bucket: str) -> bool:
    """Senal de evento/documento real, no solo encabezado o menu."""
    blob = norm(text)
    number_year = r"(?:n\s*\.?\s*[oº°]?\s*)?\d{1,4}\s*(?:[/-]|\s+)\s*\d{4}"
    if bucket == "processos":
        return bool(
            re.search(
                rf"processo\s+seletivo(?:\s+simplificado)?[^.\n]{{0,80}}{number_year}"
                rf"|{number_year}[^.\n]{{0,80}}processo\s+seletivo"
                rf"|\bpss\b[^.\n]{{0,80}}{number_year}"
                rf"|{number_year}[^.\n]{{0,80}}\bpss\b"
                rf"|selec[ao]\s+publica[^.\n]{{0,80}}{number_year}",
                blob,
            )
        )
    return bool(
        re.search(
            rf"concurso\s+publico[^.\n]{{0,100}}{number_year}"
            rf"|{number_year}[^.\n]{{0,100}}concurso\s+publico",
            blob,
        )
    )


def fetch_rendered(session: requests.Session, url: str, timeout: int) -> Page:
    """Renderiza con Chromium cuando el HTML estatico no trae filas reales."""
    cleaned = clean_url(url)
    if cleaned in _RENDER_CACHE:
        return _RENDER_CACHE[cleaned]

    start = time.perf_counter()
    browser = None
    render_timeout_ms = max(4000, min(timeout * 1000, 9000))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            user_agent = session.headers.get(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            page = browser.new_page(user_agent=user_agent, locale="pt-BR")
            page.goto(url, wait_until="domcontentloaded", timeout=render_timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(2500, render_timeout_ms))
            except Exception:
                pass
            page.wait_for_timeout(700)
            title = compact_space(page.title())
            try:
                text = compact_space(page.locator("body").inner_text(timeout=min(3000, render_timeout_ms)))
            except Exception:
                text = compact_space(re.sub(r"<[^>]+>", " ", page.content()))
            links = page.locator("a").evaluate_all(
                """els => els.slice(0, 220).map(a => ({
                    href: a.href || a.getAttribute('href') || '',
                    text: (a.innerText || a.textContent || '').trim()
                }))"""
            )
            rendered = Page(
                clean_url(page.url),
                200,
                title,
                text[:40_000],
                [
                    {"href": str(link.get("href", "")), "text": compact_space(str(link.get("text", "")))}
                    for link in links
                    if isinstance(link, dict)
                ],
                int((time.perf_counter() - start) * 1000),
                "rendered",
            )
            _RENDER_CACHE[cleaned] = rendered
            return rendered
    except Exception as exc:
        rendered = Page(cleaned or url, 0, "", "", [], int((time.perf_counter() - start) * 1000), f"render_error:{type(exc).__name__}")
        _RENDER_CACHE[cleaned] = rendered
        return rendered
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def should_try_rendered(page: Page, bucket: str) -> bool:
    if page.status in {0, 403, 406}:
        return True
    if page.status != 200 or is_soft_404(page) or is_broad_landing(clean_url(page.url)):
        return False
    text = f"{getattr(page, 'text', '') or ''} {getattr(page, 'title', '') or ''}"
    blob = norm(text)
    if has_public_selection_signal(blob, bucket) and strong_bucket_signal(text, bucket) and bucket_event_signal(text, bucket):
        return False
    return route_signal_score(page, bucket) >= 35


def verify_url(session: requests.Session, url: str, bucket: str, timeout: int) -> tuple[str, str]:
    """Acepta una URL para el bucket SOLO si el CONTENIDO lo confirma.
    El slug de la ruta es irrelevante: una pagina /documentos que lista PSS
    es valida; una /processos-seletivos vacia (catch-all) no lo es."""
    if not url:
        return "", "empty"
    page = fetch(session, url, timeout)
    used_rendered = False
    if should_try_rendered(page, bucket):
        rendered = fetch_rendered(session, url, timeout)
        if rendered.status == 200 and rendered.text:
            page = rendered
            used_rendered = True
    if page.status != 200:
        return "", f"http_{page.status}"
    if is_soft_404(page):
        return "", "soft_404"

    text = f"{getattr(page, 'text', '') or ''} {getattr(page, 'title', '') or ''}"
    blob = norm(text)
    final = clean_url(page.url)

    # Landing amplio/homepage nunca debe ser la URL final de un bucket.
    # Puede tener menús con señales del bucket, pero no es la página estable.
    if is_broad_landing(final):
        return "", "broad_landing_not_bucket_page"

    if not has_public_selection_signal(blob, bucket):
        return "", "missing_public_selection_signal"

    # Senal fuerte (listado real de editais/eventos) -> aceptar.
    if strong_bucket_signal(text, bucket) and bucket_event_signal(text, bucket):
        note = "verified_strong_rendered" if used_rendered else "verified_strong"
        return normalize_year_filter_url(session, final, bucket, timeout), note

    # Senal media: termino del bucket >=2 veces Y contexto de listado.
    # (un catch-all vacio tiene el termino 1 vez en el menu, no pasa.)
    has_context = any(c in blob for c in [norm(x) for x in LISTING_CONTEXT])
    if bucket_term_count(blob, bucket) >= 2 and has_context and bucket_event_signal(text, bucket):
        note = "verified_medium_rendered" if used_rendered else "verified_medium"
        return normalize_year_filter_url(session, final, bucket, timeout), note

    # Pagina combinada: lista concursos y ademas menciona PSS.
    if bucket == "processos" and strong_bucket_signal(text, "concursos") and re.search(
        r"processos?\s+seletivos?|\bpss\b|selec[ao]es?\s+publicas?", blob
    ):
        note = "verified_combined_rendered" if used_rendered else "verified_combined"
        return normalize_year_filter_url(session, final, bucket, timeout), note

    return "", "missing_content_signal"


def route_signal_score(page: Page, bucket: str) -> int:
    """Score barato para decidir si vale la pena pedir juicio IA.

    No decide que la URL sea buena; solo evita gastar Gemini en paginas sin
    ninguna pinta de concursos/PSS.
    """
    parsed = urlparse(clean_url(page.url))
    route = norm(unquote(f"{parsed.path} {parsed.query}"))
    title = norm(getattr(page, "title", "") or "")
    text = norm((getattr(page, "text", "") or "")[:5000])
    link_blob = norm(
        " ".join(
            compact_space(link.get("text", ""))
            for link in getattr(page, "links", [])[:80]
            if isinstance(link, dict)
        )
    )
    blob = f"{route} {title} {text} {link_blob}"

    score = 0
    if bucket == "processos":
        route_patterns = [r"processo\s+seletivo", r"processos\s+seletivos", r"selec[ao]es?\s+publicas?", r"\bpss\b"]
    else:
        route_patterns = [r"concurso\s+publico", r"concursos\s+publicos", r"\bconcursos?\b"]

    if any(re.search(p, route) for p in route_patterns):
        score += 45
    if any(re.search(p, title) for p in route_patterns):
        score += 30
    if any(re.search(p, link_blob) for p in route_patterns):
        score += 20
    if any(re.search(p, text) for p in route_patterns):
        score += 15
    if any(t in route for t in ["edital", "editais", "documento", "documentos", "publicacao", "publicacoes", "transparencia"]):
        score += 10
    if re.search(r"licitac|pregao|compras|fornecedor|credenciamento", route):
        score -= 35
    return score


def route_ai_allowed(max_route_ai: int) -> bool:
    return max_route_ai <= 0 or _ROUTE_AI_CALLS < max_route_ai


def ai_validate_route(
    session: requests.Session,
    model: str,
    page: Page,
    municipio: str,
    bucket: str,
    timeout: int,
) -> tuple[dict, dict]:
    """Gemini classifica uma pagina candidata sem inventar URL.

    Resultado:
      route_valid: a URL e uma secao oficial estavel para esse bucket.
      content_has_events: a pagina mostra eventos/documentos reais do bucket.
    """
    global _ROUTE_AI_CALLS
    key = api_key()
    if not key:
        raise RuntimeError("missing GEMINI_API_KEY")

    links = []
    for link in getattr(page, "links", [])[:80]:
        if not isinstance(link, dict):
            continue
        href = clean_url(urljoin(page.url, link.get("href", "")))
        text = compact_space(link.get("text", ""))
        if href or text:
            links.append({"text": text[:120], "href": href[:240]})

    bucket_label = "processos seletivos / selecoes publicas / PSS" if bucket == "processos" else "concursos publicos"
    prompt = {
        "task": "Classificar uma URL candidata de prefeitura sem inventar links.",
        "municipio": f"{municipio} ({UF_NOME}, {UF_SIGLA}, Brasil)",
        "bucket": bucket_label,
        "url": clean_url(page.url),
        "title": getattr(page, "title", "") or "",
        "visible_text_excerpt": compact_space((getattr(page, "text", "") or "")[:4500]),
        "links_excerpt": links,
        "rules": [
            "Use somente as evidencias fornecidas. Nao invente nem sugira outra URL.",
            "route_valid=true se a pagina for a secao/listagem oficial e estavel da prefeitura para o bucket, mesmo que esteja vazia ou tenha poucos documentos.",
            "content_has_events=true somente se a pagina mostrar eventos/documentos reais de concurso publico/processo seletivo/PSS/selecao publica.",
            "Se aparecer somente menu, propaganda, pagina inicial, erro/Oops/nao encontramos, licitacoes, pregao, compras, chamamento, credenciamento ou contratos, route_valid=false.",
            "Concursos culturais, soberanas, rainha, princesa, festival ou similares NUNCA contam como evento/documento valido de concurso publico ou processo seletivo de emprego, mesmo se aparecem dentro de uma secao oficial.",
            "Se a pagina for uma secao oficial do bucket mas o unico evento visivel for cultural/soberanas/festival, route_valid=true e content_has_events=false.",
            "Para processos seletivos, nao exija a frase 'edital de abertura'; PSS, processo seletivo simplificado ou selecao publica tambem contam.",
        ],
        "schema": {
            "route_valid": "boolean",
            "content_has_events": "boolean",
            "confidence": "0-1",
            "reason": "frase curta",
        },
    }
    payload = {
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={key}"
    _ROUTE_AI_CALLS += 1
    resp = gemini_post_with_retry(session, url, payload, timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"route_ai_http_{resp.status_code}:{resp.text[:240]}")
    data = resp.json()
    usage = extract_usage(data)
    text = "\n".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    parsed = safe_parse_json_object(text)
    result = {
        "route_valid": bool(parsed.get("route_valid", False)),
        "content_has_events": bool(parsed.get("content_has_events", False)),
        "confidence": str(parsed.get("confidence", "0")),
        "reason": compact_space(str(parsed.get("reason", "")))[:180],
    }
    return result, usage


def verified_specificity(url: str, note: str, bucket: str) -> int:
    """Score a verified URL so order of discovery does not decide the winner."""
    score = 0
    if "verified_strong" in note:
        score += 100
    elif "ai_route_events" in note:
        score += 90
    elif "ai_route_empty" in note:
        score += 55
    elif "verified_combined" in note:
        score += 70
    elif "verified_medium" in note:
        score += 40

    parsed = urlparse(clean_url(url))
    route = re.sub(r"[^a-z0-9]+", " ", norm(unquote(f"{parsed.path} {parsed.query}")))
    if bucket == "processos":
        if re.search(r"processos?\s+seletivos?|selec[ao]es?\s+publicas?|\bpss\b", route):
            score += 25
    else:
        if re.search(r"concursos?\s+publicos?", route) or (
            re.search(r"\bconcurso\b", route)
            and not re.search(r"licitac|chamamento|credenciamento|pregao", route)
        ):
            score += 25

    if re.search(r"\b(publicacao|publicacoes|documento|documentos|transparencia|index|web|home|inicio)\b", route):
        score -= 12
    return score


def bucket_dominance_score(page: Page, bucket: str) -> int:
    """Prefer pages whose visible content is actually dominated by the bucket.

    Some municipal portals keep all menu labels on every page. A concurso page
    can therefore mention "processo seletivo" in the menu, and a rendered table
    can make the page look valid for both buckets. This score is only a
    tie-breaker: it favors pages with real events for the requested bucket and
    penalizes pages where the opposite bucket is the real body content.
    """
    text = f"{getattr(page, 'text', '') or ''} {getattr(page, 'title', '') or ''}"
    blob = norm(text)
    wanted_events = bucket_event_signal(text, bucket)
    other = "concursos" if bucket == "processos" else "processos"
    other_events = bucket_event_signal(text, other)

    score = 0
    if wanted_events:
        score += 18
    if other_events and not wanted_events:
        score -= 45

    wanted_count = bucket_term_count(blob, bucket)
    other_count = bucket_term_count(blob, other)
    if wanted_count > other_count:
        score += min(18, (wanted_count - other_count) * 3)
    elif other_count > wanted_count:
        score -= min(30, (other_count - wanted_count) * 3)
    return score


def visible_event_count(text: str, bucket: str) -> int:
    """Estimate how many real events/documents a candidate page exposes."""
    blob = norm(text)
    counts: list[int] = []
    for match in re.finditer(r"(?:exibindo|showing)\s+\d+\s*(?:-|a|to)\s*\d+\s*(?:de|of)\s*(\d+)", blob):
        try:
            counts.append(int(match.group(1)))
        except ValueError:
            pass
    for match in re.finditer(r"\b(\d+)\s+(?:itens?|items?|entries)\b", blob):
        try:
            counts.append(int(match.group(1)))
        except ValueError:
            pass

    number_year = r"(?:n\s*\.?\s*[oº°]?\s*)?\d{1,4}\s*(?:[/-]|\s+)\s*\d{4}"
    if bucket == "processos":
        event_re = (
            rf"processo\s+seletivo(?:\s+simplificado)?[^.\n]{{0,90}}{number_year}"
            rf"|{number_year}[^.\n]{{0,90}}processo\s+seletivo"
            rf"|\bpss\b[^.\n]{{0,90}}{number_year}"
            rf"|selec[ao]\s+publica[^.\n]{{0,90}}{number_year}"
        )
    else:
        event_re = rf"concurso\s+publico[^.\n]{{0,100}}{number_year}|{number_year}[^.\n]{{0,100}}concurso\s+publico"
    event_hits = len(re.findall(event_re, blob))
    if event_hits:
        counts.append(event_hits)
    return max(counts or [0])


def most_recent_year(text: str) -> int:
    years = [int(y) for y in re.findall(r"\b(20(?:1[4-9]|2[0-6]))\b", text or "")]
    return max(years or [0])


def repeated_query_filter_penalty(url: str) -> int:
    parsed = urlparse(clean_url(url))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not pairs:
        return 0
    seen: dict[str, set[str]] = {}
    for key, value in pairs:
        nk = norm(unquote(key))
        if "categoria" in nk or "tipo" in nk:
            seen.setdefault(nk, set()).add(norm(unquote(value)))
    repeated_keys = sum(1 for values in seen.values() if len(values) > 1)
    duplicate_category_keys = max(0, sum(1 for key, _ in pairs if "categoria" in norm(unquote(key))) - 1)
    return repeated_keys * 22 + duplicate_category_keys * 8


def process_family_score(route: str, title: str, text: str, bucket: str) -> tuple[int, str]:
    blob = f"{route} {title} {text}"
    if bucket == "processos":
        if re.search(r"processos?\s+seletivos?\s+simplificados?|processo\s+seletivo\s+simplificado|\bpss\b", blob):
            return 85, "pss"
        if re.search(r"processos?\s+seletivos?", blob):
            return 58, "processo_seletivo"
        if re.search(r"selec[ao]es?\s+publicas?", blob):
            return 45, "selecao_publica"
        if re.search(r"contratac(?:ao|oes)", blob):
            return 14, "contratacao_generica"
        if re.search(r"chamada\s+publica", blob) and re.search(r"cargo|emprego|contratacao\s+temporaria|professor|servidor", blob):
            return 24, "chamada_pessoal"
        return 0, "sem_familia"

    if re.search(r"concursos?\s+publicos?", blob):
        return 75, "concurso_publico"
    if re.search(r"\bconcurso\b", blob) and re.search(r"edital|inscric(?:ao|oes)|prova|cargo", blob):
        return 48, "concurso_generico"
    if re.search(r"contratac(?:ao|oes)", blob):
        return 8, "contratacao_generica"
    return 0, "sem_familia"


def candidate_page_quality(page: Page, bucket: str, url: str | None = None) -> tuple[int, str]:
    """Rank sibling category pages by usefulness, not just validity.

    This keeps a valid but generic page from winning over a more specific menu
    category such as "Processos Seletivos Simplificados".
    """
    final_url = clean_url(url or getattr(page, "url", "") or "")
    parsed = urlparse(final_url)
    raw_route = unquote(f"{parsed.path} {parsed.query}").lower()
    route = norm(raw_route)
    title = norm(getattr(page, "title", "") or "")
    text_raw = f"{getattr(page, 'text', '') or ''} {getattr(page, 'title', '') or ''}"
    text = norm(text_raw[:40_000])

    family_score, family = process_family_score(route, title, text[:8000], bucket)
    events = visible_event_count(text_raw, bucket)
    year = most_recent_year(text_raw)
    score = family_score
    reasons = [f"family={family}", f"events={events}", f"year={year or '-'}"]

    if events:
        score += min(55, events * 4)
    if year >= 2026:
        score += 24
    elif year == 2025:
        score += 15
    elif year >= 2020:
        score += 8

    if bucket == "processos" and re.search(r"processo\s+seletivo\s+simplificado|\bpss\b", text[:2500]):
        score += 22
        reasons.append("exact_pss_body")
    if bucket == "concursos" and re.search(r"concursos?\s+publicos?", text[:2500]):
        score += 18
        reasons.append("exact_concurso_body")

    generic_route = bool(re.search(r"\b(contratacao|contratacoes|documento|documentos|publicacao|publicacoes|transparencia|editais?)\b", route))
    specific_route = bool(
        re.search(
            r"processos?\s+seletivos?|selec[ao]es?\s+publicas?|\bpss\b|concursos?\s+publicos?|\bconcurso\b",
            route,
        )
    )
    if generic_route and not specific_route:
        score -= 18
        reasons.append("generic_route")
    elif specific_route:
        score += 12
        reasons.append("specific_route")

    filter_penalty = repeated_query_filter_penalty(final_url)
    if filter_penalty:
        score -= filter_penalty
        reasons.append(f"mixed_filters=-{filter_penalty}")

    if is_detail_or_news_url(final_url):
        score -= 120
        reasons.append("detail_or_news_penalty")
    is_multi24_dyn = "multi24" in route and "secao dinamico" in route
    is_delegated_detail = is_multi24_dyn and re.search(r"(?:^|[?&\s])entidade\s*(?:=|\s)\s*\d+", raw_route)
    is_delegated_category = (
        is_multi24_dyn
        and not is_delegated_detail
        and re.search(r"(?:^|[?&\s])id\s*(?:=|\s)\s*\d+", raw_route)
    )
    if is_delegated_category:
        score += 75
        reasons.append("delegated_category_parent")
    if is_delegated_detail:
        score -= 140
        reasons.append("delegated_detail_penalty")

    if re.search(r"licitac|pregao|compras|fornecedor|credenciamento|contratos", route) and family in {
        "contratacao_generica",
        "sem_familia",
    }:
        score -= 45
        reasons.append("procurement_penalty")

    return score, ",".join(reasons)


def best_verified(
    session: requests.Session,
    urls: list[str],
    municipio: str,
    site_base: str,
    bucket: str,
    timeout: int,
    *,
    model: str = "",
    ai_timeout: int = 60,
    enable_route_ai: bool = False,
    max_route_ai: int = 0,
    route_ai_candidates: int = 2,
    usage_sink: dict | None = None,
    notes_sink: list[str] | None = None,
    source_labels: dict[str, list[str]] | None = None,
) -> tuple[str, str]:
    """Verify all candidates and return the strongest one by content specificity."""
    best_url, best_note, best_score = "", "", -10**9
    ai_candidates: list[tuple[int, str, Page, str]] = []
    seen: set[str] = set()
    for raw in urls:
        u = clean_url(raw)
        if not u or u in seen:
            continue
        seen.add(u)
        if not host_matches_municipio_rs(u, municipio, site_base):
            continue
        verified, note = verify_url(session, u, bucket, timeout)
        if verified:
            page = fetch(session, verified, timeout)
            if should_try_rendered(page, bucket):
                rendered = fetch_rendered(session, verified, timeout)
                if rendered.status == 200 and rendered.text:
                    page = rendered
            quality, quality_note = candidate_page_quality(page, bucket, verified)
            score = verified_specificity(verified, note, bucket)
            score += bucket_dominance_score(page, bucket)
            score += quality
            source_bonus, source_note = source_label_score(source_labels.get(u, []) if source_labels else [], bucket, verified)
            score += source_bonus
            if notes_sink is not None:
                notes_sink.append(f"score_{bucket}:{score}:{quality_note}:{source_note}:{verified}")
            if score > best_score:
                best_url, best_note, best_score = verified, note, score
            continue
        if enable_route_ai and model and note in {"missing_public_selection_signal", "missing_content_signal"}:
            page = fetch(session, u, timeout)
            if should_try_rendered(page, bucket):
                rendered = fetch_rendered(session, u, timeout)
                if rendered.status == 200 and rendered.text:
                    page = rendered
            if page.status == 200 and not is_soft_404(page) and not is_broad_landing(clean_url(page.url)):
                signal = route_signal_score(page, bucket)
                if signal >= 35:
                    ai_candidates.append((signal, u, page, note))
    if best_url:
        return best_url, best_note

    if enable_route_ai and model and ai_candidates:
        ai_best_url, ai_best_note, ai_best_score = "", "", -10**9
        for signal, u, page, reject_note in sorted(ai_candidates, key=lambda item: item[0], reverse=True)[
            : max(1, route_ai_candidates)
        ]:
            if not route_ai_allowed(max_route_ai):
                if notes_sink is not None:
                    notes_sink.append(f"route_ai_budget_exhausted_{bucket}")
                break
            try:
                verdict, usage = ai_validate_route(session, model, page, municipio, bucket, ai_timeout)
                add_usage(usage_sink, usage)
                if usage_sink is not None:
                    usage_sink["_route_ai"] = int(usage_sink.get("_route_ai", 0) or 0) + 1
                if notes_sink is not None:
                    notes_sink.append(
                        f"route_ai_{bucket}:{clean_url(page.url)} valid={verdict['route_valid']} "
                        f"events={verdict['content_has_events']} conf={verdict['confidence']} "
                        f"reason={verdict['reason']}"
                    )
                if verdict["route_valid"]:
                    note = "ai_route_events" if verdict["content_has_events"] else "ai_route_empty"
                    quality, quality_note = candidate_page_quality(page, bucket, clean_url(page.url))
                    score = signal + quality + bucket_dominance_score(page, bucket)
                    source_bonus, source_note = source_label_score(
                        source_labels.get(clean_url(page.url), []) if source_labels else [], bucket, clean_url(page.url)
                    )
                    score += source_bonus
                    if notes_sink is not None:
                        notes_sink.append(
                            f"route_ai_score_{bucket}:{score}:{quality_note}:{source_note}:{clean_url(page.url)}"
                        )
                    if score > ai_best_score:
                        ai_best_url = clean_url(page.url)
                        ai_best_note = f"{note}:from_{reject_note}:score_{signal}"
                        ai_best_score = score
            except Exception as exc:
                if notes_sink is not None:
                    notes_sink.append(f"route_ai_error_{bucket}:{type(exc).__name__}:{compact_space(str(exc))[:120]}")
        if ai_best_url:
            return ai_best_url, ai_best_note
    return best_url, best_note


def source_label_score(labels: list[str], bucket: str, url: str = "") -> tuple[int, str]:
    """Score the menu/link text that led us to a URL.

    Some municipalities send official menu buttons to a separate transparency
    portal. The destination page can be noisy, so the origin label is evidence:
    a menu item named "Processos Seletivos Simplificados - PSS" should outrank
    a specific event page that merely repeats many concurso terms.
    """
    blob = norm(" ".join(labels or []))
    score = 0
    reasons: list[str] = []
    if not blob:
        return 0, "source=no_label"

    if bucket == "processos":
        if "processo seletivo simplificado" in blob or re.search(r"\bpss\b", blob):
            score += 150
            reasons.append("source_pss_exact")
        elif "processo seletivo" in blob or "processos seletivos" in blob:
            score += 95
            reasons.append("source_pss")
        elif "selecao publica" in blob or "selecoes publicas" in blob:
            score += 75
            reasons.append("source_selecao_publica")
        elif "contratacao" in blob or "contratacoes" in blob:
            score += 35
            reasons.append("source_contratacao")
    else:
        if "concurso publico" in blob or "concursos publicos" in blob:
            score += 140
            reasons.append("source_concurso_exact")
        elif "concurso" in blob or "concursos" in blob:
            score += 80
            reasons.append("source_concurso")

    if any(t in blob for t in ["publicacoes", "publicacao", "modalidade", "categoria", "transparencia"]):
        score += 35
        reasons.append("source_category")
    label_text = " ".join(labels or [])
    specific_event_label = bool(
        re.search(
            r"\b(?:edital|processo\s+seletivo|processo\s+seletivo\s+simplificado|"
            r"concurso\s+publico|pss)?\s*(?:n\s*[Âºº°o]?\s*)?\d{1,4}\s*(?:[/.\-]|\s+)\s*20\d{2}\b",
            blob,
        )
    )
    broad_bucket_label = any(
        t in blob
        for t in [
            "concurso publico",
            "concursos publicos",
            "processo seletivo",
            "processos seletivos",
            "processo seletivo simplificado",
            "processos seletivos simplificados",
            "selecao publica",
            "selecoes publicas",
        ]
    )
    if broad_bucket_label and not specific_event_label:
        score += 65
        reasons.append("source_parent_category")
    if broad_bucket_label and not specific_event_label and any(
        t in blob for t in ["publicacoes", "publicacao", "modalidade", "categoria", "transparencia"]
    ):
        score += 120
        reasons.append("source_broad_bucket_parent")
    if re.search(r"\b(?:edital|processo seletivo|concurso publico)?\s*n?[ºo]?\s*\d{1,4}[/.-]20\d{2}\b", blob):
        score -= 85
        reasons.append("source_event_detail")
    if re.search(
        r"/\s*(?:concurso\s+publico|processo\s+seletivo|processo\s+seletivo\s+simplificado|pss|edital)\s*(?:n?[Âºo]?\s*)?\d{1,4}[/.-]20\d{2}",
        blob,
    ):
        score -= 120
        reasons.append("source_child_event")
    if specific_event_label and "source_event_detail" not in reasons:
        score -= 85
        reasons.append("source_event_detail")
    if specific_event_label and (" / " in label_text or " | " in label_text) and "source_child_event" not in reasons:
        score -= 80
        reasons.append("source_child_event")
    clean = clean_url(url)
    if clean:
        parsed = urlparse(clean)
        raw_route = unquote(f"{parsed.path} {parsed.query}").lower()
        route = norm(raw_route)
        is_multi24_dyn = "multi24" in route and "secao dinamico" in route
        if is_multi24_dyn and re.search(r"(?:^|[?&\s])entidade\s*(?:=|\s)\s*\d+", raw_route):
            score -= 95
            reasons.append("source_delegated_detail_url")
        elif is_multi24_dyn and re.search(r"(?:^|[?&\s])id\s*(?:=|\s)\s*\d+", raw_route):
            score += 55
            reasons.append("source_delegated_category_url")
    if any(t in blob for t in ["soberana", "soberanas", "cultural", "rainha", "princesa"]):
        score -= 220
        reasons.append("source_non_job_contest")
    if any(t in blob for t in ["licitacao", "pregao", "compras", "fornecedor", "credenciamento"]):
        score -= 90
        reasons.append("source_procurement")
    return score, "source=" + ("+".join(reasons) if reasons else "neutral")


def collect_tier1_candidate_records(
    session: requests.Session, home: Page, municipio: str, bucket: str, timeout: int
) -> tuple[list[str], dict[str, list[str]]]:
    """Collect free-discovery candidates and preserve the source menu/link text."""
    candidates: list[str] = []
    labels: dict[str, list[str]] = {}

    def add_candidate(url: str, label: str = "") -> None:
        clean = clean_url(url)
        if not clean:
            return
        candidates.append(clean)
        if label:
            labels.setdefault(clean, []).append(compact_space(label))

    for link in real_bucket_links(home, bucket):
        add_candidate(link["url"], link.get("text", ""))
    for inter in intermediate_links(home):
        url = clean_url(inter["url"])
        if not host_matches_municipio_rs(url, municipio, home.url):
            continue
        page = fetch(session, url, timeout)
        if page.status != 200 or is_soft_404(page):
            continue
        add_candidate(page.url, inter.get("text", ""))
        for link in real_bucket_links(page, bucket):
            label = " / ".join(x for x in [inter.get("text", ""), link.get("text", "")] if x)
            add_candidate(link["url"], label)

    seen: set[str] = set()
    out: list[str] = []
    for url in candidates:
        clean = clean_url(url)
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out, labels


def collect_tier1_candidates(
    session: requests.Session, home: Page, municipio: str, bucket: str, timeout: int
) -> list[str]:
    """Collect free-discovery candidates before verification/ranking."""
    urls, _labels = collect_tier1_candidate_records(session, home, municipio, bucket, timeout)
    return urls


# --------------------------------------------------------------------------- #
# TIER 1: descubrimiento GRATIS (dominio + links reales del home)
# --------------------------------------------------------------------------- #
def follow_migration_links(session: requests.Session, probes: list[Page], timeout: int, max_follow: int = 4) -> list[Page]:
    """Sigue avisos de 'site novo' (ej.: Acegua -> acegua.atende.net)."""
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
        print(f"      migration hint {matched} on {page.url[:80]}", flush=True)
        for link in getattr(page, "links", []):
            href = clean_url(urljoin(page.url, link.get("href", "")))
            if not href:
                continue
            host = urlparse(href).netloc.lower()
            if not host or host in seen_hosts or any(bad in host for bad in BAD_HOSTS):
                continue
            print(f"      follow migration -> {href[:110]}", flush=True)
            extra.append(fetch(session, href, timeout))
            seen_hosts.add(host)
            followed += 1
            if followed >= max_follow:
                break
    return extra


def discover_official_site_free(session: requests.Session, municipio: str, timeout: int) -> tuple[Page | None, list[Page]]:
    probes = [fetch(session, u, timeout) for u in domain_candidates(municipio)]
    migrated = follow_migration_links(session, probes, timeout)
    probes.extend(migrated)

    good_migrated = [p for p in migrated if p.status == 200 and not is_soft_404(p)]
    if good_migrated:
        print(f"      using migrated site as initial: {good_migrated[0].url[:80]}", flush=True)
        return good_migrated[0], probes

    scored = sorted(((official_site_score(p, municipio), p) for p in probes), key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= 25:
        return scored[0][1], probes

    for p in probes:  # ultimo recurso: cualquier .rs.gov.br/atende del municipio que responda
        if p.status == 200 and not is_soft_404(p) and host_matches_municipio_rs(p.url, municipio):
            return p, probes
    return None, probes


def bucket_link_signal(blob: str, bucket: str) -> bool:
    if bucket == "processos":
        return any(t in blob for t in ["processo seletivo", "processos seletivos", "selecao publica", "selecoes publicas", "pss"])
    return "concurso" in blob


def real_bucket_links(page: Page, bucket: str) -> list[dict[str, str]]:
    """Lee los hrefs REALES del site cuyo texto/href indica el bucket.
    NO adivina rutas: solo links que la prefeitura ya expone."""
    out = []
    seen = set()
    for link in getattr(page, "links", []):
        href = clean_url(urljoin(page.url, link.get("href", "")))
        if not href or href in seen:
            continue
        host = urlparse(href).netloc.lower()
        if any(bad in host for bad in BAD_HOSTS):
            continue
        text = compact_space(link.get("text", ""))
        blob = norm(f"{href} {text}")
        if any(bad in blob for bad in ["licitacao", "pregao", "compras", "fornecedor"]):
            continue
        if bucket_link_signal(blob, bucket):
            seen.add(href)
            out.append({"url": href, "text": text})
    return out


def intermediate_links(page: Page) -> list[dict[str, str]]:
    out = []
    seen = set()
    for link in getattr(page, "links", []):
        href = clean_url(urljoin(page.url, link.get("href", "")))
        if not href or href in seen:
            continue
        host = urlparse(href).netloc.lower()
        if any(bad in host for bad in BAD_HOSTS):
            continue
        text = compact_space(link.get("text", ""))
        blob = norm(f"{href} {text}")
        if any(t in blob for t in INTERMEDIATE_TERMS):
            seen.add(href)
            out.append({"url": href, "text": text})
    return out[:8]


def find_bucket_tier1(
    session: requests.Session,
    home: Page,
    municipio: str,
    bucket: str,
    timeout: int,
    *,
    model: str = "",
    ai_timeout: int = 60,
    enable_route_ai: bool = False,
    max_route_ai: int = 0,
    route_ai_candidates: int = 2,
    usage_sink: dict | None = None,
    notes_sink: list[str] | None = None,
) -> tuple[str, str]:
    candidates, source_labels = collect_tier1_candidate_records(session, home, municipio, bucket, timeout)
    url, note = best_verified(
        session,
        candidates,
        municipio,
        home.url,
        bucket,
        timeout,
        model=model,
        ai_timeout=ai_timeout,
        enable_route_ai=enable_route_ai,
        max_route_ai=max_route_ai,
        route_ai_candidates=route_ai_candidates,
        usage_sink=usage_sink,
        notes_sink=notes_sink,
        source_labels=source_labels,
    )
    if url:
        return url, f"tier1:{note}"
    return "", "tier1_not_found"


# --------------------------------------------------------------------------- #
# TIER 2: descubrimiento con GROUNDING (API + Google)
# --------------------------------------------------------------------------- #
def ground_discover(
    session: requests.Session,
    model: str,
    municipio: str,
    site_hint: str,
    buckets_needed: list[str],
    timeout: int,
) -> tuple[str, list[str], dict]:
    """UNA llamada con google_search. Devuelve (texto, urls_reales, usage).
    Las URLs salen del groundingMetadata (reales), NO del texto del modelo."""
    global _GROUNDING_CALLS
    key = api_key()
    if not key:
        raise RuntimeError("missing GEMINI_API_KEY")

    needed = " e ".join(buckets_needed) if buckets_needed else "concursos publicos e processos seletivos"
    hint = f"O site oficial ja identificado e: {site_hint}. " if site_hint else ""
    prompt_text = (
        f"Voce e um investigador de sites oficiais de prefeituras do estado {UF_NOME} ({UF_SIGLA}), Brasil. "
        f"{hint}"
        f"Encontre no Google as URLs OFICIAIS e ESTAVEIS da prefeitura de "
        f"{municipio} ({UF_NOME}, {UF_SIGLA}, Brasil) para: {needed}.\n"
        "REGRAS:\n"
        f"- Busque SEMPRE incluindo '{UF_NOME}' ou '{UF_SIGLA}' para nao confundir com "
        "municipio homonimo de outro estado ou de Portugal.\n"
        "- Prefira o dominio oficial (.rs.gov.br ou .atende.net do proprio municipio).\n"
        "- A pagina de processos seletivos pode estar dentro de 'Editais', 'Documentos', "
        "'Publicacoes' ou 'Portal da Transparencia': o que importa e o CONTEUDO, nao o nome da rota.\n"
        "- NAO use licitacoes, pregao, compras nem chamamento publico.\n"
        "Liste claramente, com a URL completa de cada uma: site oficial; pagina de concursos "
        "publicos; pagina de processos seletivos."
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "tools": [{"google_search": {}}],
        # SIN responseMimeType: grounding + JSON forzado vacia el groundingMetadata.
        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 2048},
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={key}"
    resp = gemini_post_with_retry(session, url, payload, timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"ground_http_{resp.status_code}:{resp.text[:240]}")
    _GROUNDING_CALLS += 1
    data = resp.json()
    usage = extract_usage(data)
    cand = (data.get("candidates") or [{}])[0]
    text = "\n".join(p.get("text", "") for p in (cand.get("content", {}) or {}).get("parts", []) if isinstance(p, dict))

    chunks = ((cand.get("groundingMetadata", {}) or {}).get("groundingChunks", []) or [])
    resolved: list[str] = []
    seen = set()
    for ch in chunks:
        uri = ((ch.get("web", {}) or {}).get("uri", "")) if isinstance(ch, dict) else ""
        if not uri:
            continue
        real = resolve_redirect(session, uri, timeout)
        host = urlparse(real).netloc.lower()
        if real and real not in seen and not any(bad in host for bad in BAD_HOSTS):
            seen.add(real)
            resolved.append(real)
    print(f"      grounding chunks={len(chunks)} resolved={len(resolved)}", flush=True)
    return text, resolved, usage


def structure_choice(
    session: requests.Session,
    model: str,
    municipio: str,
    grounded_text: str,
    candidate_urls: list[str],
    timeout: int,
) -> tuple[dict, dict]:
    """Llamada NO-grounded (barata) que mapea las URLs reales a las columnas.
    Elige SOLO de candidate_urls; no inventa."""
    key = api_key()
    if not key:
        raise RuntimeError("missing GEMINI_API_KEY")
    prompt = {
        "task": "Mapear URLs reais para as colunas, escolhendo APENAS da lista candidate_urls.",
        "municipio": f"{municipio} ({UF_NOME}, {UF_SIGLA}, Brasil)",
        "rules": [
            "Escolha apenas URLs presentes em candidate_urls. NAO invente nem modifique URLs.",
            "site_base = dominio oficial da prefeitura.",
            "url_concursos = pagina que lista concursos publicos.",
            "url_processos_seletivos = pagina que lista processos seletivos / PSS / selecoes publicas.",
            "Se a mesma pagina servir para ambos, repita a URL.",
            "Se nenhuma URL servir para uma coluna, deixe vazio.",
            "open_next deve ser SEMPRE uma lista (pode ser []).",
            "Responda somente JSON valido.",
        ],
        "grounded_text": (grounded_text or "")[:4000],
        "candidate_urls": candidate_urls,
        "schema": {
            "site_base": "url ou vazio",
            "url_concursos": "url ou vazio",
            "url_processos_seletivos": "url ou vazio",
            "confidence": "0-1",
            "reason": "curto",
            "open_next": ["urls"],
        },
    }
    payload = {
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024, "responseMimeType": "application/json"},
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={key}"
    resp = gemini_post_with_retry(session, url, payload, timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"structure_http_{resp.status_code}:{resp.text[:240]}")
    data = resp.json()
    usage = extract_usage(data)
    text = "\n".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    try:
        return coerce_choice(safe_parse_json_object(text)), usage
    except Exception as exc:
        print(f"      structure JSON parse failed; retrying compact mapping: {exc}", flush=True)
        compact_prompt = {
            "task": "Escolha URLs para prefeitura, concursos e processos seletivos.",
            "municipio": f"{municipio} ({UF_NOME}, {UF_SIGLA}, Brasil)",
            "rules": [
                "Use somente URLs de candidate_urls.",
                "Nao invente URLs.",
                "Se nao souber, deixe vazio.",
                "Responda JSON valido e curto.",
            ],
            "candidate_urls": candidate_urls,
            "schema": {
                "site_base": "",
                "url_concursos": "",
                "url_processos_seletivos": "",
                "confidence": "0-1",
                "reason": "",
                "open_next": [],
            },
        }
        repair_payload = {
            "contents": [{"role": "user", "parts": [{"text": json.dumps(compact_prompt, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 512, "responseMimeType": "application/json"},
        }
        repair = gemini_post_with_retry(session, url, repair_payload, timeout)
        if repair.status_code >= 400:
            raise RuntimeError(f"structure_repair_http_{repair.status_code}:{repair.text[:240]}") from exc
        repair_data = repair.json()
        repair_usage = extract_usage(repair_data)
        usage = {
            "input": usage["input"] + repair_usage["input"],
            "output": usage["output"] + repair_usage["output"],
            "total": usage["total"] + repair_usage["total"],
        }
        repair_text = "\n".join(p.get("text", "") for p in repair_data["candidates"][0]["content"]["parts"])
        return coerce_choice(safe_parse_json_object(repair_text)), usage


def grounding_allowed(max_grounding: int) -> bool:
    return max_grounding <= 0 or _GROUNDING_CALLS < max_grounding


def status_from_note(url: str, note: str) -> str:
    if not url:
        return "nao_encontrada"
    if "ai_route_empty" in note:
        return "boa_sem_eventos"
    return "boa"


# --------------------------------------------------------------------------- #
# Orquestacion por municipio (la cascada)
# --------------------------------------------------------------------------- #
def build_row(args: argparse.Namespace, municipio_row: dict[str, str], index: int, total: int) -> dict[str, str]:
    municipio = title_case_municipio(repair_text_encoding(municipio_row["municipio"]))
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )
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
        "method": "free_tier",
        "notes": "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        # internos (extrasaction="ignore" los descarta del CSV)
        "_tokens_in": 0,
        "_tokens_out": 0,
        "_tokens_total": 0,
        "_grounded": 0,
        "_route_ai": 0,
        "_elapsed": 0.0,
    }
    notes: list[str] = []
    t0 = time.time()
    try:
        # ---------- TIER 1: gratis ----------
        print(f"[{index}/{total}] {municipio}: TIER 1 (free discovery)", flush=True)
        home, _probes = discover_official_site_free(session, municipio, args.timeout)
        site_base = clean_url(home.url) if home else ""
        if site_base:
            row["site_base"] = site_base
            row["site_status"] = "boa"

        conc_url = pss_url = ""
        conc_note = pss_note = "tier1_no_site"
        if home:
            conc_url, conc_note = find_bucket_tier1(
                session,
                home,
                municipio,
                "concursos",
                args.timeout,
                model=args.model,
                ai_timeout=args.ai_timeout,
                enable_route_ai=args.ai_route_validator,
                max_route_ai=args.max_route_ai,
                route_ai_candidates=args.route_ai_candidates,
                usage_sink=row,
                notes_sink=notes,
            )
            pss_url, pss_note = find_bucket_tier1(
                session,
                home,
                municipio,
                "processos",
                args.timeout,
                model=args.model,
                ai_timeout=args.ai_timeout,
                enable_route_ai=args.ai_route_validator,
                max_route_ai=args.max_route_ai,
                route_ai_candidates=args.route_ai_candidates,
                usage_sink=row,
                notes_sink=notes,
            )
            notes.append(f"t1_conc={conc_note}; t1_pss={pss_note}")
        else:
            notes.append("t1_no_site")

        # ---------- TIER 2: grounding (solo si falta algo) ----------
        def is_weak_note(note: str) -> bool:
            return "verified_medium" in note or "container" in note

        needed = []
        if not conc_url or (args.ground_weak and is_weak_note(conc_note)):
            needed.append("concursos publicos")
        if not pss_url or (args.ground_weak and is_weak_note(pss_note)):
            needed.append("processos seletivos")

        if needed and grounding_allowed(args.max_grounding):
            print(f"[{index}/{total}] {municipio}: TIER 2 (grounding) needed={needed}", flush=True)
            row["method"] = "grounded"
            row["_grounded"] = 1
            try:
                text, ground_urls, ug = ground_discover(
                    session, args.model, municipio, site_base, needed, args.ai_timeout
                )
                row["_tokens_in"] += ug["input"]
                row["_tokens_out"] += ug["output"]
                row["_tokens_total"] += ug["total"]

                # Gemini grounded solo descubre URLs. El codigo decide si sirven.
                candidate_urls = list(dict.fromkeys([u for u in ([site_base] + ground_urls) if u]))
                row["confidence"] = "0.65" if ground_urls else row["confidence"]

                # site_base si Tier 1 no lo hallo
                if not row["site_base"]:
                    for raw_site in candidate_urls:
                        cand_site = clean_url(raw_site)
                        if host_matches_municipio_rs(cand_site, municipio):
                            parsed = urlparse(cand_site)
                            row["site_base"] = f"{parsed.scheme}://{parsed.netloc}/"
                            row["site_status"] = "boa"
                            notes.append("t3_site_from_grounding")
                            break

                # ---------- TIER 3: rankear las URLs del grounding ----------
                if "concursos publicos" in needed:
                    g_url, g_note = best_verified(
                        session,
                        candidate_urls,
                        municipio,
                        row["site_base"],
                        "concursos",
                        args.timeout,
                        model=args.model,
                        ai_timeout=args.ai_timeout,
                        enable_route_ai=args.ai_route_validator,
                        max_route_ai=args.max_route_ai,
                        route_ai_candidates=args.route_ai_candidates,
                        usage_sink=row,
                        notes_sink=notes,
                    )
                    if g_url and (
                        not conc_url
                        or verified_specificity(g_url, g_note, "concursos")
                        > verified_specificity(conc_url, conc_note, "concursos")
                    ):
                        conc_url, conc_note = g_url, f"grounded:{g_note}"
                        notes.append(f"t3_conc={g_note}")
                    elif not conc_url:
                        notes.append("t3_conc_not_verified")

                if "processos seletivos" in needed:
                    g_url, g_note = best_verified(
                        session,
                        candidate_urls,
                        municipio,
                        row["site_base"],
                        "processos",
                        args.timeout,
                        model=args.model,
                        ai_timeout=args.ai_timeout,
                        enable_route_ai=args.ai_route_validator,
                        max_route_ai=args.max_route_ai,
                        route_ai_candidates=args.route_ai_candidates,
                        usage_sink=row,
                        notes_sink=notes,
                    )
                    if g_url and (
                        not pss_url
                        or verified_specificity(g_url, g_note, "processos")
                        > verified_specificity(pss_url, pss_note, "processos")
                    ):
                        pss_url, pss_note = g_url, f"grounded:{g_note}"
                        notes.append(f"t3_pss={g_note}")
                    elif not pss_url:
                        notes.append("t3_pss_not_verified")
            except Exception as gexc:
                notes.append(f"grounding_error:{type(gexc).__name__}:{compact_space(str(gexc))[:140]}")
                traceback.print_exc()
        elif needed:
            notes.append("grounding_skipped_budget")

        # ---------- finalizar ----------
        row["url_concursos"] = conc_url
        row["status_concursos"] = status_from_note(conc_url, conc_note)
        row["url_processos_seletivos"] = pss_url
        row["status_processos_seletivos"] = status_from_note(pss_url, pss_note)
        row["notes"] = compact_space("; ".join(notes))[:900]
    except Exception as exc:
        row["notes"] = f"error:{type(exc).__name__}:{compact_space(str(exc))[:300]}"
        traceback.print_exc()
    finally:
        row["_elapsed"] = round(time.time() - t0, 1)

    print(
        f"[{index}/{total}] {municipio}: site={row['site_status']} "
        f"conc={row['status_concursos']} pss={row['status_processos_seletivos']} "
        f"| method={row['method']} tokens={row['_tokens_total']} | {row['_elapsed']}s :: {row['notes'][:90]}",
        flush=True,
    )
    return row


# --------------------------------------------------------------------------- #
# I/O y stats
# --------------------------------------------------------------------------- #
def load_existing_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def print_run_stats(rows: list[dict]) -> None:
    this_run = [r for r in rows if "_tokens_total" in r]
    if not this_run:
        return
    n = len(this_run)
    sum_in = sum(int(r.get("_tokens_in", 0) or 0) for r in this_run)
    sum_out = sum(int(r.get("_tokens_out", 0) or 0) for r in this_run)
    sum_total = sum(int(r.get("_tokens_total", 0) or 0) for r in this_run)
    sum_time = sum(float(r.get("_elapsed", 0) or 0) for r in this_run)
    grounded = sum(1 for r in this_run if int(r.get("_grounded", 0) or 0))
    free = n - grounded

    cost_tokens = sum_in * PRICE_IN_PER_TOKEN + sum_out * PRICE_OUT_PER_TOKEN
    cost_ground = grounded * PRICE_GROUNDING_PER_PROMPT
    cost_run = cost_tokens + cost_ground
    avg_cost = cost_run / n if n else 0
    proj_cost = avg_cost * TOTAL_MUNICIPIOS_RS

    print("-" * 72, flush=True)
    print(f"CASCADE: free_tier={free} grounded={grounded} (n={n})  grounding_calls={_GROUNDING_CALLS}", flush=True)
    print(
        f"AVERAGES/municipio: tokens in={sum_in / n:.0f} out={sum_out / n:.0f} "
        f"total={sum_total / n:.0f} | tiempo={sum_time / n:.1f}s",
        flush=True,
    )
    print(
        f"COSTO este run=${cost_run:.4f} (tokens=${cost_tokens:.4f} + grounding=${cost_ground:.4f}) | "
        f"promedio/muni=${avg_cost:.5f} | proyeccion {TOTAL_MUNICIPIOS_RS} muni=${proj_cost:.2f}",
        flush=True,
    )
    print("-" * 72, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "municipios_resources_a_grounded.csv")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--ai-timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--max-grounding",
        type=int,
        default=0,
        help="Tope de prompts de grounding para todo el run (0 = sin tope). Guard de presupuesto.",
    )
    parser.add_argument(
        "--ground-weak",
        action="store_true",
        help="Disparar grounding tambien cuando Tier 1 solo encontro match debil.",
    )
    parser.add_argument(
        "--ai-route-validator",
        action="store_true",
        help="Usar Gemini sem grounding para validar rutas ambiguas que las reglas rechazaron.",
    )
    parser.add_argument(
        "--max-route-ai",
        type=int,
        default=20,
        help="Tope de validaciones IA de rutas ambiguas por corrida (0 = sin tope).",
    )
    parser.add_argument(
        "--route-ai-candidates",
        type=int,
        default=2,
        help="Maximo de candidatos ambiguos por bucket que Gemini puede revisar.",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    municipios = [r for r in load_municipios(args.timeout) if norm(r["municipio"]).startswith("a")]
    municipios.sort(key=lambda r: norm(r["municipio"]))
    if args.offset:
        municipios = municipios[args.offset :]
    if args.limit:
        municipios = municipios[: args.limit]
    print(f"MUNICIPIOS_A {len(municipios)} offset={args.offset} model={args.model} max_grounding={args.max_grounding}", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = load_existing_rows(args.output) if args.resume else []
    done = {(row.get("municipio", ""), row.get("ibge", "")) for row in rows}
    mode = "a" if args.resume and args.output.exists() and args.output.stat().st_size > 0 else "w"
    with args.output.open(mode, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        for idx, row in enumerate(municipios, start=1):
            key = (title_case_municipio(repair_text_encoding(row["municipio"])), row.get("ibge", ""))
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
        f"concursos_sem_eventos={sum(1 for r in rows if r['status_concursos']=='boa_sem_eventos')} "
        f"processos_boa={sum(1 for r in rows if r['status_processos_seletivos']=='boa')} "
        f"processos_sem_eventos={sum(1 for r in rows if r['status_processos_seletivos']=='boa_sem_eventos')}",
        flush=True,
    )
    print_run_stats(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
