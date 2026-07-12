from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional runtime dependency
    curl_requests = None

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional runtime dependency
    PdfReader = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "shared"))

from scope_rs import RSScopeRegistry, candidate_rs_evidence, normalize_text, normalize_slug  # noqa: E402


OUT_CSV = PROJECT_ROOT / "data" / "raw" / "bancas_base_rs_quick.csv"
FIELDS = [
    "semaforo",
    "tipo",
    "orgao",
    "municipio",
    "uf",
    "numero",
    "banca",
    "edital_pagina",
    "edital_pdf",
    "fonte_primaria",
    "fonte_radar",
    "radar_url",
]


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Link:
    url: str
    text: str


@dataclass
class DetailCandidate:
    banca: str
    index_url: str
    detail_url: str
    title: str
    context: str


class AnchorParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[Link] = []
        self._href: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        data = {k.lower(): v or "" for k, v in attrs}
        href = data.get("href", "")
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            return
        self._href = urljoin(self.base_url, html.unescape(href))
        title_parts = [data.get("title", ""), data.get("aria-label", "")]
        self._chunks = [p for p in title_parts if p]

    def handle_data(self, data: str) -> None:
        if self._href:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = clean_spaces(" ".join(self._chunks))
            self.links.append(Link(self._href, text))
            self._href = None
            self._chunks = []


def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value or "")
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return clean_spaces(value)


def decode_response_text(res: object) -> str:
    content = getattr(res, "content", b"") or b""
    text = getattr(res, "text", "") or ""
    sample = text[:300].lower()
    if content and ("charset=iso-8859-1" in sample or "charset=\"iso-8859-1" in sample):
        return content.decode("iso-8859-1", errors="replace")
    if content and ("\ufffd" in text[:1000] or "�" in text[:1000]):
        for enc in ("iso-8859-1", "cp1252", "utf-8"):
            try:
                decoded = content.decode(enc)
            except UnicodeDecodeError:
                continue
            if decoded.count("\ufffd") < text.count("\ufffd"):
                return decoded
    return text


def norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def debug(args: argparse.Namespace, msg: str) -> None:
    if args.debug:
        print(msg, flush=True)


def is_block_page(status: int, raw_html: str) -> bool:
    text = norm(raw_html[:5000])
    return status in {429, 503} or any(
        token in text
        for token in (
            "wordfence",
            "acesso a este site foi limitado",
            "maximum number of page requests",
            "page requests per minute",
        )
    )


def cache_paths(url: str, args: argparse.Namespace) -> tuple[Path, Path]:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_dir = Path(args.cache_dir)
    return cache_dir / f"{key}.json", cache_dir / f"{key}.html"


def read_cache(url: str, args: argparse.Namespace) -> tuple[int, str, str] | None:
    if args.refresh_cache:
        return None
    meta_path, html_path = cache_paths(url, args)
    if not meta_path.exists() or not html_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = html_path.read_text(encoding="utf-8", errors="replace")
        return int(meta.get("status", 0)), raw, str(meta.get("final_url") or url)
    except Exception:
        return None


def write_cache(url: str, status: int, raw_html: str, final_url: str, args: argparse.Namespace) -> None:
    if status >= 400 or is_block_page(status, raw_html):
        return
    meta_path, html_path = cache_paths(url, args)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"status": status, "final_url": final_url, "cached_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    html_path.write_text(raw_html or "", encoding="utf-8", errors="replace")


def enforce_host_delay(url: str, bank: str, args: argparse.Namespace) -> None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if not host:
        return
    min_delay = args.lasalle_host_delay if bank == "lasalle" or "fundacaolasalle.org.br" in host else args.host_delay
    if min_delay <= 0:
        return
    last_by_host = getattr(args, "_last_by_host", {})
    last = last_by_host.get(host, 0.0)
    wait = min_delay - (time.time() - last)
    if wait > 0:
        debug(args, f"THROTTLE host={host} wait={wait:.1f}s")
        time.sleep(wait)
    last_by_host[host] = time.time()
    setattr(args, "_last_by_host", last_by_host)


def fetch(url: str, args: argparse.Namespace, bank: str = "") -> tuple[int, str, str]:
    blocked_banks = getattr(args, "_blocked_banks", set())
    if bank and bank in blocked_banks:
        debug(args, f"FETCH_SKIP_BLOCKED bank={bank} {url}")
        return 0, "", url

    cached = read_cache(url, args)
    if cached:
        status, raw, final_url = cached
        debug(args, f"FETCH_CACHE {status} {url}")
        return status, raw, final_url

    fetch_counts = getattr(args, "_fetch_counts", {})
    if bank:
        limit = args.lasalle_max_fetches if bank == "lasalle" else args.max_fetches_per_bank
        current = fetch_counts.get(bank, 0)
        if limit and current >= limit:
            debug(args, f"FETCH_BUDGET_EXHAUSTED bank={bank} limit={limit} {url}")
            return 0, "", url
        fetch_counts[bank] = current + 1
        setattr(args, "_fetch_counts", fetch_counts)

    retry_statuses = {429, 500, 502, 503, 504}
    attempts = 1 if bank == "lasalle" else args.retries
    for attempt in range(1, attempts + 1):
        if args.delay:
            time.sleep(args.delay)
        enforce_host_delay(url, bank, args)
        started = time.time()
        try:
            res = requests.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,es;q=0.8"},
                timeout=args.timeout,
                allow_redirects=True,
            )
            res.encoding = res.apparent_encoding or res.encoding
            raw_text = res.text or ""
            if curl_requests is not None and (
                res.status_code in {202, 403} or (res.status_code == 200 and len(raw_text) < 200)
            ):
                try:
                    cres = curl_requests.get(
                        url,
                        impersonate="chrome124",
                        headers={"Accept-Language": "pt-BR,pt;q=0.9,es;q=0.8"},
                        timeout=args.timeout,
                        allow_redirects=True,
                    )
                    craw = decode_response_text(cres)
                    if cres.status_code < 400 and len(craw) > len(raw_text):
                        res = cres
                        raw_text = craw
                except Exception as exc:  # noqa: BLE001
                    debug(args, f"FETCH_CURL_FALLBACK_ERR {type(exc).__name__} {url} :: {exc}")
            debug(args, f"FETCH {res.status_code} {time.time() - started:.1f}s attempt={attempt} {url}")
            if is_block_page(res.status_code, raw_text):
                if bank:
                    blocked_banks.add(bank)
                    setattr(args, "_blocked_banks", blocked_banks)
                debug(args, f"FETCH_BLOCKED bank={bank or '-'} status={res.status_code} {url}")
                return res.status_code, raw_text, res.url
            if res.status_code in retry_statuses and attempt < attempts:
                time.sleep(1.5 * attempt)
                continue
            write_cache(url, res.status_code, raw_text, res.url, args)
            return res.status_code, raw_text, res.url
        except Exception as exc:  # noqa: BLE001
            debug(args, f"FETCH_ERR {type(exc).__name__} attempt={attempt} {url} :: {exc}")
            if attempt < attempts:
                time.sleep(1.5 * attempt)
                continue
            return 0, "", url
    return 0, "", url


def extract_links(raw_html: str, base_url: str) -> list[Link]:
    parser = AnchorParser(base_url)
    try:
        parser.feed(raw_html or "")
    except Exception:
        pass
    seen: set[str] = set()
    out: list[Link] = []
    for link in parser.links:
        url = unwrap_document_url(link.url.split("#", 1)[0])
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(Link(url, clean_spaces(link.text)))

    # Some sites expose direct PDFs in JS attributes rather than anchor hrefs.
    for match in re.finditer(r"https?://[^\"'<>\s]+", raw_html or "", flags=re.I):
        url = unwrap_document_url(html.unescape(match.group(0)).rstrip(").,;]'\""))
        if url not in seen:
            seen.add(url)
            out.append(Link(url, ""))
    return out


def unwrap_document_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.netloc.lower().endswith("pdfviewer.consulplan.net") and "/Edital/http" in parsed.path:
        return unquote(parsed.path.split("/Edital/", 1)[1])
    qs = parse_qs(parsed.query)
    for key in ("file", "pdf", "arquivo"):
        candidate = (qs.get(key) or [""])[0]
        if candidate and candidate.lower().startswith("http"):
            candidate = html.unescape(unquote(candidate))
            if is_document_url(candidate):
                return candidate
    return url


def is_document_url(url: str) -> bool:
    low = unquote((url or "").lower())
    path = low.split("?", 1)[0]
    return path.endswith((".pdf", ".doc", ".docx")) or (
        any(ext in low for ext in (".pdf", ".doc", ".docx")) and any(key in low for key in ("file=", "pdf=", "arquivo="))
    )


def page_title(raw_html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw_html or "")
    return strip_tags(match.group(1)) if match else ""


def extract_headings(raw_html: str) -> list[str]:
    headings: list[str] = []
    for match in re.finditer(r"(?is)<h[1-4][^>]*>(.*?)</h[1-4]>", raw_html or ""):
        text = strip_tags(match.group(1))
        ntext = norm(text)
        if not text or len(text) < 6 or len(text) > 180:
            continue
        if ntext in {
            "concursos",
            "informacoes",
            "publicacoes",
            "arquivos disponiveis",
            "vagas",
            "cargos",
            "cronograma de atividades",
            "arquivos relacionados",
        }:
            continue
        if any(skip in ntext for skip in ("compartilhe", "trabalhe conosco", "area do candidato")):
            continue
        headings.append(text)
    return headings


def is_generic_banca_heading(text: str) -> bool:
    ntext = norm(text)
    generic = {
        "fundacao la salle",
        "fundatec concursos",
        "concursos fundatec",
        "instituto legalle",
        "legalle concursos",
        "quadrix",
        "instituto fenix",
        "objetiva concursos",
        "fgv conhecimento",
        "faurgs",
        "fundacao cesgranrio",
        "cesgranrio",
        "fundacao carlos chagas",
        "fcc",
        "instituto aocp",
        "ibfc",
        "instituto brasileiro de formacao e capacitacao",
        "instituto consulplan",
    }
    return ntext in generic or ntext in {
        "concursos",
        "informacoes",
        "publicacoes",
        "cargos",
        "arquivos relacionados",
        "cronograma de atividades",
    }


def meaningful_heading(raw_html: str) -> str:
    headings = [h for h in extract_headings(raw_html) if not is_generic_banca_heading(h)]
    for text in headings:
        ntext = norm(text)
        if any(
            token in ntext
            for token in (
                "prefeitura",
                "municipio",
                "camara",
                "fundacao",
                "conselho",
                "hospital",
                "universidade",
                "tribunal",
                "defensoria",
                "ministerio publico",
                "secretaria",
                "banco",
                "companhia",
            )
        ):
            return text
    return headings[0] if headings else ""


def nearby_text(raw_html: str, needle: str, radius: int = 900) -> str:
    if not raw_html or not needle:
        return ""
    idx = raw_html.find(needle)
    if idx < 0:
        idx = raw_html.find(html.escape(needle))
    if idx < 0:
        return ""
    return strip_tags(raw_html[max(0, idx - radius) : idx + len(needle) + radius])


def detail_summary(raw_html: str) -> str:
    text = strip_tags(raw_html)
    compact = clean_spaces(text)
    for marker in ("INFORMAÇÕES", "INFORMAÇÕES DO CONCURSO", "Dados do Concurso", "Informações"):
        idx = compact.find(marker)
        if idx >= 0:
            return compact[idx : idx + 700]
    return compact[:700]


def lasalle_certame_text(raw_html: str) -> str:
    headings = [h for h in extract_headings(raw_html) if not is_generic_banca_heading(h)]
    if not headings:
        return meaningful_heading(raw_html)

    orgao_line = ""
    for text in headings:
        ntext = norm(text)
        looks_like_orgao = "/RS" in text or any(
            token in ntext
            for token in ("prefeitura", "municipio", "camara", "fundacao", "hospital", "conselho", "universidade")
        )
        looks_like_event = any(token in ntext for token in ("concurso publico", "processo seletivo", "edital"))
        if looks_like_orgao and not looks_like_event:
            orgao_line = text
            break
    if not orgao_line:
        orgao_line = next(
            (
                text
                for text in headings
                if any(token in norm(text) for token in ("prefeitura", "municipio", "camara", "fundacao", "hospital"))
            ),
            "",
        )

    edital_line = next(
        (
            text
            for text in headings
            if any(token in norm(text) for token in ("edital de abertura", "concurso publico", "processo seletivo"))
        ),
        "",
    )
    if orgao_line and edital_line and orgao_line != edital_line:
        return clean_spaces(f"{orgao_line} {edital_line}")
    return edital_line or orgao_line or headings[0]


def fundatec_certame_text(raw_html: str) -> str:
    for match in re.finditer(r"(?is)<td[^>]*>(.*?)</td>", raw_html or ""):
        text = strip_tags(match.group(1))
        ntext = norm(text)
        if len(text) < 12 or len(text) > 260:
            continue
        if ("/RS" in text or re.search(r"\bRS\b", text)) and any(
            token in ntext for token in ("concurso publico", "processo seletivo", "edital")
        ):
            return text

    compact = strip_tags(raw_html)
    compact = re.sub(r"^\s*(?:\.: Fundatec Concursos :\.|Concursos\s*-\s*Fundatec)\s*", "", compact, flags=re.I)
    compact = re.sub(r"\s+", " ", compact).strip()
    dated = re.search(r"(.{12,260}?)(?=\s+\d{2}/\d{2}/\d{4}\s+)", compact)
    if dated:
        return clean_spaces(dated.group(1))
    match = re.search(
        r"((?:Prefeitura|Munic[ií]pio|C[aâ]mara|Funda[cç][aã]o)[^|]{0,180}?(?:Concurso|Processo Seletivo)[^|]{0,80})",
        compact,
        flags=re.I,
    )
    return clean_spaces(match.group(1)) if match else meaningful_heading(raw_html)


def objetiva_certame_text(candidate: DetailCandidate, raw_html: str) -> str:
    if re.search(r"/(?:PB|SP|RJ|MG|ES|SC|PR|GO|CE|PE|AM|RO)\b", candidate.title) and "/RS" not in candidate.title:
        return clean_spaces(candidate.title)
    for text in (candidate.title, candidate.context):
        cleaned = clean_spaces(text)
        ntext = norm(cleaned)
        if not cleaned or len(cleaned) < 12:
            continue
        if ntext in {"objetiva instituto", "busca"}:
            continue
        if "/RS" in cleaned or re.search(r"\bRS\b", cleaned):
            return cleaned
    heading = meaningful_heading(raw_html)
    if norm(heading) not in {"busca", "objetiva instituto"}:
        return heading
    return detail_summary(raw_html)


def enrich_links_with_context(raw_html: str, links: list[Link]) -> list[Link]:
    out: list[Link] = []
    for link in links:
        text_norm = norm(link.text)
        if link.url.lower().split("?", 1)[0].endswith(".pdf") and (not text_norm or text_norm in {"download", "baixar"}):
            context = nearby_text(raw_html, link.url, radius=450)
            if not context:
                context = nearby_text(raw_html, Path(urlparse(link.url).path).name, radius=450)
            if context:
                out.append(Link(link.url, context))
                continue
        out.append(link)
    return out


def infer_tipo(text: str) -> str:
    n = norm(text)
    if any(tok in n for tok in ("processo seletivo", "pss", "seletivo simplificado", "selecao publica")):
        return "processo_seletivo"
    return "concurso_publico"


def normalize_num(raw: str) -> str:
    if not raw:
        return ""
    match = re.search(r"(\d{1,4})\s*[/.-]\s*((?:20)?\d{2})(?!\d)", raw)
    if not match:
        return ""
    number = match.group(1).lstrip("0") or "0"
    year = match.group(2)
    if len(year) == 2:
        year = "20" + year
    if len(number) <= 2:
        number = number.zfill(2)
    return f"nº {number}/{year}"


def extract_num(text: str) -> str:
    text = html.unescape(text or "")
    patterns = [
        r"(?:edital|ato|certame|concurso|processo seletivo|pss)[^\d]{0,30}(\d{1,4}\s*[/.-]\s*(?:20)?\d{2})(?!\d)",
        r"(?:n[ºo\.]|numero|número)\s*(\d{1,4}\s*[/.-]\s*(?:20)?\d{2})(?!\d)",
    ]
    for pat in patterns:
        match = re.search(pat, text, flags=re.I)
        if match:
            return normalize_num(match.group(1))
    loose = re.search(
        r"(?:n[Âººo\.]|numero|nÃºmero|número)\s*(\d{1,4})(?!\s*[/.-])\D{0,80}(20\d{2})",
        text,
        flags=re.I,
    )
    if loose:
        return normalize_num(f"{loose.group(1)}/{loose.group(2)}")
    return ""


def normalize_num(raw: str) -> str:  # type: ignore[no-redef]
    if not raw:
        return ""
    match = re.search(r"(\d{1,4})\s*[/.-]\s*((?:20)?\d{2})(?!\d)(?!\s*[/.-]\s*\d)", raw)
    if not match:
        return ""
    number = match.group(1).lstrip("0") or "0"
    year = match.group(2)
    if len(year) == 2:
        year = "20" + year
    if len(number) <= 2:
        number = number.zfill(2)
    return f"n\u00ba {number}/{year}"


def extract_num(text: str) -> str:  # type: ignore[no-redef]
    text = html.unescape(text or "")
    prefix = r"(?:n(?:\u00ba|º|ş|o|\.)|numero|número|nÃºmero|nĂşmero)"
    patterns = [
        r"(?:edital|ato|certame|concurso|processo seletivo|pss)[^\d]{0,30}(\d{1,4}\s*[/.-]\s*(?:20)?\d{2})(?!\d)(?!\s*[/.-]\s*\d)",
        prefix + r"\s*(\d{1,4}\s*[/.-]\s*(?:20)?\d{2})(?!\d)(?!\s*[/.-]\s*\d)",
    ]
    for pat in patterns:
        match = re.search(pat, text, flags=re.I)
        if match:
            return normalize_num(match.group(1))
    loose = re.search(prefix + r"\s*(\d{1,4})(?!\s*[/.-]).{0,120}?(20\d{2})", text, flags=re.I | re.S)
    if loose:
        return normalize_num(f"{loose.group(1)}/{loose.group(2)}")
    return ""


def extract_pdf_text_prefix(url: str, args: argparse.Namespace, max_pages: int = 2) -> str:
    if not url or PdfReader is None:
        return ""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_dir = PROJECT_ROOT / "data" / "cache" / "pdf_text"
    cache_path = cache_dir / f"{key}.txt"
    if cache_path.exists() and not args.refresh_cache:
        return cache_path.read_text(encoding="utf-8", errors="replace")

    try:
        res = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}, timeout=args.timeout)
        if res.status_code >= 400 or not res.content:
            return ""
        reader = PdfReader(io.BytesIO(res.content))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        text = clean_spaces("\n".join(chunks))
        if text:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8", errors="replace")
        return text
    except Exception as exc:  # noqa: BLE001
        debug(args, f"PDF_TEXT_ERR {type(exc).__name__} {url} :: {exc}")
        return ""


def extract_num_from_pdf(url: str, args: argparse.Namespace) -> str:
    return extract_num(extract_pdf_text_prefix(url, args)[:8000])


def orgao_from_pdf_text(text: str) -> str:
    ntext = normalize_text(text or "")
    if (
        "tribunal de justica do estado do rio grande do sul" in ntext
        and (
            "delegacoes de notas" in ntext
            or "notarial e registral" in ntext
            or "corregedora geral da justica" in ntext
            or "corregedoria geral da justica" in ntext
        )
    ):
        return "Tribunal de Justiça do Estado do Rio Grande do Sul"
    if "universidade federal do rio grande do sul" in ntext or re.search(r"\bufrgs\b", ntext):
        return "Universidade Federal do Rio Grande do Sul"
    if "hospital de clinicas de porto alegre" in ntext:
        return "Hospital de Clínicas de Porto Alegre"
    if "banco do estado do rio grande do sul" in ntext or "banrisul" in ntext:
        return "Banco do Estado do Rio Grande do Sul"
    patterns = [
        r"\b(Prefeitura Municipal de [A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú\s.'-]{2,80})\b",
        r"\b(Munic[ií]pio de [A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú\s.'-]{2,80})\b",
        r"\b(C[aâ]mara Municipal de [A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú\s.'-]{2,80})\b",
        r"\b(Funda[cç][aã]o [A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú\s.'-]{2,100})\b",
        r"\b(Conselho Regional [A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú\s.'-]{2,100})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            return clean_spaces(match.group(1))
    return ""


def original_municipio(registry: RSScopeRegistry, normalized_city: str) -> str:
    with (PROJECT_ROOT / "data" / "sites_municipios_rs.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        first = handle.readline()
        if not first.startswith("sep="):
            handle.seek(0)
        for row in csv.DictReader(handle, delimiter=";"):
            if normalize_text(row.get("municipio", "")) == normalized_city:
                return row.get("municipio", "")
    return normalized_city.title()


def find_municipio(registry: RSScopeRegistry, text: str) -> str:
    ntext = normalize_text(text or "")
    explicit_rs = re.findall(
        r"(?:^|[\s(/-])([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú.'-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇà-ú.'-]+){0,4})\s*/\s*RS\b",
        text or "",
    )
    for raw_city in reversed(explicit_rs):
        city = normalize_text(raw_city)
        if city in registry.municipalities:
            return original_municipio(registry, city)
    best = ""
    best_len = 0
    for city in registry.municipalities:
        if city == "rio grande" and "rio grande do sul" in ntext and not re.search(
            r"\b(?:municipio de rio grande|prefeitura(?: municipal)? de rio grande|cidade de rio grande|rio grande rs)\b",
            ntext,
        ):
            continue
        if city == "ipe" and re.search(r"\bipe(?:\s+prev)?\b", ntext) and not re.search(
            r"\b(?:municipio de ipe|prefeitura(?: municipal)? de ipe|cidade de ipe|ipe rs)\b",
            ntext,
        ):
            continue
        if city == "getulio vargas" and "fundacao hospitalar getulio vargas" in ntext:
            continue
        if city and len(city) > best_len and re.search(rf"\b{re.escape(city)}\b", ntext):
            best = city
            best_len = len(city)
    if not best:
        return ""
    return original_municipio(registry, best)


def has_statewide_rs_signal(text: str) -> bool:
    ntext = normalize_text(text or "")
    return any(
        token in ntext
        for token in (
            "rio grande do sul",
            "estado do rs",
            "crq rs",
            "crp rs",
            "cra rs",
            "crea rs",
            "cref rs",
            "crefito 5",
            "crefito da 5",
            "crefito 5 regiao",
            "crefito 5ª regiao",
            "brigada militar",
            "tribunal de justica do estado do rio grande do sul",
            "defensoria publica do estado do rio grande do sul",
            "ministerio publico do estado do rio grande do sul",
            "ministerio publico do rio grande do sul",
            "secretaria da educacao do estado do rio grande do sul",
            "estado do rio grande do sul",
            "hospital de clinicas de porto alegre",
            "universidade federal do rio grande do sul",
            "ufrgs",
            "ufsm",
            "trf 4",
            "tribunal regional federal da 4",
            "banrisul",
            "badesul",
        )
    )


def is_national_scope(text: str) -> bool:
    ntext = normalize_text(text or "")
    national_tokens = (
        "banco do brasil",
        "caixa economica federal",
        "petrobras",
        "empresa brasileira de correios",
        "correios",
        "concurso publico nacional unificado",
        "ministerio publico da uniao",
        "tribunal superior eleitoral",
        "policia federal",
    )
    return any(token in ntext for token in national_tokens)


def matches_year(year: str, *, numero: str, detail_url: str, text: str) -> bool:
    if not year:
        return True
    if numero:
        return numero.endswith(f"/{year}")
    short = year[-2:]
    blob = " ".join([numero or "", detail_url or "", text or ""])
    if year in blob:
        return True
    if numero.endswith(f"/{year}"):
        return True
    path = urlparse(detail_url or "").path.lower()
    # Some authority pages encode the year as a suffix, e.g. tjrsnotarial26.
    if re.search(rf"[a-z][a-z0-9_.-]*{re.escape(short)}\b", path):
        return True
    return False


def candidate_year_hint(candidate: DetailCandidate, year: str) -> bool:
    if not year:
        return True
    short = year[-2:]
    blob = " ".join([candidate.title, candidate.context, candidate.detail_url])
    if year in blob:
        return True
    path = urlparse(candidate.detail_url or "").path.lower()
    if re.search(rf"[a-z][a-z0-9_.-]*{re.escape(short)}\b", path):
        return True
    return False


def row_quality_ok(orgao: str, municipio: str, title_for_org: str, candidate: DetailCandidate) -> bool:
    norg = norm(orgao)
    scope_text = " ".join([orgao, municipio, title_for_org, candidate.title, candidate.detail_url])
    nscope = normalize_text(scope_text)
    if is_national_scope(scope_text):
        return False
    non_rs_states = (
        "estado do parana",
        "estado de santa catarina",
        "estado de sao paulo",
        "estado do rio de janeiro",
        "estado de minas gerais",
        "estado da bahia",
        "estado de pernambuco",
        "estado do ceara",
        "estado do para",
        "estado do maranhao",
        "estado do piaui",
        "estado de goias",
        "estado do mato grosso",
        "estado de mato grosso",
        "estado do espirito santo",
        "estado da paraiba",
        "estado de roraima",
        "estado do acre",
        "estado de sergipe",
        "estado de alagoas",
        "estado do amapá",
        "estado do amapa",
        "estado do amazonas",
        "estado de rondonia",
        "estado de rondônia",
        "estado do ceara",
        "estado do rio grande do norte",
        "estado de tocantins",
    )
    if any(state in nscope for state in non_rs_states) and "rio grande do sul" not in nscope:
        return False
    non_rs_uf = r"(?:AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RO|RR|SC|SE|SP|TO)"
    if re.search(rf"(?i)(?:^|[\s/(._-]){non_rs_uf}(?:$|[\s/)._,-])", scope_text):
        return False
    if re.search(r"/(?:PB|SP|RJ|MG|ES|SC|PR|GO|CE|PE|AM|RO)\b", candidate.title) and "/RS" not in candidate.title:
        return False
    if norg in {"busca", "objetiva instituto", "instituto legalle"}:
        return False
    if re.search(r"\b[A-Z]{2}\s*/\s*(?:PB|SP|RJ|MG|ES|SC|PR|GO|CE|PE|AM|RO)\b", scope_text):
        return False
    if re.search(r"/(?:PB|SP|RJ|MG|ES|SC|PR|GO|CE|PE|AM|RO)\b", scope_text) and "/RS" not in scope_text:
        return False
    if not municipio and not has_statewide_rs_signal(scope_text):
        return False
    if "rio grande do sul" not in nscope and not municipio and "rs" not in nscope:
        return False
    return True


def smart_orgao_case(text: str) -> str:
    if not text:
        return ""
    letters = [ch for ch in text if ch.isalpha()]
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
    if upper_ratio < 0.72:
        return text
    out = text.title()
    for small in ("De", "Da", "Das", "Do", "Dos", "E"):
        out = re.sub(rf"\b{small}\b", small.lower(), out)
    replacements = {
        "Rs": "RS",
        "Crq": "CRQ",
        "Crea": "CREA",
        "Cref": "CREF",
        "Ufsm": "UFSM",
        "Ufrgs": "UFRGS",
        "Trt": "TRT",
        "Dpe": "DPE",
    }
    for src, dst in replacements.items():
        out = re.sub(rf"\b{src}\b", dst, out)
    return out


def clean_orgao(title: str, municipio: str) -> str:
    text = clean_spaces(title)
    ntext = normalize_text(text)
    if "universidade federal do rio grande do sul" in ntext:
        return "Universidade Federal do Rio Grande do Sul"
    if "hospital de clinicas de porto alegre" in ntext:
        return "Hospital de Clínicas de Porto Alegre"
    if "procuradoria geral do estado" in ntext or "pge 15" in ntext:
        return "Procuradoria Geral do Estado do Rio Grande do Sul"
    if "procergs" in ntext or "centro de tecnologia da informacao e comunicacao do estado do rio grande do sul" in ntext:
        return "PROCERGS - Centro de Tecnologia da Informação e Comunicação do Estado do Rio Grande do Sul S.A."
    if "policia civil" in ntext and "rio grande do sul" in ntext:
        return "Polícia Civil do Estado do Rio Grande do Sul"
    if "emergencial para o cadastro de reserva" in ntext and "formadores" in ntext and "rio grande do sul" in ntext:
        return "Universidade Federal do Rio Grande do Sul"
    text = re.sub(r"(?i)^(?:fcc|fgv conhecimento|fundacao cesgranrio|cesgranrio)\s*[-|:]\s*", "", text)
    text = re.sub(
        r"(?i)^concurso\s+p(?:ú|u|Ãº|ÃƒÂº)blico\s+de\s+provas(?:\s+e\s+t[íi]tulos)?\s+para\s+(?:(?:a|o|as|os)\s+)?",
        "",
        text,
    )
    text = re.sub(
        r"(?i)^(?:concurso\s+p(?:ú|u|Ãº|ÃƒÂº)blico|processo\s+seletivo(?:\s+(?:p(?:ú|u)blico|simplificado))?)\s+para\s+(?:(?:a|o|as|os)\s+)?",
        "",
        text,
    )
    text = re.sub(r"(?i)\s*(?:\|\s*)?(?:fgv conhecimento|cesgranrio|fundacao carlos chagas|fcc|faurgs)$", "", text)
    if municipio:
        title_norm = normalize_text(text)
        municipio_norm = normalize_text(municipio)
        if "camara municipal" in title_norm:
            return f"Câmara Municipal de {municipio}"
        if (
            f"prefeitura municipal de {municipio_norm}" in title_norm
            or f"prefeitura de {municipio_norm}" in title_norm
        ):
            return f"Prefeitura Municipal de {municipio}"
        if (
            f"municipio de {municipio_norm}" in title_norm
            or re.search(rf"\b{re.escape(municipio_norm)}\s+rs\s+municipio\b", title_norm)
            or re.search(rf"\b{re.escape(municipio_norm)}\s+municipio\b", title_norm)
        ):
            return f"Município de {municipio}"
    prefix = re.split(
        r"(?i)\b(?:concurso\s+p(?:ú|u|Ãº|ÃƒÂº)blico|processo\s+seletivo(?:\s+(?:p(?:ú|u)blico|simplificado))?|edital\s+de\s+abertura)\b",
        text,
        maxsplit=1,
    )[0]
    if prefix and any(
        token in norm(prefix)
        for token in (
            "prefeitura",
            "municipio",
            "camara",
            "fundacao",
            "hospital",
            "conselho",
            "universidade",
            "tribunal",
            "defensoria",
            "ministerio publico",
            "secretaria",
            "banco",
            "companhia",
        )
    ):
        text = prefix
    text = re.sub(r"(?i)\s*/\s*RS\b", " ", text)
    text = re.sub(r"(?i)\binformacoes\b|\binformações\b", " ", text)
    info_match = re.search(
        r"(?is)(?:(?:concurso\s+)?publico|(?:concurso\s+)?público|processo seletivo(?: simplificado)?)\s+"
        r"(?:n[ºo.]?\s*)?\d{1,4}\s*[/.-]\s*\d{2,4}\s+(.+?)\s+"
        r"(?:inscricoes|inscrições|periodo|local|cidade|$)",
        text,
    )
    if info_match:
        text = clean_spaces(info_match.group(1))
    text = re.sub(r"(?i)\b(concurso publico|concurso|processo seletivo simplificado|processo seletivo|pss)\b", " ", text)
    text = re.sub(r"(?i)\b(edital de abertura|edital|inscri[cç][oõ]es?|abertas?|em andamento|encerrad[oa]s?)\b", " ", text)
    text = re.sub(r"\bn[ºo\.]?\s*\d{1,4}\s*[/.-]\s*(?:20)?\d{2}\b", " ", text, flags=re.I)
    text = re.sub(r"\s+\d{1,4}\s*[/.-]\s*(?:20)?\d{2}\b.*$", " ", text)
    text = re.sub(r"(?i)\b(?:publico|pÃºblico|público|concurso publico|processo seletivo(?: simplificado)?)\s*[-:]\s*.*$", " ", text)
    text = re.sub(
        r"(?i)\b(?:inscri[cç][oõ]es?|upload|documentos\s+de\s+an[áa]lise|an[áa]lise\s+curricular|at[eé]\s+\d{1,2}h)\b.*$",
        " ",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:acompanhar\s+inscri[cç][aã]o|trabalhe\s+conosco|arquivos\s+dispon[ií]veis|vagas|extrato\s+e\s+legisla[cç][aã]o)\b.*$",
        " ",
        text,
    )
    if municipio:
        text = re.sub(rf"(?i)(?:^|\s+){re.escape(municipio)}\s*[-/]?\s*RS\b.*$", " ", text)
    text = re.sub(r"\s*[-|:]\s*", " - ", text)
    text = re.sub(r"(?i)(?:^|\s)[e&]\s*$", " ", text)
    text = re.sub(r"(?i)\s+(?:concursos?\s+p[úu]blicos?(?:\s*\d{4})?|p[úu]blico(?:\s*\d{4})?)\s*$", " ", text)
    text = clean_spaces(text).strip(" -|:")
    if municipio and normalize_text(text).strip(" -") in {"", normalize_text(municipio), f"{normalize_text(municipio)} e"}:
        text = f"Prefeitura Municipal de {municipio}"
    if not text and municipio:
        text = f"Prefeitura de {municipio}-RS"
    if municipio and "prefeitura" in norm(title) and municipio.lower() not in text.lower():
        text = f"Prefeitura de {municipio}-RS"
    return smart_orgao_case(text)


def doc_text_for_scoring(link: Link) -> tuple[str, str]:
    text = norm(" ".join([link.text, unquote(link.url)]))
    bad_text = re.sub(r"\bcom\s*anexos?\b", " ", text)
    bad_text = bad_text.replace("comanexos", " ")
    return text, bad_text


def has_opening_doc_signal(text: str) -> bool:
    return bool(
        "edital de abertura" in text
        or "editaldeabertura" in text
        or "edital abertura" in text
        or "abertura e inscricoes" in text
        or re.search(r"\bedital\s*n[Âºo.]?\s*0*1\s*[/.-]\s*20\d{2}\b", text)
    )


def has_accessory_doc_signal(text: str) -> bool:
    accessory_tokens = (
        "convoca",
        "convocacao",
        "admissao",
        "nomea",
        "homolog",
        "resultado",
        "cronograma",
        "data hora",
        "data, hora",
        "local de realizacao",
        "prova pratica",
        "notas",
        "gabarito",
        "classific",
        "isencao",
        "atendimentos especiais",
        "pessoa com deficiencia",
        "candidatos negros",
        "prorroga",
        "prorrogacao",
        "comissao",
        "heteroidentificacao",
        "reabertura",
        "inscricao definitiva",
        "candidatos aptos",
        "candidato apto",
        "fase definitiva",
        "prova dissertativa",
        "sustentacao oral",
        "espelho de correcao",
        "regulamento",
        "nominata",
        "banca elaboradora",
        "bancas elaboradoras",
    )
    return any(token in text for token in accessory_tokens)


def doc_score(link: Link) -> int:
    text = norm(" ".join([link.text, unquote(link.url)]))
    bad_text = re.sub(r"\bcom\s*anexos?\b", " ", text)
    bad_text = bad_text.replace("comanexos", " ")
    opening_signal = bool(
        "edital de abertura" in text
        or "editaldeabertura" in text
        or "edital abertura" in text
        or "abertura e inscricoes" in text
        or re.search(r"\bedital\s*n[ºo.]?\s*0*1\s*[/.-]\s*20\d{2}\b", text)
    )
    score = 0
    if any(token in text for token in ("termos de uso", "politica de privacidade", "lgpd", "cookies")):
        score -= 50
    if is_document_url(link.url):
        score += 3
    if "edital de abertura" in text or "abertura e inscricoes" in text:
        score += 12
    if "edital de abertura" in text or "edital-de-abertura" in text or "edital_abertura" in text:
        score += 8
    if re.search(r"\bedital\s*\(\s*retificad", text):
        score += 22
    if re.search(r"\bedital[-_\s]*0*1[-_\s]*20\d{2}\b", text):
        score += 18
    if "edital" in text:
        score += 6
    if "concurso publico" in text or "processo seletivo" in text:
        score += 4
    if "consolidado" in text:
        score += 2
    if "retifica" in text or "adendo" in text:
        score -= 12
    accessory_tokens = (
        "convoca",
        "convocacao",
        "admissao",
        "nomea",
        "homolog",
        "resultado",
        "cronograma",
        "data hora",
        "data, hora",
        "local de realizacao",
        "prova pratica",
        "notas",
        "gabarito",
        "classific",
        "isencao",
        "atendimentos especiais",
        "pessoa com deficiencia",
        "candidatos negros",
        "prorroga",
        "prorrogacao",
        "comissao",
        "heteroidentificacao",
        "reabertura",
        "inscricao definitiva",
        "candidatos aptos",
        "candidato apto",
        "fase definitiva",
        "prova dissertativa",
        "sustentacao oral",
        "espelho de correcao",
        "regulamento",
    )
    if any(token in bad_text for token in accessory_tokens):
        score -= 45
    if "retificacao do edital de abertura" in bad_text or "retificativo" in bad_text:
        score -= 30
    if re.search(r"\banexos?\b", bad_text) and not opening_signal:
        score -= 35
    bad_tokens = [
        "retifica",
        "adendo",
        "resultado",
        "gabarito",
        "homolog",
        "cronograma",
        "convoca",
        "nomea",
        "anexo",
        "aviso",
        "extrato",
        "isencao",
        "definitivo",
        "pessoa com deficiencia",
        "candidatos negros",
        "atendimentos especiais",
        "inscricoes homologadas",
        "lista",
        "classific",
        "impugna",
        "comunicado",
        "resultado",
        "homolog",
        "cronograma",
        "anexo",
        "suspensao",
        "suspensão",
        "indeferid",
        "preliminar",
        "recurso",
        "definitivo",
        "pessoa com deficiencia",
        "candidatos negros",
        "atendimentos especiais",
        "prova tipo",
        "tipo 1",
        "tipo 2",
        "tipo 3",
        "tipo 4",
        "caderno de prova",
        "sorteio",
        "demanda",
        "vaga",
        "prorroga",
        "prorrogacao",
        "comissao",
        "heteroidentificacao",
        "reabertura",
        "inscricao definitiva",
        "candidatos aptos",
        "prova dissertativa",
        "sustentacao oral",
        "espelho de correcao",
        "regulamento",
        "nominata",
        "banca elaboradora",
        "bancas elaboradoras",
    ]
    always_bad = {
        "impugna",
        "comunicado",
        "suspensao",
        "suspensão",
        "indeferid",
        "preliminar",
        "recurso",
        "sorteio",
        "demanda",
    }
    for tok in bad_tokens:
        haystack = bad_text if tok in {"anexo"} else text
        if tok in haystack and ("abertura" not in text or tok in always_bad):
            score -= 25 if tok in always_bad else 5
    return score


def best_opening_doc(links: list[Link]) -> Link | None:
    docs = [
        link
        for link in links
        if is_document_url(link.url)
        or "edital" in norm(link.text + " " + link.url)
    ]
    filtered_docs = []
    for link in docs:
        text, bad_text = doc_text_for_scoring(link)
        opening_signal = has_opening_doc_signal(text)
        if has_accessory_doc_signal(bad_text) and not opening_signal:
            continue
        if not opening_signal and doc_score(link) < 9:
            continue
        filtered_docs.append(link)
    docs = filtered_docs
    if not docs:
        return None
    docs.sort(key=lambda item: (doc_score(item), -len(item.text)), reverse=True)
    if doc_score(docs[0]) <= 0:
        return None
    return docs[0]


def legalle_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        if re.search(r"/edital/ver/\d+", link.url):
            out.append(link)
    return out


def fundatec_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    seen: set[str] = set()
    for link in links:
        parsed = urlparse(link.url)
        qs = parse_qs(parsed.query)
        cid = (qs.get("concurso") or [""])[0]
        if not cid:
            match = re.search(r"concurso=(\d+)", link.url)
            cid = match.group(1) if match else ""
        if cid:
            url = f"https://www.fundatec.org.br/portal/concursos/pagina_editais.php?concurso={cid}"
            if url not in seen:
                seen.add(url)
                out.append(Link(url, link.text))
    return out


def lasalle_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().removeprefix("www.") != "fundacaolasalle.org.br":
            continue
        if re.search(r"^/concurso/[^/?#]+/?$", parsed.path, flags=re.I):
            out.append(link)
    return out


def selecao_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        if re.search(r"\.selecao\.net\.br/informacoes/\d+/?", link.url, flags=re.I):
            out.append(link)
    return out


def quadrix_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        if re.search(r"quadrix\.org\.br/informacoes/\d+/?", link.url, flags=re.I):
            out.append(link)
    return out


def objetiva_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        if re.search(r"concursos\.objetivas\.com\.br/informacoes/\d+/?", link.url, flags=re.I):
            out.append(link)
    return out


def cebraspe_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        if re.search(r"cebraspe\.org\.br/concursos/[A-Z0-9_]+/?", link.url, flags=re.I):
            out.append(link)
    return out


def consulplan_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().endswith("institutoconsulplan.org.br") and re.search(
            r"^/Concurso/[^/?#]+/?$", parsed.path, flags=re.I
        ):
            out.append(link)
    return out


def fgv_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        path = parsed.path.strip("/")
        if parsed.netloc.lower().endswith("conhecimento.fgv.br") and re.fullmatch(r"concursos/[^/]+", path):
            if path not in {"concursos/nosso-portfolio"}:
                out.append(link)
    return out


def cesgranrio_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().endswith("cesgranrio.org.br") and re.search(r"^/concurso/[^/]+/?$", parsed.path):
            out.append(link)
    return out


def faurgs_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().endswith("portalfaurgs.com.br") and re.search(
            r"^/concursosFaurgs/(?:emandamento|encerrados)/[^/?#]+/?$", parsed.path, flags=re.I
        ):
            out.append(link)
    return out


def fcc_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().endswith("concursosfcc.com.br") and re.search(
            r"^/concursos/[^/]+/index\.html$", parsed.path, flags=re.I
        ):
            out.append(link)
    return out


def aocp_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().endswith("institutoaocp.org.br") and re.search(
            r"^/concursos/\d+/?$", parsed.path, flags=re.I
        ):
            out.append(link)
    return out


def ibfc_detail_urls(links: Iterable[Link]) -> list[Link]:
    out = []
    for link in links:
        parsed = urlparse(link.url)
        if parsed.netloc.lower().endswith("ibfc.org.br") and re.search(r"^/informacoes/\d+/?$", parsed.path):
            out.append(link)
    return out


def bank_indexes(args: argparse.Namespace) -> dict[str, list[str]]:
    pages = range(1, args.pages + 1)
    lasalle_pages = range(1, min(args.pages, args.lasalle_pages) + 1)
    lasalle_filters = [item.strip() for item in args.lasalle_filters.split(",") if item.strip()]
    return {
        "legalle": [
            "https://portal.institutolegalle.org.br/edital",
            "https://portal.institutolegalle.org.br/edital/index/abertos",
            "https://portal.institutolegalle.org.br/edital/index/andamento",
            "https://portal.institutolegalle.org.br/edital/index/encerrados",
            "https://portal.institutolegalle.org.br/edital/index/suspensos",
            "https://portal.editais.legalleconcursos.com.br/edital/index/abertos",
            "https://portal.editais.legalleconcursos.com.br/edital/index/andamento",
            "https://portal.editais.legalleconcursos.com.br/edital/index/encerrados",
            "https://portal.editais.legalleconcursos.com.br/edital/index/suspensos",
        ],
        "instituto_legalle": [
            "https://portal.institutolegalle.org.br/edital",
            "https://portal.institutolegalle.org.br/edital/index/abertos",
            "https://portal.institutolegalle.org.br/edital/index/andamento",
            "https://portal.institutolegalle.org.br/edital/index/encerrados",
            "https://portal.institutolegalle.org.br/edital/index/suspensos",
        ],
        "legalle_concursos": [
            "https://portal.editais.legalleconcursos.com.br/edital/index/abertos",
            "https://portal.editais.legalleconcursos.com.br/edital/index/andamento",
            "https://portal.editais.legalleconcursos.com.br/edital/index/encerrados",
            "https://portal.editais.legalleconcursos.com.br/edital/index/suspensos",
        ],
        "lasalle": [
            f"https://fundacaolasalle.org.br/filtro-concursos/page/{p}/?filtro={filtro}"
            for filtro in lasalle_filters
            for p in lasalle_pages
        ],
        "fundatec": [
            "https://www.fundatec.org.br/portal/concursos/",
            "https://www.fundatec.org.br/portal/concursos/concursos_abertos.php",
            "https://www.fundatec.org.br/portal/concursos/concursos_andamento.php",
            "https://www.fundatec.org.br/portal/concursos/concursos_encerrados.php",
        ],
        "quadrix": [
            "https://quadrix.org.br/",
            "https://quadrix.org.br/todos-os-concursos/",
            "https://quadrix.org.br/concursos/",
        ],
        "objetiva": [
            "https://concursos.objetivas.com.br/",
            "https://www.objetivas.com.br/",
        ],
        "fenix_selecao": [
            "https://institutofenix.selecao.net.br/",
            "https://talentconcursos.selecao.net.br/",
        ],
        "cebraspe": [
            "https://www.cebraspe.org.br/concursos/",
        ],
        "consulplan": [
            "https://www.institutoconsulplan.org.br/concursos/",
            "https://www.institutoconsulplan.org.br/",
        ],
        "fgv": [f"https://conhecimento.fgv.br/concursos?page={p}" for p in range(0, max(args.pages, 1))],
        "cesgranrio": [
            "https://www.cesgranrio.org.br/concursos/",
            *[f"https://www.cesgranrio.org.br/concursos/page/{p}/" for p in range(2, args.pages + 1)],
        ],
        "faurgs": [
            "https://portalfaurgs.com.br/concursosfaurgs",
            "https://portalfaurgs.com.br/concursosfaurgs/emandamento",
            "https://portalfaurgs.com.br/concursosfaurgs/encerrados",
        ],
        "fcc": [
            "https://www.concursosfcc.com.br/concursoInscricaoAberta.html",
            "https://www.concursosfcc.com.br/concursoAndamento.html",
            "https://www.concursosfcc.com.br/concursoOutraSituacao.html",
        ],
        "aocp": [
            "https://www.institutoaocp.org.br/concursos/status/em-andamento",
            "https://www.institutoaocp.org.br/concursos/status/encerrado",
            "https://www.institutoaocp.org.br/concursos",
        ],
        "ibfc": [
            "https://concursos.ibfc.org.br/",
            "https://concursos.ibfc.org.br/index/abertos/",
            "https://concursos.ibfc.org.br/index/1/",
            "https://concursos.ibfc.org.br/index/3/",
        ],
    }


DETAIL_EXTRACTORS = {
    "legalle": legalle_detail_urls,
    "instituto_legalle": legalle_detail_urls,
    "legalle_concursos": legalle_detail_urls,
    "lasalle": lasalle_detail_urls,
    "fundatec": fundatec_detail_urls,
    "quadrix": quadrix_detail_urls,
    "objetiva": objetiva_detail_urls,
    "fenix_selecao": selecao_detail_urls,
    "cebraspe": cebraspe_detail_urls,
    "consulplan": consulplan_detail_urls,
    "fgv": fgv_detail_urls,
    "cesgranrio": cesgranrio_detail_urls,
    "faurgs": faurgs_detail_urls,
    "fcc": fcc_detail_urls,
    "aocp": aocp_detail_urls,
    "ibfc": ibfc_detail_urls,
}


def is_legalle_bank(bank: str) -> bool:
    return bank in {"legalle", "instituto_legalle", "legalle_concursos"}


def output_banca(bank: str, detail_url: str) -> str:
    if bank == "legalle":
        host = urlparse(detail_url).netloc.lower()
        if "institutolegalle" in host:
            return "instituto_legalle"
        if "legalleconcursos" in host:
            return "legalle_concursos"
    return bank


def faurgs_certame_text(raw_html: str) -> str:
    heading = meaningful_heading(raw_html)
    if heading:
        return heading
    summary = detail_summary(raw_html)
    match = re.search(
        r"(?is)(Edital\s+n[ºo.]?\s*\d{1,4}\s*/\s*(?:20)?\d{2}\s*[-–]\s*.+?)\s+"
        r"(?:Acesso\s+à?\s+area|Acesso\s+a\s+area|CRONOGRAMA|Cargos|Arquivos\s+Relacionados|Voltar)",
        summary,
    )
    if match:
        return clean_spaces(match.group(1))
    return summary[:220]


def certame_text_for(candidate: DetailCandidate, raw_html: str, detail_url: str) -> str:
    body_title = page_title(raw_html)
    heading = meaningful_heading(raw_html)
    summary = detail_summary(raw_html)
    full_context = clean_spaces(" ".join([candidate.title, candidate.context, heading, body_title, summary]))

    if candidate.banca == "lasalle":
        return lasalle_certame_text(raw_html) or full_context[:180]
    if candidate.banca == "fundatec":
        return fundatec_certame_text(raw_html) or full_context[:180]
    if candidate.banca == "faurgs":
        return faurgs_certame_text(raw_html) or heading or summary or candidate.title or body_title or full_context[:180]
    if candidate.banca in {"fgv", "fcc", "cesgranrio"}:
        return body_title or heading or candidate.title or summary or full_context[:180]
    if candidate.banca in {"ibfc", "aocp"}:
        return candidate.title or heading or summary or body_title or full_context[:180]
    if candidate.banca == "objetiva":
        return objetiva_certame_text(candidate, raw_html) or full_context[:180]
    if is_legalle_bank(candidate.banca):
        return summary or heading or body_title or candidate.title or full_context[:180]
    return heading or candidate.title or body_title or summary or detail_url


def discover_details(bank: str, args: argparse.Namespace) -> list[DetailCandidate]:
    candidates: list[DetailCandidate] = []
    seen: set[str] = set()
    for index_url in bank_indexes(args).get(bank, []):
        status, raw, final_url = fetch(index_url, args, bank)
        if status >= 400 or not raw:
            continue
        links = extract_links(raw, final_url or index_url)
        debug(args, f"INDEX {bank} links={len(links)} {final_url}")
        extractor = DETAIL_EXTRACTORS[bank]
        for link in extractor(links):
            if link.url in seen:
                continue
            seen.add(link.url)
            context = nearby_text(raw, link.url, radius=300) or link.text or page_title(raw)
            title = link.text or context[:180] or page_title(raw)
            candidates.append(DetailCandidate(bank, index_url, link.url, clean_spaces(title), clean_spaces(context)))
            debug(args, f"DETAIL_CANDIDATE {bank} {link.url} :: {clean_spaces(title)[:100]}")
            if args.max_candidates_per_bank and len(candidates) >= args.max_candidates_per_bank:
                return candidates
    return candidates


def build_row(candidate: DetailCandidate, registry: RSScopeRegistry, args: argparse.Namespace) -> dict[str, str] | None:
    status, raw, final_url = fetch(candidate.detail_url, args, candidate.banca)
    if status >= 400 or not raw:
        debug(args, f"DETAIL_FAIL {candidate.banca} {status} {candidate.detail_url}")
        return None
    detail_url = final_url or candidate.detail_url
    links = enrich_links_with_context(raw, extract_links(raw, detail_url))
    body_title = page_title(raw)
    heading = meaningful_heading(raw)
    summary = detail_summary(raw)
    full_context = clean_spaces(
        " ".join([candidate.title, candidate.context, heading, body_title, summary, strip_tags(raw[:25000])])
    )
    if is_national_scope(full_context):
        debug(args, f"SKIP_NATIONAL {candidate.banca} {detail_url} :: {clean_spaces(body_title or candidate.title)[:110]}")
        return None
    evidence = candidate_rs_evidence(title=heading or body_title or candidate.title, context=full_context, url=detail_url, registry=registry)
    if not evidence and not has_statewide_rs_signal(full_context):
        debug(args, f"SKIP_SCOPE {candidate.banca} {detail_url} :: {clean_spaces(body_title or candidate.title)[:110]}")
        return None

    doc = best_opening_doc(links)
    title_for_org = certame_text_for(candidate, raw, detail_url)
    pdf_text = ""
    if doc and candidate.banca == "faurgs":
        pdf_text = extract_pdf_text_prefix(doc.url, args)
    municipio = find_municipio(registry, " ".join([title_for_org, doc.text if doc else "", candidate.title]))
    orgao = clean_orgao(title_for_org, municipio)
    needs_pdf_org = not orgao or any(
        token in norm(orgao)
        for token in (
            "emergencial",
            "cadastro de reserva",
            "formadores",
            "cargos",
            "arquivos relacionados",
            "outorga de delegacoes",
            "delegacoes de notas",
            "notas e de registro",
            "notarial e registral",
        )
    )
    if doc and (candidate.banca == "faurgs" or needs_pdf_org):
        if not pdf_text:
            pdf_text = extract_pdf_text_prefix(doc.url, args)
        pdf_orgao = orgao_from_pdf_text(pdf_text)
        if pdf_orgao and (candidate.banca == "faurgs" or needs_pdf_org):
            orgao = clean_orgao(pdf_orgao, municipio)
    orgao_municipio = find_municipio(registry, orgao)
    if orgao_municipio and (
        not municipio or len(normalize_text(orgao_municipio)) > len(normalize_text(municipio))
    ):
        municipio = orgao_municipio
    if not municipio and has_statewide_rs_signal(" ".join([full_context, title_for_org, orgao, doc.text if doc else ""])):
        municipio = "Estatal"
    if not row_quality_ok(orgao, municipio, title_for_org, candidate):
        debug(args, f"SKIP_QUALITY {candidate.banca} {detail_url} :: orgao={orgao[:90]} municipio={municipio or '-'}")
        return None
    numero_parts = [doc.text if doc else "", title_for_org, candidate.title]
    if doc and re.search(r"\bedital[-_\s]*0*\d{1,4}[-_\s]*20\d{2}\b", unquote(doc.url), flags=re.I):
        numero_parts.append(doc.url)
    numero = extract_num(" ".join(numero_parts))
    if not numero and doc:
        if not pdf_text:
            pdf_text = extract_pdf_text_prefix(doc.url, args)
        numero = extract_num(pdf_text[:8000])
        if numero:
            debug(args, f"PDF_NUM {numero} {doc.url}")
    year_text = " ".join(
        [
            title_for_org,
            candidate.title,
            candidate.context,
            heading,
            body_title,
            summary,
            doc.text if doc else "",
            pdf_text[:3000],
        ]
    )
    if args.year and not matches_year(args.year, numero=numero, detail_url=detail_url, text=year_text):
        debug(args, f"SKIP_YEAR {candidate.banca} {detail_url} :: year={args.year} numero={numero or '-'}")
        return None
    tipo = infer_tipo(" ".join([title_for_org, doc.text if doc else "", full_context[:1200]]))

    doc_norm = norm(" ".join([doc.text, doc.url])) if doc else ""
    doc_quality = doc_score(doc) if doc else 0
    primary_retified_edital = bool(
        doc
        and (
            re.search(r"\bedital\s*\(\s*retificad", doc_norm)
            or re.search(r"\bedital[-_\s]*0*1[-_\s]*20\d{2}\b", doc_norm)
        )
    )
    doc_is_secondary = bool(
        doc
        and ("retifica" in doc_norm or "adendo" in doc_norm)
        and "consolidado" not in doc_norm
        and not primary_retified_edital
    )
    semaforo = "listo" if doc and numero and not doc_is_secondary and doc_quality >= 8 else "revisar"
    if doc and doc_is_secondary:
        status_validacao = "pagina_oficial_con_documento_secundario_revisar_edital_abertura"
    else:
        status_validacao = "pagina_oficial_y_edital_abertura" if doc else "pagina_oficial_sin_edital_abertura_confiable"
    row = {
        "semaforo": semaforo,
        "tipo": tipo,
        "orgao": orgao,
        "municipio": municipio,
        "uf": "RS",
        "numero": numero,
        "banca": output_banca(candidate.banca, detail_url),
        "edital_pagina": detail_url,
        "edital_pdf": doc.url.strip() if doc else "",
        "fonte_primaria": "banca",
        "fonte_radar": "",
        "radar_url": "",
        "evidencia_rs": ",".join(evidence),
        "status_validacao": status_validacao,
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "debug_source_index": candidate.index_url,
        "debug_title": title_for_org,
        "debug_doc_title": doc.text if doc else "",
        "debug_doc_count": str(len([l for l in links if l.url.lower().split('?', 1)[0].endswith('.pdf')])),
    }
    digest = hashlib.sha1((row["banca"] + row["edital_pagina"]).encode("utf-8")).hexdigest()[:8]
    debug(
        args,
        "FOUND "
        f"{row['banca']} {digest} semaforo={row['semaforo']} tipo={row['tipo']} "
        f"municipio={row['municipio'] or '-'} numero={row['numero'] or '-'} "
        f"orgao={row['orgao'][:95]}",
    )
    if doc:
        debug(args, f"  DOC {doc_score(doc)} {doc.text[:110]} -> {doc.url}")
    return row


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("sep=;\n")
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Authority-first bank crawler for RS base table")
    parser.add_argument("--out", type=Path, default=OUT_CSV)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep before each HTTP request")
    parser.add_argument("--host-delay", type=float, default=0.75, help="Minimum seconds between requests to the same host")
    parser.add_argument("--lasalle-host-delay", type=float, default=15.0, help="Minimum seconds between La Salle requests")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-fetches-per-bank", type=int, default=120)
    parser.add_argument("--lasalle-max-fetches", type=int, default=8)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "data" / "cache" / "http")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--lasalle-pages", type=int, default=1)
    parser.add_argument("--lasalle-filters", default="inscricoes-abertas,em-andamento")
    parser.add_argument("--max-total", type=int, default=20)
    parser.add_argument("--max-per-bank", type=int, default=4)
    parser.add_argument("--max-candidates-per-bank", type=int, default=80)
    parser.add_argument("--year", default="", help="Optional certame year filter, e.g. 2026")
    parser.add_argument(
        "--banks",
        default=(
            "instituto_legalle,legalle_concursos,lasalle,fundatec,quadrix,objetiva,fenix_selecao,"
            "cebraspe,consulplan,fgv,cesgranrio,faurgs,fcc,aocp,ibfc"
        ),
        help="Comma-separated bank ids",
    )
    args = parser.parse_args()

    registry = RSScopeRegistry.from_csv()
    rows: list[dict[str, str]] = []
    seen_pages: set[str] = set()
    banks = [b.strip() for b in args.banks.split(",") if b.strip()]
    debug(args, f"START banks={banks} max_total={args.max_total} max_per_bank={args.max_per_bank}")
    debug(args, f"RS_REGISTRY municipios={len(registry.municipalities)} hosts={len(registry.official_hosts)}")

    for bank in banks:
        if args.max_total and len(rows) >= args.max_total:
            break
        bank_rows: list[dict[str, str]] = []
        debug(args, f"BANK_START {bank}")
        details = discover_details(bank, args)
        debug(args, f"BANK_DETAILS {bank} candidates={len(details)}")
        for detail in details:
            if detail.detail_url in seen_pages:
                continue
            if args.year and detail.banca in {"lasalle", "fgv", "cesgranrio", "fcc", "faurgs", "aocp", "ibfc"}:
                if not candidate_year_hint(detail, args.year):
                    debug(args, f"SKIP_YEAR_HINT {detail.banca} {detail.detail_url}")
                    continue
            seen_pages.add(detail.detail_url)
            row = build_row(detail, registry, args)
            if not row:
                continue
            bank_rows.append(row)
            ready = [item for item in bank_rows if item["semaforo"] == "listo"]
            if args.max_per_bank and len(ready) >= args.max_per_bank:
                break

        bank_rows.sort(
            key=lambda item: (
                0 if item["semaforo"] == "listo" else 1,
                0 if item["edital_pdf"] else 1,
                0 if item["numero"] else 1,
                item["orgao"],
            )
        )
        remaining_total = args.max_total - len(rows) if args.max_total else len(bank_rows)
        selected = bank_rows[: max(0, min(args.max_per_bank or len(bank_rows), remaining_total))]
        rows.extend(selected)
        debug(args, f"BANK_END {bank} accepted={len(selected)} scanned_rs={len(bank_rows)} total={len(rows)}")

    write_csv(rows, args.out)
    print(f"OUT_CSV {args.out}", flush=True)
    print(f"ROWS {len(rows)}", flush=True)
    by_bank: dict[str, int] = {}
    for row in rows:
        by_bank[row["banca"]] = by_bank.get(row["banca"], 0) + 1
    for bank, count in sorted(by_bank.items()):
        print(f"BANK_ROWS {bank} {count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
