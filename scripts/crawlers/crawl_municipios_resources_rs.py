from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MUNICIPIOS_URL = "https://dados.tce.rs.gov.br/dados/auxiliar/municipios.csv"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_LOCK = threading.Lock()


FIELDS = [
    "uf",
    "municipio",
    "ibge",
    "site_base",
    "url_concursos",
    "url_processos_seletivos",
    "url_editais",
    "url_convocacoes",
    "url_diario_publicacoes",
    "status_concursos",
    "status_processos_seletivos",
    "status_convocacoes",
    "status_diario_publicacoes",
    "confidence",
    "method",
    "gemini_used",
    "notes",
    "checked_at",
]


KEYWORDS = {
    "concursos": [
        "concurso",
        "concursos publicos",
        "concursos pÃºblicos",
        "concurso publico",
        "concurso pÃºblico",
    ],
    "processos": [
        "processo seletivo",
        "processos seletivos",
        "selecao publica",
        "seleÃ§Ã£o pÃºblica",
        "pss",
    ],
    "editais": [
        "edital",
        "editais",
        "licitacao",
        "licitaÃ§Ã£o",
        "publicacoes legais",
        "publicaÃ§Ãµes legais",
    ],
    "convocacoes": [
        "convocacao",
        "convocaÃ§Ã£o",
        "convocacoes",
        "convocaÃ§Ãµes",
        "nomeacao",
        "nomeaÃ§Ã£o",
        "nomeacoes",
        "nomeaÃ§Ãµes",
        "classificados",
    ],
    "diario": [
        "diario oficial",
        "diÃ¡rio oficial",
        "publicacoes oficiais",
        "publicaÃ§Ãµes oficiais",
        "diario dos municipios",
        "diÃ¡rio dos municÃ­pios",
        "famurs",
    ],
}

COMMON_PATHS = [
    "/",
    "/editais",
    "/edital",
    "/editais/concursos-publicos",
    "/editais/processos-seletivos",
    "/concursos",
    "/concurso",
    "/concursos-publicos",
    "/concurso-publico",
    "/processos-seletivos",
    "/processo-seletivo",
    "/processo_seletivo",
    "/selecoes",
    "/selecoes-publicas",
    "/publicacoes",
    "/publicacoes-oficiais",
    "/diario-oficial",
    "/portal/diario-oficial",
    "/convocacoes",
    "/nomeacoes",
]


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self._current_href = href
                self._current_text = []
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = compact_space(" ".join(self._current_text))
            self.links.append({"href": self._current_href, "text": text})
            self._current_href = None
            self._current_text = []
        elif tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)
        if self._in_title:
            self.title_parts.append(data)


@dataclass
class PageProbe:
    url: str
    status: int
    title: str
    text: str
    links: list[dict[str, str]]
    elapsed_ms: int
    error: str = ""


def compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def norm(value: str) -> str:
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
    parts = []
    for part in compact_space(name).lower().split():
        parts.append(part if part in small else part[:1].upper() + part[1:])
    return " ".join(parts)


def clean_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", parsed.query, ""))


def score_url_text(url: str, text: str, bucket: str) -> int:
    blob = norm(f"{url} {text}")
    score = 0
    for keyword in KEYWORDS[bucket]:
        if norm(keyword) in blob:
            score += 5
    path = norm(urlparse(url).path)
    if bucket == "concursos" and "concurso" in path:
        score += 8
    if bucket == "processos" and ("processo seletivo" in blob or "processos seletivos" in blob or "pss" in blob):
        score += 8
    if bucket == "convocacoes" and any(x in blob for x in ["convocacao", "convocacoes", "nomeacao", "nomeacoes"]):
        score += 8
    if bucket == "diario" and any(x in blob for x in ["diario oficial", "publicacoes oficiais", "famurs"]):
        score += 8
    if bucket == "editais" and "edital" in blob:
        score += 5
    if any(bad in blob for bad in ["peao", "prenda", "soberana", "rainha", "rodeio", "turismo"]) and bucket in {"concursos", "processos"}:
        score -= 12
    if any(bad in blob for bad in ["transparencia", "licitacao", "licitacoes", "compras", "pregao", "chamamento publico"]) and bucket in {"concursos", "processos", "convocacoes"}:
        score -= 4
    if any(bad_path in path for bad_path in ["noticia", "noticias", "licitacao", "licitacoes"]) and bucket in {"concursos", "processos", "convocacoes"}:
        score -= 6
    return score


def fetch(session: requests.Session, url: str, timeout: int) -> PageProbe:
    start = time.perf_counter()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        elapsed = int((time.perf_counter() - start) * 1000)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and response.status_code == 200:
            return PageProbe(response.url, response.status_code, "", "", [], elapsed, f"non_html:{content_type}")
        text = response.text[:350_000]
        parser = LinkParser()
        parser.feed(text)
        page_text = compact_space(re.sub(r"<[^>]+>", " ", text))
        return PageProbe(
            clean_url(response.url),
            response.status_code,
            compact_space(" ".join(parser.title_parts)),
            page_text[:6000],
            parser.links,
            elapsed,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return PageProbe(url, 0, "", "", [], elapsed, type(exc).__name__)


def load_municipios(path: Path | None, timeout: int) -> list[dict[str, str]]:
    if path and path.exists():
        rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    else:
        response = requests.get(DEFAULT_MUNICIPIOS_URL, timeout=timeout)
        response.raise_for_status()
        rows = list(csv.DictReader(response.text.splitlines()))
    out = []
    for row in rows:
        uf = row.get("UF", "RS")
        name = row.get("NOME_MUNICIPIO") or row.get("municipio") or row.get("nome") or ""
        ibge = row.get("CD_MUNICIPIO_IBGE") or row.get("ibge") or ""
        if uf == "RS" and name:
            out.append({"uf": "RS", "municipio": title_case_municipio(name), "ibge": ibge})
    return out


def site_candidates(municipio: str) -> list[str]:
    slug = slugify_municipio(municipio)
    return [
        f"https://www.{slug}.rs.gov.br",
        f"https://{slug}.rs.gov.br",
        f"https://www.prefeitura{slug}.rs.gov.br",
        f"https://prefeitura{slug}.rs.gov.br",
    ]


def discover_site(session: requests.Session, municipio: str, timeout: int) -> PageProbe | None:
    probes = [fetch(session, url, timeout) for url in site_candidates(municipio)]
    good = [p for p in probes if p.status in {200, 301, 302} and p.text]
    if not good:
        return None
    muni_norm = norm(municipio)
    good.sort(key=lambda p: (muni_norm not in norm(p.url + " " + p.title + " " + p.text[:500]), p.elapsed_ms))
    return good[0]


def candidate_urls_from_home(home: PageProbe) -> list[str]:
    urls: dict[str, str] = {}
    base = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"
    for path in COMMON_PATHS:
        urls[clean_url(urljoin(base, path))] = ""
    for link in home.links:
        href = link.get("href", "")
        text = link.get("text", "")
        abs_url = clean_url(urljoin(home.url, href))
        parsed = urlparse(abs_url)
        if parsed.netloc != urlparse(home.url).netloc:
            continue
        blob = norm(abs_url + " " + text)
        if any(norm(k) in blob for words in KEYWORDS.values() for k in words):
            urls[abs_url] = text
    return list(urls.keys())[:80]


def probe_candidates(session: requests.Session, home: PageProbe, timeout: int, workers: int) -> list[PageProbe]:
    urls = candidate_urls_from_home(home)
    probes: list[PageProbe] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, session, url, timeout): url for url in urls}
        for future in as_completed(futures):
            probe = future.result()
            if probe.status == 200 and probe.text:
                probes.append(probe)
    unique: dict[str, PageProbe] = {}
    for probe in probes:
        unique[clean_url(probe.url)] = probe
    return list(unique.values())


def best_by_bucket(candidates: list[PageProbe], bucket: str) -> tuple[str, str]:
    ranked = []
    for page in candidates:
        score = score_url_text(page.url, page.title + " " + page.text[:1000], bucket)
        if score > 0:
            ranked.append((score, page.url))
    if not ranked:
        return "", "nao_encontrada"
    ranked.sort(reverse=True)
    status = "boa" if ranked[0][0] >= 8 else "revisar"
    return ranked[0][1], status


def parse_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if isinstance(parsed, list):
        if not parsed:
            return {}
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


def gemini_choose(args: argparse.Namespace, municipio: str, home: PageProbe, candidates: list[PageProbe]) -> dict[str, Any] | None:
    api_key = os.environ.get(args.gemini_api_key_env) or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    cache_dir = Path(args.cache_dir) / "municipios_gemini"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        (args.gemini_model + "|" + municipio + "|" + home.url + "|" + "|".join(sorted(p.url for p in candidates))).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists() and not args.refresh_ai_cache:
        try:
            return parse_json_object(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache_path.unlink(missing_ok=True)

    compact = []
    for page in candidates[:args.max_candidates_for_ai]:
        compact.append(
            {
                "url": page.url,
                "title": page.title[:160],
                "text": compact_space(page.text)[:700],
            }
        )
    prompt = {
        "municipio": municipio,
        "site_base": home.url,
        "task": "Escolha URLs oficiais estÃ¡veis do site municipal para concursos pÃºblicos, processos seletivos, editais gerais, convocaÃ§Ãµes/nomeaÃ§Ãµes e diÃ¡rio/publicaÃ§Ãµes oficiais. NÃ£o invente URL. Use apenas candidates.",
        "rules": [
            "url_concursos deve apontar para lista/seÃ§Ã£o de concursos pÃºblicos, nÃ£o para uma notÃ­cia individual se houver seÃ§Ã£o.",
            "url_processos_seletivos deve apontar para lista/seÃ§Ã£o de processos seletivos/PSS.",
            "url_convocacoes deve apontar para lista/seÃ§Ã£o de convocaÃ§Ãµes/nomeaÃ§Ãµes/classificados.",
            "url_diario_publicacoes deve apontar para diÃ¡rio oficial/publicaÃ§Ãµes oficiais se existir.",
            "Se a rota parece genÃ©rica demais ou incerta, marque status como revisar.",
            "Responda somente JSON vÃ¡lido.",
        ],
        "candidates": compact,
        "schema": {
            "url_concursos": "string",
            "url_processos_seletivos": "string",
            "url_editais": "string",
            "url_convocacoes": "string",
            "url_diario_publicacoes": "string",
            "status_concursos": "boa|revisar|nao_encontrada",
            "status_processos_seletivos": "boa|revisar|nao_encontrada",
            "status_convocacoes": "boa|revisar|nao_encontrada",
            "status_diario_publicacoes": "boa|revisar|nao_encontrada",
            "confidence": "0-1",
            "notes": "curto",
        },
    }
    url = f"{args.gemini_base_url}/models/{args.gemini_model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    with GEMINI_LOCK:
        response = requests.post(url, json=payload, timeout=args.ai_timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"gemini_http_{response.status_code}:{response.text[:240]}")
        time.sleep(args.ai_delay)
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = parse_json_object(text)
    cache_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return parsed


def build_row(args: argparse.Namespace, municipio_row: dict[str, str]) -> dict[str, str]:
    municipio = municipio_row["municipio"]
    checked_at = datetime.now(timezone.utc).isoformat()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 concursos-rs-resource-mapper/0.1"})
    home = discover_site(session, municipio, args.timeout)
    base = {
        "uf": "RS",
        "municipio": municipio,
        "ibge": municipio_row.get("ibge", ""),
        "site_base": home.url if home else "",
        "url_concursos": "",
        "url_processos_seletivos": "",
        "url_editais": "",
        "url_convocacoes": "",
        "url_diario_publicacoes": "",
        "status_concursos": "nao_encontrada",
        "status_processos_seletivos": "nao_encontrada",
        "status_convocacoes": "nao_encontrada",
        "status_diario_publicacoes": "nao_encontrada",
        "confidence": "0",
        "method": "site_not_found",
        "gemini_used": "0",
        "notes": "",
        "checked_at": checked_at,
    }
    if not home:
        return base

    candidates = probe_candidates(session, home, args.timeout, args.inner_workers)
    if args.debug:
        print(f"[{municipio}] site={home.url} candidates={len(candidates)}", flush=True)
    for out_field, status_field, bucket in [
        ("url_concursos", "status_concursos", "concursos"),
        ("url_processos_seletivos", "status_processos_seletivos", "processos"),
        ("url_editais", "status_diario_publicacoes", "editais"),
        ("url_convocacoes", "status_convocacoes", "convocacoes"),
        ("url_diario_publicacoes", "status_diario_publicacoes", "diario"),
    ]:
        url, status = best_by_bucket(candidates, bucket)
        base[out_field] = url
        if out_field == "url_editais":
            continue
        base[status_field] = status
    base["method"] = "heuristic"
    base["confidence"] = "0.55" if any(base[k] for k in ["url_concursos", "url_processos_seletivos", "url_convocacoes", "url_diario_publicacoes"]) else "0.25"

    should_use_ai = args.use_gemini and candidates and (
        args.force_gemini
        or any(base[k] in {"revisar", "nao_encontrada"} for k in ["status_concursos", "status_processos_seletivos", "status_convocacoes"])
    )
    if should_use_ai:
        try:
            chosen = gemini_choose(args, municipio, home, candidates)
            if chosen:
                candidate_set = {clean_url(p.url) for p in candidates}
                for field in ["url_concursos", "url_processos_seletivos", "url_editais", "url_convocacoes", "url_diario_publicacoes"]:
                    value = clean_url(str(chosen.get(field, "") or ""))
                    if value and value in candidate_set:
                        base[field] = value
                for field in ["status_concursos", "status_processos_seletivos", "status_convocacoes", "status_diario_publicacoes"]:
                    value = str(chosen.get(field, "") or "")
                    if value in {"boa", "revisar", "nao_encontrada"}:
                        base[field] = value
                base["confidence"] = str(chosen.get("confidence", base["confidence"]))
                base["notes"] = compact_space(str(chosen.get("notes", "")))[:400]
                base["method"] = "heuristic+gemini"
                base["gemini_used"] = "1"
        except Exception as exc:
            base["notes"] = f"gemini_error:{type(exc).__name__}:{compact_space(str(exc))[:180]}"
            base["method"] = "heuristic+gemini_error"
    return base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--municipios-csv", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "municipios_resources_rs.csv")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "data" / "cache")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--inner-workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--use-gemini", action="store_true")
    parser.add_argument("--force-gemini", action="store_true")
    parser.add_argument("--gemini-base-url", default="https://generativelanguage.googleapis.com/v1beta")
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--gemini-api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--ai-timeout", type=int, default=45)
    parser.add_argument("--ai-delay", type=float, default=0.2)
    parser.add_argument("--refresh-ai-cache", action="store_true")
    parser.add_argument("--max-candidates-for-ai", type=int, default=30)
    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        user_key = os.popen("powershell -NoProfile -Command \"[Environment]::GetEnvironmentVariable('GEMINI_API_KEY','User')\"").read().strip()
        if user_key:
            os.environ["GEMINI_API_KEY"] = user_key

    municipios = load_municipios(args.municipios_csv, args.timeout)
    municipios.sort(key=lambda row: norm(row["municipio"]))
    if args.limit:
        municipios = municipios[: args.limit]
    print(f"MUNICIPIOS {len(municipios)} use_gemini={args.use_gemini}", flush=True)

    rows: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(build_row, args, row): row for row in municipios}
        completed = 0
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            completed += 1
            print(
                f"[{completed}/{len(municipios)}] {row['municipio']} site={'ok' if row['site_base'] else 'missing'} "
                f"conc={row['status_concursos']} pss={row['status_processos_seletivos']} conv={row['status_convocacoes']} gemini={row['gemini_used']}",
                flush=True,
            )

    rows.sort(key=lambda row: norm(row["municipio"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"WROTE {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
