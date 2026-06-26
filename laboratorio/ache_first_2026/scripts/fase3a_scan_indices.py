#!/usr/bin/env python3
"""
Fase 3A - scanner de indices municipales RS.

Entrada:
  data/sites_municipios_rs.csv

Salida:
  data/fase3a_documentos_municipais_rs.csv
  data/fase3a_documentos_municipais_rs.xlsx
  data/fase3a_indices_scan_rs.csv
  data/fase3a_documentos_municipais_rs.md

Esta fase no descarga PDFs ni publica concursos. Convierte paginas indice
oficiales (concursos publicos / processos seletivos) en candidatos de
documentos/eventos: edital, retificacao, convocacao, resultado, homologacao,
gabarito, etc. Cuando el indice apunta a una pagina interna, opcionalmente
entra a esa pagina y captura el mejor PDF/DOC asociado.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import read_csv_dicts, write_table, write_xlsx  # noqa: E402
import fase2c_sites_municipios as sites  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITES = PROJECT_ROOT / "data" / "sites_municipios_rs.csv"
DEFAULT_DOC_CSV = PROJECT_ROOT / "data" / "fase3a_documentos_municipais_rs.csv"
DEFAULT_DOC_XLSX = PROJECT_ROOT / "data" / "fase3a_documentos_municipais_rs.xlsx"
DEFAULT_QUEUE_CSV = PROJECT_ROOT / "data" / "fase3a_download_queue_rs.csv"
DEFAULT_QUEUE_XLSX = PROJECT_ROOT / "data" / "fase3a_download_queue_rs.xlsx"
DEFAULT_SCAN_CSV = PROJECT_ROOT / "data" / "fase3a_indices_scan_rs.csv"
DEFAULT_REPORT = PROJECT_ROOT / "data" / "fase3a_documentos_municipais_rs.md"

CURRENT_YEAR = datetime.now().year

DOC_FIELDS = [
    "candidate_id",
    "uf",
    "municipio",
    "municipio_slug",
    "orgao_guess",
    "source_kind",
    "index_url",
    "source_page_url",
    "source_page_status",
    "candidate_url",
    "candidate_domain",
    "doc_title",
    "doc_type",
    "file_ext",
    "is_pdf",
    "score",
    "score_reasons",
    "edital_nums",
    "edital_num_primary",
    "date_guess",
    "year_guess",
    "best_download_url",
    "download_urls",
    "download_count",
    "detail_probe_status",
    "detail_page_hash",
    "source_page_hash",
    "official_scope",
    "discovered_at",
    "context",
]

SCAN_FIELDS = [
    "uf",
    "municipio",
    "municipio_slug",
    "source_kind",
    "index_url",
    "index_status",
    "final_url",
    "pages_scanned",
    "links_seen",
    "candidates_found",
    "details_probed",
    "downloads_found",
    "page_hashes",
    "error",
    "scanned_at",
]


STATIC_EXTS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".webp", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".odt", ".ods"}

NAV_TEXT = {
    "inicio", "home", "mapa do site", "acessibilidade", "ouvidoria", "contato",
    "facebook", "instagram", "twitter", "linkedin", "whatsapp", "youtube",
    "voltar", "imprimir", "compartilhar", "alto contraste", "fonte original",
}

NOISE_TERMS = {
    "licitacao", "pregao", "dispensa", "inexigibilidade", "leilao",
    "concurso artistico", "concurso cultural", "concursos culturais",
    "fotografia", "fotografias", "transito", "soberanas", "rainha",
    "aldir blanc", "patrocinio", "bolsa patrocinio", "contas publicas",
    "despesa", "receita", "recursos recebidos", "recursos repassados",
    "transferencia constitucional", "transferencias recebidos",
    "repasses e transferencias", "relatorios contabeis", "relatorios do rh", "diarias",
    "perguntas frequentes", "nota fiscal", "sistema administrativo",
    "todos os atalhos", "acessibilidade",
    "demonstrativo", "execucao da despesa", "execucao da receita",
    "balanco orcamentario", "tributos arrecadados", "itens de empenho",
    "cargos e salarios", "salarios por colaborador", "ata de posse prefeito",
    "edital taxi", "programa rs qualificacao", "folder corona",
}

CONTEXT_NOISE_PHRASES = {
    "concurso de fotografias", "educacao para o transito", "concurso artistico",
    "concurso cultural", "concursos culturais", "bolsa patrocinio",
}

DOC_TYPE_RULES: Sequence[Tuple[str, Sequence[str]]] = (
    ("convocacao", ("convocacao", "convoca candidato", "convoca candidatos", "chamamento", "nomeacao", "nomear", "nomeou", "nomeado", "nomeada", "posse")),
    ("resultado", ("resultado final", "resultado preliminar", "resultado", "aprovados", "classificados")),
    ("classificacao", ("classificacao final", "classificacao preliminar", "classificacao", "lista de classificados")),
    ("homologacao", ("homologacao", "homologa")),
    ("gabarito", ("gabarito",)),
    ("retificacao", ("retificacao", "retificado", "retifica")),
    ("inscricoes_homologadas", ("homologacao das inscricoes", "inscricoes homologadas", "relacao de inscritos")),
    ("cronograma", ("cronograma",)),
    ("recurso", ("recurso", "recursos")),
    ("edital_abertura", ("edital de abertura", "abertura das inscricoes", "abertura de inscricoes")),
    ("edital", ("edital",)),
    ("processo_seletivo", ("processo seletivo", "processo seletivo simplificado", "pss")),
    ("concurso_publico", ("concurso publico", "concursos publicos")),
)


@dataclass
class Anchor:
    href: str
    text: str
    title: str = ""


class AnchorExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: List[Anchor] = []
        self._stack: List[Dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        data = {k.lower(): v or "" for k, v in attrs}
        self._stack.append({"href": data.get("href", ""), "title": data.get("title", "") or data.get("aria-label", ""), "text": ""})

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1]["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._stack:
            return
        data = self._stack.pop()
        self.anchors.append(Anchor(data["href"], collapse_ws(data["text"]), collapse_ws(data["title"])))


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def clean_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", urlencode(query_pairs), ""))


def file_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    match = re.search(r"(\.[a-z0-9]{2,5})$", path)
    return match.group(1) if match else ""


def page_title(raw_html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw_html or "")
    return collapse_ws(sites.visible_text(match.group(1))) if match else ""


def source_hash(raw_html: str) -> str:
    return hashlib.sha256((raw_html or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


def candidate_id(url: str, source_kind: str) -> str:
    return hashlib.sha256(f"{source_kind}|{url}".encode("utf-8", errors="ignore")).hexdigest()[:16]


def extract_anchors(raw_html: str, base_url: str) -> List[Anchor]:
    parser = AnchorExtractor()
    try:
        parser.feed(raw_html or "")
    except Exception:
        pass
    out: List[Anchor] = []
    for anchor in parser.anchors:
        href = (anchor.href or "").strip()
        if not href or href.startswith("#"):
            continue
        low = href.lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "sms:")):
            continue
        url = clean_url(urljoin(base_url, href))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if file_ext(url) in STATIC_EXTS:
            continue
        text = collapse_ws(anchor.text or anchor.title)
        out.append(Anchor(url, text, anchor.title))
    return out


def extract_context(raw_html: str, href: str, fallback: str, window: int = 700) -> str:
    raw = raw_html or ""
    needle = href
    pos = raw.find(needle)
    if pos < 0:
        parsed = urlparse(href)
        pos = raw.find(parsed.path) if parsed.path else -1
    if pos >= 0:
        chunk = raw[max(0, pos - window): pos + window]
        text = sites.visible_text(chunk)
    else:
        text = fallback
    return collapse_ws(text)[:800]


def classify_doc_type(blob_norm: str) -> str:
    for doc_type, terms in DOC_TYPE_RULES:
        if any(term in blob_norm for term in terms):
            return doc_type
    return ""


def extract_date_guess(text: str) -> str:
    match = re.search(r"\b(\d{1,2}[./-]\d{1,2}[./-]20\d{2})\b", text or "")
    return match.group(1) if match else ""


def extract_year_guess(text: str) -> str:
    years = re.findall(r"\b(20\d{2})\b", text or "")
    if not years:
        return ""
    years = sorted(set(years), reverse=True)
    return years[0]


def extract_edital_nums(text: str) -> List[str]:
    patterns = [
        r"\b(?:edital|concurso)\s*(?:de\s+concurso\s*)?(?:n[ºo.]?\s*)?(\d{1,4})\s*/\s*(20\d{2})",
        r"\b(?:edital|concurso)[\s\-_]*(?:n[ºo.]?[\s\-_]*)?(\d{1,4})[\s\-_]+(20\d{2})",
        r"\bn[ºo.]?\s*(\d{1,4})\s*/\s*(20\d{2})",
    ]
    found: List[str] = []
    norm_text = sites.normalize(text or "")
    for pattern in patterns:
        for num, year in re.findall(pattern, norm_text, flags=re.I):
            value = f"{num.zfill(2) if len(num) < 2 else num}/{year}"
            if value not in found:
                found.append(value)
    return found[:6]


def official_scope(candidate_url: str, index_url: str) -> str:
    c = urlparse(candidate_url)
    i = urlparse(index_url)
    if c.netloc == i.netloc:
        return "same_host"
    if c.netloc.endswith(".rs.gov.br") or c.netloc.endswith(".gov.br"):
        return "gov_br"
    if any(token in c.netloc for token in ("fundatec", "legalle", "objetivas", "quadrix", "cebraspe", "faurgs", "fundacaolasalle")):
        return "banca_known"
    return "external_linked_from_official_index"


def classify_link(anchor: Anchor, context: str, source_kind: str, index_url: str) -> Dict[str, object]:
    title = collapse_ws(anchor.text or anchor.title or context)
    ext = file_ext(anchor.href)
    is_doc_file = ext in DOC_EXTS
    url_title_norm = sites.normalize(f"{anchor.href} {title}")
    blob_norm = sites.normalize(f"{anchor.href} {title} {context}")

    if any(noise in url_title_norm for noise in NOISE_TERMS) or any(noise in blob_norm for noise in CONTEXT_NOISE_PHRASES):
        return {"score": 0, "doc_type": "", "reasons": "noise"}
    if sites.normalize(title) in NAV_TEXT:
        return {"score": 0, "doc_type": "", "reasons": "navigation"}
    title_norm = sites.normalize(title)
    if not is_doc_file and (
        re.fullmatch(r"20\d{2}", title_norm)
        or re.fullmatch(r"\d+\s+(concursos|processos seletivos)", title_norm)
        or title_norm in {"ativos", "inativos", "janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"}
        or title_norm in {"editais", "concursos publicos", "processos seletivos", "todos"}
        or title_norm.startswith("exportar em")
    ):
        return {"score": 0, "doc_type": "", "reasons": "section"}
    parsed_anchor = urlparse(anchor.href)
    query_keys = {key.lower() for key, _ in parse_qsl(parsed_anchor.query, keep_blank_values=True)}
    path_norm = sites.normalize(parsed_anchor.path)
    if not is_doc_file and (query_keys & {"pagina", "page", "ano", "mes", "status", "tipo"} or "/categoria/" in parsed_anchor.path.lower() or "/category/" in parsed_anchor.path.lower()):
        return {"score": 0, "doc_type": "", "reasons": "filter_page"}
    if not is_doc_file and parsed_anchor.path in {"", "/"}:
        return {"score": 0, "doc_type": "", "reasons": "root_page"}
    if not is_doc_file and official_scope(anchor.href, index_url) == "external_linked_from_official_index":
        return {"score": 0, "doc_type": "", "reasons": "external_nonfile"}

    edital_nums = extract_edital_nums(f"{title} {context} {anchor.href}")
    doc_type_from_url_title = classify_doc_type(url_title_norm)
    doc_type = classify_doc_type(blob_norm)
    if not doc_type and source_kind == "processos_seletivos" and "seletivo" in blob_norm:
        doc_type = "processo_seletivo"
    if not doc_type and source_kind == "concursos_publicos" and "concurso" in blob_norm:
        doc_type = "concurso_publico"
    doc_signal_in_url_title = bool(doc_type_from_url_title) or any(
        term in url_title_norm
        for term in ("concurso", "processo seletivo", "seletivo", "edital", "convoca", "resultado", "gabarito", "homolog", "retifica", "nomear", "nomeou", "portaria")
    )
    doc_signal_in_blob = bool(doc_type) or bool(edital_nums) or any(
        term in blob_norm
        for term in ("concurso", "processo seletivo", "seletivo", "edital", "convoca", "resultado", "gabarito", "homolog", "retifica", "nomear", "nomeou", "portaria")
    )
    if not is_doc_file and doc_type and not doc_signal_in_url_title:
        return {"score": 0, "doc_type": "", "reasons": "context_only_section"}
    if is_doc_file and not doc_signal_in_blob:
        return {"score": 0, "doc_type": "", "reasons": "file_without_doc_signal"}
    if doc_type == "recurso" and not any(term in blob_norm for term in ("edital", "concurso", "processo seletivo", "inscricao", "prova", "gabarito")):
        return {"score": 0, "doc_type": "", "reasons": "generic_recurso"}
    date_guess = extract_date_guess(f"{title} {context}")
    year_guess = extract_year_guess(f"{title} {context} {anchor.href}")

    score = 0
    reasons: List[str] = []
    if doc_type:
        score += 7
        reasons.append(f"type:{doc_type}")
    if ext == ".pdf":
        score += 5
        reasons.append("pdf")
    elif ext in DOC_EXTS:
        score += 3
        reasons.append(f"file:{ext.lstrip('.')}")
    if edital_nums:
        score += 3
        reasons.append("edital_num")
    if date_guess:
        score += 1
        reasons.append("date")
    if year_guess and int(year_guess) >= CURRENT_YEAR - 2:
        score += 1
        reasons.append("recent_year")
    if source_kind == "concursos_publicos" and ("concurso publico" in blob_norm or "concursos publicos" in blob_norm):
        score += 1
        reasons.append("source_kind")
    if source_kind == "processos_seletivos" and ("processo seletivo" in blob_norm or "processos seletivos" in blob_norm or "pss" in blob_norm):
        score += 1
        reasons.append("source_kind")
    if official_scope(anchor.href, index_url) in {"same_host", "gov_br", "banca_known"}:
        score += 1
        reasons.append("official_link")

    return {
        "score": score,
        "doc_type": doc_type,
        "reasons": ",".join(reasons),
        "edital_nums": edital_nums,
        "date_guess": date_guess,
        "year_guess": year_guess,
        "file_ext": ext,
        "is_pdf": "1" if ext == ".pdf" else "0",
        "title": title[:500],
    }


def should_follow_index_page(anchor: Anchor, source_kind: str, index_url: str, current_url: str) -> bool:
    url = anchor.href
    if clean_url(url) in {clean_url(index_url), clean_url(current_url)}:
        return False
    if urlparse(url).netloc != urlparse(index_url).netloc:
        return False
    ext = file_ext(url)
    if ext and ext not in {""}:
        return False
    url_blob = sites.normalize(f"{url} {anchor.text} {anchor.title}")
    if any(noise in url_blob for noise in NOISE_TERMS):
        return False
    path_low = urlparse(url).path.lower()
    if "/pages/" in path_low or any(term in url_blob for term in ("edital", "convoca", "resultado", "homologacao", "gabarito", "retificacao")):
        return False
    source_terms = ("concurso", "concursos", "concursos publicos") if source_kind == "concursos_publicos" else (
        "processo seletivo", "processos seletivos", "seletivos", "pss"
    )
    if any(term in url_blob for term in source_terms):
        year_match = re.search(r"\b(20\d{2})\b", url_blob)
        if not year_match:
            return False
        if int(year_match.group(1)) < CURRENT_YEAR - 1:
            return False
        return True
    url_path_norm = sites.normalize(urlparse(url).path)
    if re.search(r"\b(proxima|proximo|pagina|page|marcador)\b", url_blob) and any(term in url_path_norm for term in source_terms):
        return True
    return False


def best_download_from_detail(
    detail_url: str,
    detail_html: str,
    parent_title: str,
    source_kind: str,
    index_url: str,
) -> Tuple[str, List[str]]:
    anchors = extract_anchors(detail_html, detail_url)
    scored: List[Tuple[int, str]] = []
    parent_context = f"{parent_title} {page_title(detail_html)}"
    for anchor in anchors:
        context = extract_context(detail_html, anchor.href, parent_context)
        info = classify_link(anchor, f"{parent_context} {context}", source_kind, index_url)
        ext = str(info.get("file_ext") or "")
        if ext in DOC_EXTS and int(info.get("score") or 0) >= 5:
            bonus = 10 if ext == ".pdf" else 0
            scored.append((int(info["score"]) + bonus, anchor.href))
    scored.sort(reverse=True)
    urls: List[str] = []
    for _, url in scored:
        if url not in urls:
            urls.append(url)
    return (urls[0] if urls else ""), urls[:10]


def source_rows(site_rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in site_rows:
        if row.get("concursos_url"):
            out.append({**row, "source_kind": "concursos_publicos", "index_url": row["concursos_url"]})
        if row.get("processos_seletivos_url"):
            out.append({**row, "source_kind": "processos_seletivos", "index_url": row["processos_seletivos_url"]})
    return out


def scan_one(source: Dict[str, str], args: argparse.Namespace) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    discovered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    index_url = source["index_url"]
    source_kind = source["source_kind"]
    municipio = source["municipio"]
    scan = {
        "uf": source.get("uf", "RS"),
        "municipio": municipio,
        "municipio_slug": source.get("municipio_slug", ""),
        "source_kind": source_kind,
        "index_url": index_url,
        "index_status": "",
        "final_url": "",
        "pages_scanned": 0,
        "links_seen": 0,
        "candidates_found": 0,
        "details_probed": 0,
        "downloads_found": 0,
        "page_hashes": "",
        "error": "",
        "scanned_at": discovered_at,
    }

    docs_by_url: Dict[str, Dict[str, object]] = {}
    page_hashes: List[str] = []
    queue = [index_url]
    visited: set[str] = set()

    with sites.requests.Session() as session:
        while queue and len(visited) < args.max_pages_per_index:
            page_url = clean_url(queue.pop(0))
            if page_url in visited:
                continue
            visited.add(page_url)
            fetch = sites.fetch_url(page_url, args.timeout, session)
            if page_url == index_url:
                scan["index_status"] = fetch.status
                scan["final_url"] = fetch.final_url or page_url
                if fetch.error:
                    scan["error"] = fetch.error
            if fetch.status != 200 or not fetch.body:
                continue
            scan["pages_scanned"] = int(scan["pages_scanned"]) + 1
            p_hash = source_hash(fetch.body)
            page_hashes.append(f"{page_url}#{p_hash}")
            anchors = extract_anchors(fetch.body, fetch.final_url or page_url)
            scan["links_seen"] = int(scan["links_seen"]) + len(anchors)
            page_hash_value = p_hash

            for anchor in anchors:
                if should_follow_index_page(anchor, source_kind, index_url, page_url):
                    next_url = clean_url(anchor.href)
                    if next_url not in visited and next_url not in queue:
                        queue.append(next_url)

                if clean_url(anchor.href) in {clean_url(index_url), clean_url(page_url)}:
                    continue
                context = extract_context(fetch.body, anchor.href, anchor.text or page_title(fetch.body))
                info = classify_link(anchor, context, source_kind, index_url)
                score = int(info.get("score") or 0)
                if score < args.score_threshold:
                    continue
                url = clean_url(anchor.href)
                existing = docs_by_url.get(url)
                if existing and int(existing["score"]) >= score:
                    continue
                edital_nums = info.get("edital_nums") or []
                row = {
                    "candidate_id": candidate_id(url, source_kind),
                    "uf": source.get("uf", "RS"),
                    "municipio": municipio,
                    "municipio_slug": source.get("municipio_slug", ""),
                    "orgao_guess": f"Prefeitura Municipal de {municipio}",
                    "source_kind": source_kind,
                    "index_url": index_url,
                    "source_page_url": page_url,
                    "source_page_status": fetch.status,
                    "candidate_url": url,
                    "candidate_domain": urlparse(url).netloc,
                    "doc_title": info.get("title") or anchor.text or page_title(fetch.body),
                    "doc_type": info.get("doc_type") or "",
                    "file_ext": info.get("file_ext") or "",
                    "is_pdf": info.get("is_pdf") or "0",
                    "score": score,
                    "score_reasons": info.get("reasons") or "",
                    "edital_nums": ";".join(edital_nums),
                    "edital_num_primary": edital_nums[0] if edital_nums else "",
                    "date_guess": info.get("date_guess") or "",
                    "year_guess": info.get("year_guess") or "",
                    "best_download_url": url if info.get("file_ext") in DOC_EXTS else "",
                    "download_urls": json.dumps([url], ensure_ascii=False) if info.get("file_ext") in DOC_EXTS else "",
                    "download_count": 1 if info.get("file_ext") in DOC_EXTS else 0,
                    "detail_probe_status": "not_needed_file" if info.get("file_ext") in DOC_EXTS else "pending",
                    "detail_page_hash": "",
                    "source_page_hash": page_hash_value,
                    "official_scope": official_scope(url, index_url),
                    "discovered_at": discovered_at,
                    "context": context,
                }
                docs_by_url[url] = row

        # Detail pages: keep bounded. Newest items usually appear first in municipal indexes.
        probed = 0
        for row in sorted(docs_by_url.values(), key=lambda r: int(r["score"]), reverse=True):
            if probed >= args.max_detail_pages_per_index:
                break
            if row["best_download_url"] or file_ext(str(row["candidate_url"])) in DOC_EXTS:
                continue
            if urlparse(str(row["candidate_url"])).netloc != urlparse(index_url).netloc:
                continue
            detail_fetch = sites.fetch_url(str(row["candidate_url"]), args.timeout, session)
            probed += 1
            row["detail_probe_status"] = f"{detail_fetch.status}" if detail_fetch.status else detail_fetch.error
            if detail_fetch.status == 200 and detail_fetch.body:
                row["detail_page_hash"] = source_hash(detail_fetch.body)
                if not row["date_guess"]:
                    row["date_guess"] = extract_date_guess(sites.visible_text(detail_fetch.body)[:3000])
                if not row["edital_nums"]:
                    nums = extract_edital_nums(f"{row['doc_title']} {sites.visible_text(detail_fetch.body)[:4000]}")
                    row["edital_nums"] = ";".join(nums)
                    row["edital_num_primary"] = nums[0] if nums else ""
                best, downloads = best_download_from_detail(
                    str(row["candidate_url"]),
                    detail_fetch.body,
                    str(row["doc_title"]),
                    source_kind,
                    index_url,
                )
                if best:
                    row["best_download_url"] = best
                    row["download_urls"] = json.dumps(downloads, ensure_ascii=False)
                    row["download_count"] = len(downloads)
                    row["detail_probe_status"] = f"{detail_fetch.status}:downloads_found"

    docs = list(docs_by_url.values())
    scan["candidates_found"] = len(docs)
    scan["details_probed"] = sum(1 for row in docs if str(row.get("detail_probe_status", "")).startswith("200"))
    scan["downloads_found"] = sum(1 for row in docs if row.get("best_download_url"))
    scan["page_hashes"] = json.dumps(page_hashes[:20], ensure_ascii=False)
    return docs, scan


def write_report(rows: List[Dict[str, object]], scans: List[Dict[str, object]], path: Path) -> None:
    by_type: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    for row in rows:
        by_type[str(row.get("doc_type") or "unknown")] = by_type.get(str(row.get("doc_type") or "unknown"), 0) + 1
        by_kind[str(row.get("source_kind") or "")] = by_kind.get(str(row.get("source_kind") or ""), 0) + 1

    sources_ok = sum(1 for scan in scans if int(scan.get("pages_scanned") or 0) > 0)
    downloads = sum(1 for row in rows if row.get("best_download_url"))
    path.write_text(
        "\n".join([
            "# Fase 3A - Scanner de indices municipais RS",
            "",
            f"- Indices escaneados: {len(scans)}",
            f"- Indices con pagina cargada: {sources_ok}/{len(scans)}",
            f"- Documentos/eventos candidatos: {len(rows)}",
            f"- Candidatos con download/PDF/DOC detectado: {downloads}",
            f"- Tipos: {json.dumps(by_type, ensure_ascii=False, sort_keys=True)}",
            f"- Fuentes: {json.dumps(by_kind, ensure_ascii=False, sort_keys=True)}",
            "",
            "## Archivos",
            "",
            f"- CSV documentos: {DEFAULT_DOC_CSV}",
            f"- Excel documentos: {DEFAULT_DOC_XLSX}",
            f"- CSV cola descarga: {DEFAULT_QUEUE_CSV}",
            f"- Excel cola descarga: {DEFAULT_QUEUE_XLSX}",
            f"- CSV scan log: {DEFAULT_SCAN_CSV}",
            "",
            "## Nota",
            "",
            "- Esta fase no descarga archivos: solo localiza paginas/documentos oficiales enlazados desde indices municipales.",
            "- `best_download_url` es la URL preferida para la siguiente fase de descarga + SHA256.",
            "- `candidate_url` puede ser una pagina interna oficial cuando aun no se detecto PDF/DOC directo.",
        ]),
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fase 3A - scan indices municipales RS")
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES)
    parser.add_argument("--csv", type=Path, default=DEFAULT_DOC_CSV)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_DOC_XLSX)
    parser.add_argument("--queue-csv", type=Path, default=DEFAULT_QUEUE_CSV)
    parser.add_argument("--queue-xlsx", type=Path, default=DEFAULT_QUEUE_XLSX)
    parser.add_argument("--scan-csv", type=Path, default=DEFAULT_SCAN_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=int, default=6)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--score-threshold", type=int, default=6)
    parser.add_argument("--max-pages-per-index", type=int, default=3)
    parser.add_argument("--max-detail-pages-per-index", type=int, default=10)
    parser.add_argument("--limit-sources", type=int, default=0)
    parser.add_argument("--municipio", action="append", default=[], help="Filtra por municipio_slug o nombre normalizado")
    args = parser.parse_args(argv)

    t0 = time.time()
    site_rows = read_csv_dicts(args.sites)
    sources = source_rows(site_rows)
    if args.municipio:
        wanted = {sites.slug_compact(v) for v in args.municipio}
        sources = [row for row in sources if sites.slug_compact(row.get("municipio", "")) in wanted or row.get("municipio_slug") in wanted]
    if args.limit_sources:
        sources = sources[: args.limit_sources]

    print("Fase 3A - scanner de indices municipales RS")
    print(f"  municipios rows : {len(site_rows)}")
    print(f"  indices a scan  : {len(sources)}")
    print(f"  max pages/index : {args.max_pages_per_index}")
    print(f"  max detail/index: {args.max_detail_pages_per_index}")

    all_docs: List[Dict[str, object]] = []
    scans: List[Dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(scan_one, source, args): source for source in sources}
        for idx, future in enumerate(as_completed(future_map), start=1):
            source = future_map[future]
            try:
                docs, scan = future.result()
            except Exception as exc:  # noqa: BLE001
                scan = {
                    "uf": source.get("uf", "RS"),
                    "municipio": source.get("municipio", ""),
                    "municipio_slug": source.get("municipio_slug", ""),
                    "source_kind": source.get("source_kind", ""),
                    "index_url": source.get("index_url", ""),
                    "index_status": "",
                    "final_url": "",
                    "pages_scanned": 0,
                    "links_seen": 0,
                    "candidates_found": 0,
                    "details_probed": 0,
                    "downloads_found": 0,
                    "page_hashes": "",
                    "error": type(exc).__name__,
                    "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                docs = []
            all_docs.extend(docs)
            scans.append(scan)
            if idx % 25 == 0:
                print(f"  {idx:03d}/{len(sources)} indices | docs={len(all_docs)}")

    all_docs.sort(key=lambda r: (str(r["municipio"]), str(r["source_kind"]), -int(r["score"]), str(r["doc_title"])))
    scans.sort(key=lambda r: (str(r["municipio"]), str(r["source_kind"])))

    write_table(all_docs, DOC_FIELDS, args.csv, sheet_name="RS documentos")
    write_xlsx(all_docs, DOC_FIELDS, args.xlsx, sheet_name="RS documentos")
    queue_rows = [row for row in all_docs if row.get("best_download_url")]
    write_table(queue_rows, DOC_FIELDS, args.queue_csv, sheet_name="RS download queue")
    write_xlsx(queue_rows, DOC_FIELDS, args.queue_xlsx, sheet_name="RS download queue")
    write_table(scans, SCAN_FIELDS, args.scan_csv, sheet_name="RS scan")
    write_report(all_docs, scans, args.report)

    print()
    print("=============== FASE 3A - INDICES MUNICIPALES RS ===============")
    print(f"  Indices escaneados              : {len(scans)}")
    print(f"  Indices con pagina cargada       : {sum(1 for s in scans if int(s.get('pages_scanned') or 0) > 0)}")
    print(f"  Documentos/eventos candidatos    : {len(all_docs)}")
    print(f"  Con download/PDF/DOC detectado   : {sum(1 for r in all_docs if r.get('best_download_url'))}")
    print(f"  CSV documentos : {args.csv}")
    print(f"  Excel          : {args.xlsx}")
    print(f"  CSV queue      : {args.queue_csv}")
    print(f"  Excel queue    : {args.queue_xlsx}")
    print(f"  CSV scan log   : {args.scan_csv}")
    print(f"  Report         : {args.report}")
    print(f"  Tiempo         : {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
