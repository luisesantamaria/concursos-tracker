from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CRAWLER_PATH = PROJECT_ROOT / "scripts" / "fase1_bancas" / "crawl_bancas_base_rs.py"


BASE_FIELDS = [
    "ano",
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

AI_FIELDS = [
    "ai_decision",
    "ai_confidence",
    "ai_changed_fields",
    "ai_issues",
    "ai_evidence",
    "ai_validation",
    "ai_model",
    "ai_checked_at",
]

DEFAULT_MODEL = "qwen2.5:3b-instruct"
DEFAULT_OPENAI_MODEL = "Qwen/Qwen2.5-7B-Instruct-AWQ"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


@dataclass
class PageEvidence:
    url: str
    status: int
    title: str
    heading: str
    text: str
    links: list[Any]


@dataclass
class DocEvidence:
    url: str
    source_page: str
    anchor: str
    score: int
    opening_signal: bool
    accessory_signal: bool
    text_prefix: str


def load_crawler() -> Any:
    spec = importlib.util.spec_from_file_location("bancas_crawler", CRAWLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load crawler: {CRAWLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bancas_crawler"] = module
    spec.loader.exec_module(module)
    return module


def detect_delimiter(path: Path) -> tuple[str, list[str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        return ",", []
    if lines[0].startswith("sep="):
        delimiter = lines[0].split("=", 1)[1] or ";"
        return delimiter, lines[1:]
    sample = "\n".join(lines[:5])
    comma = sample.count(",")
    semicolon = sample.count(";")
    return (";" if semicolon > comma else ","), lines


def read_csv(path: Path) -> list[dict[str, str]]:
    delimiter, lines = detect_delimiter(path)
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter=delimiter)
    return [{k: (v or "") for k, v in row.items()} for row in reader]


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def debug(args: argparse.Namespace, message: str) -> None:
    if args.debug:
        print(message, flush=True)


def make_crawler_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        debug=args.debug_fetch,
        delay=args.delay,
        host_delay=args.host_delay,
        lasalle_host_delay=args.lasalle_host_delay,
        timeout=args.timeout,
        refresh_cache=args.refresh_cache,
        cache_dir=str(args.cache_dir),
        max_fetches_per_bank=args.max_fetches_per_bank,
        lasalle_max_fetches=args.lasalle_max_fetches,
        retries=args.retries,
    )


def clean_text_for_prompt(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + " [...]"


def compact_url(url: str, limit: int = 180) -> str:
    if len(url or "") <= limit:
        return url
    parsed = urlparse(url)
    tail = parsed.path[-80:]
    return urlunparse(parsed._replace(path="..." + tail))


def field_diffs(before: dict[str, str], after: dict[str, str], fields: list[str]) -> list[str]:
    diffs: list[str] = []
    for field in fields:
        old = str(before.get(field, ""))
        new = str(after.get(field, ""))
        if old != new:
            diffs.append(f"{field}: '{old[:90]}' -> '{new[:90]}'")
    return diffs


def doc_blob(doc: DocEvidence) -> str:
    return " ".join([doc.anchor or "", doc.url or "", doc.text_prefix or ""]).lower()


def match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    return re.sub(r"\s+", " ", value)


def doc_label_blob(doc: DocEvidence) -> str:
    return match_text(" ".join([doc.anchor or "", doc.url or ""]))


def doc_has_accessory_words(doc: DocEvidence) -> bool:
    blob = doc_label_blob(doc)
    # "Edital (retificado...)" is often the consolidated opening edital. Only
    # explicit retificacao/adendo docs should be treated as accessory here.
    return any(
        token in blob
        for token in (
            "homolog",
            "resultado",
            "classifica",
            "gabarito",
            "convoca",
            "sorteio",
            "prova pratica",
            "prova prÃ¡tica",
            "notas ",
            "retificacao",
            "retificaÃ§Ã£o",
            "adendo",
            "cronograma",
            "extraordinario",
            "extraordinario",
            "prosseguimento",
            "cancelamento",
            "eliminacao",
            "prorrogacao",
            "tipo 1",
            "tipo 2",
            "caderno de prova",
            "prova objetiva",
            "provas objetivas",
        )
    )


def is_priority_edital_link(crawler: Any, link: Any) -> bool:
    blob = crawler.norm(" ".join([getattr(link, "text", "") or "", getattr(link, "url", "") or ""]))
    if not any(token in blob for token in ("edital", "processo seletivo simplificado", "pss n")):
        return False
    return not any(
        token in blob
        for token in (
            "resultado",
            "homolog",
            "classifica",
            "gabarito",
            "convoca",
            "cronograma",
            "comunicado",
            "prova",
            "tipo 1",
            "tipo 2",
            "anexo",
            "retificacao",
            "retificaÃ§Ã£o",
        )
    )


def looks_like_site_specific_doc(crawler: Any, source_url: str, link: Any) -> bool:
    url = normalize_url(getattr(link, "url", ""))
    if not url:
        return False
    host = urlparse(source_url).netloc.lower()
    blob = crawler.norm(" ".join([getattr(link, "text", "") or "", url, getattr(link, "context", "") or ""]))
    if "portalfaurgs.com.br" in host:
        return "lerarquivo" in url.lower() and any(
            token in blob for token in ("edital", "processo seletivo", "pss n", "download")
        )
    if "conhecimento.fgv.br" in host:
        return "edital" in blob and not any(
            token in blob
            for token in (
                "resultado",
                "homolog",
                "classifica",
                "gabarito",
                "convoca",
                "prova objetiva",
                "padrao de resposta",
                "tipo 1",
                "tipo 2",
            )
        )
    return False


def doc_has_strong_opening_words(doc: DocEvidence) -> bool:
    blob = doc_blob(doc)
    return any(token in blob for token in ("edital de abertura", "abertura e inscr", "abertura das inscr", "edital n")) or doc_has_base_edital_words(doc)


def doc_has_base_edital_words(doc: DocEvidence) -> bool:
    label = doc_label_blob(doc)
    body = match_text(doc.text_prefix or "")
    blob = " ".join([label, body])
    if doc_has_accessory_words(doc):
        return False
    return bool(
        "edital de abertura" in blob
        or "edital abertura" in blob
        or "abertura e inscr" in blob
        or "edital pss" in blob
        or re.search(r"\bprocesso\s+seletivo\s+simplificado\s*\(?pss\)?\s*n[^\d]{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}\b", blob)
        or re.search(r"\bpss\s*n[^\d]{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}\b", blob)
        or re.search(r"\bedital\s*(?:pss\s*)?n[ÂºoÂ°.]?\s*\d{1,4}\s*[/.-]\s*20\d{2}\b", blob)
        or re.search(r"\bedital\s*n[ÂºoÂ°.]?\s*\d{1,4}\s*\(\s*abertura\s*\)", blob)
        or re.search(r"\bedital\s*[-â€“:]\s*(?:concurso publico|processo seletivo)", blob)
        or re.search(r"\bedital\s*n[ÂºoÂ°.]?\s*\d{1,4}\s*[/.-]\s*20\d{2}.*(?:concurso publico|processo seletivo|inscricoes|pgm)", blob)
    )


def url_key(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    parsed = parsed._replace(query="", fragment="")
    return urlunparse(parsed).rstrip("/").lower()


def doc_rank(doc: DocEvidence, current_pdf_norm: str, current_page_norm: str = "") -> tuple[int, int, int, int]:
    is_current = normalize_url(doc.url) == current_pdf_norm
    is_current_page_doc = bool(current_page_norm and url_key(doc.source_page) == url_key(current_page_norm))
    strong_opening = doc_has_strong_opening_words(doc) and not doc_has_accessory_words(doc)
    clean_opening = doc.opening_signal and not doc_has_accessory_words(doc)
    base_edital = doc_has_base_edital_words(doc)
    return (
        (8 if is_current_page_doc else 0)
        + (3 if is_current else 0)
        + (8 if base_edital else 0)
        + (6 if strong_opening else 0)
        + (4 if clean_opening else 0),
        doc.score,
        -len(doc.url),
        -len(doc.anchor),
    )


def deterministic_base_label(doc: DocEvidence) -> bool:
    label = doc_label_blob(doc)
    body = match_text(doc.text_prefix or "")
    blob = " ".join([label, body])
    base = bool(
        "edital de abertura" in blob
        or "edital pss" in blob
        or "edital de concurso publico" in blob
        or "abertura e inscr" in blob
        or re.search(r"\bprocesso\s+seletivo\s+simplificado\s*\(?pss\)?\s*n[^\d]{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}\b", blob)
        or re.search(r"\bpss\s*n[^\d]{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}\b", blob)
        or re.search(r"\bedital\s*(?:pss\s*)?n\S{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}\b", blob)
        or re.search(r"\bedital\s*n\S{0,4}\s*\d{1,4}\s*\(\s*abertura\s*\)", blob)
        or re.search(r"\bedital\s*n\S{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}.*(?:edital|concurso publico|processo seletivo|inscricoes|pgm)", blob)
        or re.search(r"\bedital\s+(?:de\s+)?concurso publico\s*n?\S{0,4}\s*\d{1,4}\s*[/.-]\s*20\d{2}", blob)
        or re.search(r"\bconcurso publico\s*(?:[-:â€¢]\s*)?(?:edital\s*)?\d{1,4}\s*[/.-]\s*20\d{2}", blob)
        or re.search(r"\bedital\s+\d{1,4}\s*[/.-]\s*20\d{2}", blob)
    )
    if not base:
        return False
    label_only_accessory = any(
        token in label
        for token in (
            "homolog",
            "resultado",
            "classifica",
            "gabarito",
            "convoca",
            "retificacao",
            "retificaÃ§Ã£o",
            "adendo",
            "cronograma",
            "ato oficial",
            "extraordinario",
            "prosseguimento",
            "cancelamento",
            "eliminacao",
            "prorrogacao",
        )
    )
    return not label_only_accessory


def best_deterministic_doc(row: dict[str, str], docs: list[DocEvidence]) -> DocEvidence | None:
    current_page = normalize_url(row.get("edital_pagina", ""))
    current_pdf = normalize_url(row.get("edital_pdf", ""))
    expected_num = normalize_numero(row.get("numero", ""))
    expected_year = row.get("ano", "")
    candidates: list[tuple[tuple[int, int, int, int], DocEvidence]] = []
    for doc in docs:
        if not deterministic_base_label(doc):
            continue
        doc_num = extract_numero_from_text(" ".join([doc.anchor, doc.text_prefix, doc.url]))
        if expected_num and doc_num and doc_num != expected_num:
            same_context = normalize_url(doc.source_page) == current_page or normalize_url(doc.url) == current_pdf
            allow_clean_01_repair = (
                bool(expected_year)
                and expected_num.endswith(f"/{expected_year}")
                and doc_num.endswith(f"/{expected_year}")
                and doc_num.startswith("nÂº 01/")
            )
            if not allow_clean_01_repair and not same_context:
                continue
        candidates.append((doc_rank(doc, current_pdf, current_page), doc))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def deterministic_proposal(row: dict[str, str], pages: list[PageEvidence], docs: list[DocEvidence]) -> dict[str, Any] | None:
    doc = best_deterministic_doc(row, docs)
    if not doc:
        return None
    numero = extract_numero_from_text(" ".join([doc.anchor, doc.text_prefix, doc.url])) or row.get("numero", "")
    return {
        "decision": "listo",
        "confidence": 0.86,
        "tipo": row.get("tipo", ""),
        "orgao": row.get("orgao", ""),
        "municipio": row.get("municipio", ""),
        "uf": row.get("uf", "RS"),
        "numero": numero,
        "edital_pagina": doc.source_page or row.get("edital_pagina", ""),
        "edital_pdf": doc.url,
        "document_class": "edital_abertura",
        "issues": [],
        "evidence": clean_text_for_prompt(f"{doc.anchor or 'documento'} em {compact_url(doc.url, 90)}", 120),
        "_deterministic": True,
    }


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        return url.strip()
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower().removeprefix("www.") == urlparse(b).netloc.lower().removeprefix("www.")


def is_too_broad_page(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    if path in {"", "/", "/portal", "/portal/concursos", "/edital", "/concursos"} and not parsed.query:
        return True
    if any(token in path for token in ("provas_anteriores", "dicasimportantes", "concursos_abertos")):
        return True
    return False


def parent_urls(url: str) -> list[str]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return []
    out: list[str] = []
    parts = [part for part in parsed.path.split("/") if part]
    for cut in range(len(parts), max(len(parts) - 2, 0), -1):
        parent_path = "/" + "/".join(parts[: cut - 1])
        if parent_path == "/" or len([part for part in parent_path.split("/") if part]) < 2:
            continue
        out.append(urlunparse(parsed._replace(path=parent_path + "/", query="", fragment="")))
    return [u for i, u in enumerate(out) if u and u != url and u not in out[:i]]


def related_pages(row: dict[str, str]) -> list[str]:
    url = normalize_url(row.get("edital_pagina", ""))
    banca = (row.get("banca") or "").lower()
    if not url:
        return []
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    out = [url]

    concurso_id = (qs.get("concurso") or [""])[0]
    if "fundatec" in banca and concurso_id:
        base = "https://www.fundatec.org.br/portal/concursos/"
        out.extend(
            [
                f"{base}pagina_editais.php?concurso={concurso_id}",
                f"{base}index_concursos.php?concurso={concurso_id}",
                f"{base}pagina_concurso.php?concurso={concurso_id}",
            ]
        )

    if "legalle" in banca:
        if "editais.legalleconcursos.com.br" in parsed.netloc:
            out.append(urljoin(url, "/edital"))
        if "institutolegalle.org.br" in parsed.netloc:
            out.append("https://portal.institutolegalle.org.br/edital")

    if not any(token in banca for token in ("fundatec", "legalle", "lasalle", "quadrix", "objetiva")):
        out.extend(parent_urls(url))
    return [u for i, u in enumerate(out) if u and u not in out[:i]]


def canonical_edital_pagina(row: dict[str, str], url: str) -> str:
    url = normalize_url(url)
    banca = (row.get("banca") or "").lower()
    if "fundatec" not in banca or not url:
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    concurso_id = (qs.get("concurso") or [""])[0]
    if concurso_id and parsed.netloc.endswith("fundatec.org.br"):
        return f"https://www.fundatec.org.br/portal/concursos/pagina_editais.php?concurso={concurso_id}"
    return url


def identity_terms(value: str) -> set[str]:
    text = match_text(value or "")
    stop = {
        "prefeitura", "municipal", "municipio", "camara", "vereadores",
        "conselho", "regional", "estado", "rio", "grande", "sul",
        "fundacao", "instituto", "secretaria", "universidade", "federal",
        "do", "da", "de", "dos", "das", "e", "rs",
    }
    return {token for token in re.findall(r"[a-z0-9]{3,}", text) if token not in stop}


def doc_matches_identity(row: dict[str, str], doc: DocEvidence | None) -> bool:
    if not doc:
        return False
    banca = match_text(row.get("banca", ""))
    current_page = normalize_url(row.get("edital_pagina", ""))
    current_pdf = normalize_url(row.get("edital_pdf", ""))
    doc_page = normalize_url(doc.source_page)
    doc_url = normalize_url(doc.url)
    if doc_page and current_page and url_key(doc_page) == url_key(current_page):
        return True
    if doc_url and current_pdf and url_key(doc_url) == url_key(current_pdf):
        return True
    if "fundatec" in banca:
        row_id = (parse_qs(urlparse(current_page).query).get("concurso") or [""])[0]
        doc_page_id = (parse_qs(urlparse(doc_page).query).get("concurso") or [""])[0]
        if row_id and doc_page_id == row_id:
            return True
    if "faurgs" in banca:
        row_num = normalize_numero(row.get("numero", ""))
        doc_num = extract_numero_from_text(" ".join([doc.anchor, doc.text_prefix, doc.url]))
        if row_num and doc_num and row_num == doc_num:
            return True
    blob = match_text(" ".join([doc.anchor or "", doc.text_prefix or "", doc.url or ""]))
    municipio = match_text(row.get("municipio", ""))
    if municipio and municipio != "estatal":
        city_terms = identity_terms(municipio)
        if city_terms and any(term in blob for term in city_terms):
            return True
    org_terms = identity_terms(row.get("orgao", ""))
    if len(org_terms) >= 2 and sum(1 for term in org_terms if term in blob) >= 2:
        return True
    if municipio == "estatal":
        state_terms = {"rio", "grande", "sul"}
        if sum(1 for term in state_terms if term in blob) >= 2:
            return True
    return False


def should_follow_link(crawler: Any, current_url: str, link: Any, row: dict[str, str]) -> bool:
    url = normalize_url(getattr(link, "url", ""))
    if not url or crawler.is_document_url(url):
        return False
    if is_too_broad_page(url):
        return False
    if not same_host(current_url, url):
        return False
    if "portalfaurgs.com.br" in urlparse(current_url).netloc.lower():
        low_path = urlparse(url).path.lower().rstrip("/")
        if low_path in {
            "/concursosfaurgs/emandamento",
            "/concursosfaurgs/encerrados",
            "/concursosfaurgs/copiasdeprovas",
            "/concursosfaurgs/falecomconcursos",
        }:
            return False
    if "fundatec.org.br" in urlparse(current_url).netloc.lower():
        current_id = (parse_qs(urlparse(current_url).query).get("concurso") or [""])[0]
        link_id = (parse_qs(urlparse(url).query).get("concurso") or [""])[0]
        if current_id and link_id != current_id:
            return False
    blob = crawler.norm(" ".join([getattr(link, "text", ""), url]))
    wanted = (
        "edital",
        "abertura",
        "arquivo",
        "documento",
        "publicacao",
        "publicacoes",
        "aviso",
        "download",
        "concurso",
        "processo seletivo",
    )
    if any(token in blob for token in wanted):
        return True
    municipio = crawler.norm(row.get("municipio", ""))
    orgao = crawler.norm(row.get("orgao", ""))
    if municipio and municipio in blob:
        return True
    if orgao and len(orgao) > 12 and orgao[:40] in blob:
        return True
    return False


def collect_evidence(row: dict[str, str], crawler: Any, crawler_args: argparse.Namespace, args: argparse.Namespace) -> tuple[list[PageEvidence], list[DocEvidence]]:
    page_urls = related_pages(row)
    pages: list[PageEvidence] = []
    docs_by_url: dict[str, DocEvidence] = {}
    seen_pages: set[str] = set()
    queue = list(page_urls)

    while queue and len(seen_pages) < args.max_pages_per_row:
        page_url = normalize_url(queue.pop(0))
        if not page_url or page_url in seen_pages or is_too_broad_page(page_url):
            continue
        seen_pages.add(page_url)
        bank = row.get("banca", "")
        status, raw, final_url = crawler.fetch(page_url, crawler_args, bank)
        final_url = normalize_url(final_url or page_url)
        if status <= 0 or not raw:
            debug(args, f"    PAGE_FAIL {status} {page_url}")
            continue

        links = crawler.enrich_links_with_context(raw, crawler.extract_links(raw, final_url))
        page = PageEvidence(
            url=final_url,
            status=status,
            title=crawler.page_title(raw),
            heading=crawler.meaningful_heading(raw),
            text=clean_text_for_prompt(crawler.strip_tags(raw), args.page_text_chars),
            links=links,
        )
        pages.append(page)
        debug(args, f"    PAGE {status} links={len(links)} {final_url}")
        debug(args, f"      PAGE_HINT title={page.title[:90] or '-'} heading={page.heading[:90] or '-'}")

        for link in links:
            link_url = normalize_url(getattr(link, "url", ""))
            if not link_url:
                continue
            if (
                crawler.is_document_url(link_url)
                or "edital" in crawler.norm(getattr(link, "text", "") + " " + link_url)
                or looks_like_site_specific_doc(crawler, final_url, link)
            ):
                priority_edital = is_priority_edital_link(crawler, link)
                site_specific_doc = looks_like_site_specific_doc(crawler, final_url, link)
                if link_url not in docs_by_url and (len(docs_by_url) < args.max_raw_docs_per_row or priority_edital or site_specific_doc):
                    text, bad_text = crawler.doc_text_for_scoring(link)
                    docs_by_url[link_url] = DocEvidence(
                        url=link_url,
                        source_page=final_url,
                        anchor=clean_text_for_prompt(getattr(link, "text", ""), 350),
                        score=crawler.doc_score(link),
                        opening_signal=crawler.has_opening_doc_signal(text),
                        accessory_signal=crawler.has_accessory_doc_signal(bad_text),
                        text_prefix="",
                    )
                    doc = docs_by_url[link_url]
                    if len(docs_by_url) <= args.debug_doc_limit:
                        debug(
                            args,
                            "      DOC_CANDIDATE "
                            f"score={doc.score} opening={doc.opening_signal} accessory={doc.accessory_signal} "
                            f"anchor={doc.anchor[:90] or '-'} url={compact_url(link_url)}",
                        )
            elif should_follow_link(crawler, final_url, link, row) and len(seen_pages) + len(queue) < args.max_pages_per_row:
                queue.append(link_url)
                debug(args, f"      FOLLOW_LINK anchor={clean_text_for_prompt(getattr(link, 'text', ''), 80) or '-'} url={compact_url(link_url)}")

    current_pdf = normalize_url(row.get("edital_pdf", ""))
    if current_pdf and current_pdf not in docs_by_url:
        link = crawler.Link(current_pdf, "current edital_pdf")
        text, bad_text = crawler.doc_text_for_scoring(link)
        docs_by_url[current_pdf] = DocEvidence(
            url=current_pdf,
            source_page=normalize_url(row.get("edital_pagina", "")),
            anchor="current edital_pdf",
            score=crawler.doc_score(link),
            opening_signal=crawler.has_opening_doc_signal(text),
            accessory_signal=crawler.has_accessory_doc_signal(bad_text),
            text_prefix="",
        )

    current_pdf_norm = normalize_url(row.get("edital_pdf", ""))
    current_page_norm = normalize_url(row.get("edital_pagina", ""))
    docs = sorted(docs_by_url.values(), key=lambda doc: doc_rank(doc, current_pdf_norm, current_page_norm), reverse=True)
    pdf_text_count = 0
    shortlisted_docs = docs[: args.max_docs_per_row]
    if current_pdf_norm:
        current_doc = docs_by_url.get(current_pdf_norm)
        if current_doc and all(normalize_url(doc.url) != current_pdf_norm for doc in shortlisted_docs):
            shortlisted_docs = (shortlisted_docs + [current_doc])[: args.max_docs_per_row - 1] + [current_doc]
    for doc in shortlisted_docs:
        if pdf_text_count >= args.max_pdf_texts_per_row and normalize_url(doc.url) != current_pdf_norm:
            continue
        if crawler.is_document_url(doc.url):
            debug(args, f"    PDF_TEXT reading score={doc.score} opening={doc.opening_signal} anchor={doc.anchor[:80] or '-'} url={compact_url(doc.url)}")
            pdf_text = crawler.extract_pdf_text_prefix(doc.url, crawler_args, max_pages=args.pdf_pages)
            doc.text_prefix = clean_text_for_prompt(pdf_text, args.pdf_text_chars)
            pdf_text_count += 1
    debug(args, f"    DOC_SCAN raw={len(docs_by_url)} shortlist={len(shortlisted_docs)}")
    return pages, shortlisted_docs


def build_prompt(row: dict[str, str], pages: list[PageEvidence], docs: list[DocEvidence]) -> list[dict[str, str]]:
    allowed_pages = [page.url for page in pages]
    allowed_docs = [doc.url for doc in docs]
    evidence = {
        "row_current": {key: row.get(key, "") for key in BASE_FIELDS},
        "allowed_edital_pagina_urls": allowed_pages,
        "allowed_edital_pdf_urls": allowed_docs,
        "pages": [
            {
                "id": idx,
                "url": page.url,
                "status": page.status,
                "title": page.title,
                "heading": page.heading,
                "text_hint": page.text,
            }
            for idx, page in enumerate(pages)
        ],
        "documents": [
            {
                "id": idx,
                "url": doc.url,
                "source_page": doc.source_page,
                "anchor": doc.anchor,
                "deterministic_score": doc.score,
                "deterministic_opening_signal": doc.opening_signal,
                "deterministic_accessory_signal": doc.accessory_signal,
                "text_prefix": doc.text_prefix,
            }
            for idx, doc in enumerate(docs)
        ],
    }
    system = (
        "You are an auditor for Brazilian public contest datasets. "
        "You verify official RS concursos publicos and processos seletivos. "
        "Return strict JSON only. Never invent URLs. Use only the allowed URLs in the evidence. "
        "Crawler deterministic signals are hints only and may be wrong; reason from page titles, anchors, dates, and PDF text. "
        "If the correct edital de abertura or equivalent base edital is not present, mark decision as revisar or no_encontrado."
    )
    user = (
        "Revise esta fila. Confirma si el orgao, municipio, tipo, numero, edital_pagina y edital_pdf hacen sentido. "
        "El edital_pdf correcto debe ser edital de abertura o documento equivalente de abertura/inscricoes. "
        "TambiÃ©n cuenta como base edital: 'Edital PSS nÂº X/AAAA', 'Edital nÂº X/AAAA - Edital - Concurso PÃºblico', 'Edital nÂº 1 (abertura)', o el primer documento oficial 'Edital' del certame cuando los documentos posteriores son atos/retificaÃ§Ãµes/resultados. "
        "No rechaces un PDF solo porque la URL contiene /anexos/; muchas bancas guardan todos los PDFs ahÃ­. "
        "No aceptes cronograma, resultado, homologacao, gabarito, convocacao, anexo accesorio, adendo o retificacao como base si hay un edital principal. "
        "Si hay una pagina menos profunda o hermana con boton de edital de abertura, escoge esa pagina como edital_pagina y su PDF como edital_pdf, pero solo si la URL aparece en allowed_edital_pagina_urls/allowed_edital_pdf_urls. "
        "Si corriges la fila y el edital_pdf propuesto es base edital valido, usa decision='listo' y confidence >= 0.70. "
        "Si la fila ya esta marcada revisar pero la evidencia muestra que los links y datos actuales son correctos, no cambies URLs ni campos; solo usa decision='listo'. "
        "Normaliza numero como 'n\\u00ba 01/2026'. Usa municipio='Estatal' para orgaos estaduais/regionais de RS. "
        "MantÃ©n issues y evidence muy cortos; evidence debe tener maximo 120 caracteres. "
        "Devuelve exactamente este JSON:\n"
        "{"
        "\"decision\":\"listo|revisar|no_encontrado\","
        "\"confidence\":0.0,"
        "\"tipo\":\"concurso_publico|processo_seletivo\","
        "\"orgao\":\"\","
        "\"municipio\":\"\","
        "\"uf\":\"RS\","
        "\"numero\":\"\","
        "\"edital_pagina\":\"\","
        "\"edital_pdf\":\"\","
        "\"document_class\":\"edital_abertura|retificacao|cronograma|resultado|homologacao|convocacao|anexo|outro|sem_pdf\","
        "\"issues\":[],"
        "\"evidence\":\"frase corta max 120 caracteres\""
        "}\n\n"
        f"EVIDENCE_JSON:\n{json.dumps(evidence, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ollama_chat(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload = {
        "model": args.model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "keep_alive": "15m",
        "options": {
            "temperature": 0,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
        },
    }
    res = requests.post(args.ollama_url.rstrip("/") + "/api/chat", json=payload, timeout=args.ollama_timeout)
    res.raise_for_status()
    res_json = res.json()
    content = res_json.get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if isinstance(parsed, dict):
        parsed["_ollama_metrics"] = {
            "total_s": round(float(res_json.get("total_duration") or 0) / 1_000_000_000, 3),
            "load_s": round(float(res_json.get("load_duration") or 0) / 1_000_000_000, 3),
            "prompt_eval_s": round(float(res_json.get("prompt_eval_duration") or 0) / 1_000_000_000, 3),
            "prompt_tokens": res_json.get("prompt_eval_count"),
            "eval_s": round(float(res_json.get("eval_duration") or 0) / 1_000_000_000, 3),
            "eval_tokens": res_json.get("eval_count"),
        }
    return parsed


def openai_compatible_chat(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
    api_key = args.openai_api_key or os.environ.get(args.openai_api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing API key. Set ${args.openai_api_key_env} or pass --openai-api-key.")
    if not args.openai_base_url:
        raise RuntimeError("Missing --openai-base-url, e.g. https://api.runpod.ai/v2/ENDPOINT_ID/openai/v1")

    payload: dict[str, Any] = {
        "model": args.openai_model or args.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": args.num_predict,
    }
    if args.openai_json_mode:
        payload["response_format"] = {"type": "json_object"}

    started = time.time()
    res = requests.post(
        args.openai_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=args.openai_timeout,
    )
    res.raise_for_status()
    res_json = res.json()
    content = res_json.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if isinstance(parsed, dict):
        parsed["_openai_metrics"] = {
            "total_s": round(time.time() - started, 3),
            "prompt_tokens": (res_json.get("usage") or {}).get("prompt_tokens"),
            "completion_tokens": (res_json.get("usage") or {}).get("completion_tokens"),
            "total_tokens": (res_json.get("usage") or {}).get("total_tokens"),
        }
    return parsed


def gemini_chat(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
    api_key = args.gemini_api_key or os.environ.get(args.gemini_api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing API key. Set ${args.gemini_api_key_env} or pass --gemini-api-key.")

    system_parts: list[str] = []
    user_parts: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)

    payload: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "\n\n".join(user_parts)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": args.num_predict,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    model = args.gemini_model or args.model
    started = time.time()
    endpoint = args.gemini_base_url.rstrip("/") + f"/models/{model}:generateContent"
    res: requests.Response | None = None
    for attempt in range(4):
        res = requests.post(
            endpoint,
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=args.gemini_timeout,
        )
        if res.status_code not in {429, 503}:
            break
        time.sleep(4 * (attempt + 1))
    if res is None:
        raise RuntimeError("Gemini request did not run.")
    if res.status_code >= 400:
        detail = ""
        try:
            detail = json.dumps(res.json().get("error") or {}, ensure_ascii=False)[:500]
        except Exception:
            detail = res.text[:500]
        raise RuntimeError(f"Gemini HTTP {res.status_code}: {detail}")
    res_json = res.json()
    candidates = res_json.get("candidates") or []
    content = ""
    if candidates:
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        content = "\n".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    parsed = parse_json_object(content, "Gemini")
    if isinstance(parsed, dict):
        usage = res_json.get("usageMetadata") or {}
        parsed["_openai_metrics"] = {
            "provider": "gemini",
            "total_s": round(time.time() - started, 3),
            "prompt_tokens": usage.get("promptTokenCount"),
            "completion_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        }
    return parsed


def runpod_extract_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if isinstance(output.get("text"), str):
            return output["text"]
        if isinstance(output.get("content"), str):
            return output["content"]
        choices = output.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        return message["content"]
                    if isinstance(choice.get("text"), str):
                        return choice["text"]
                    tokens = choice.get("tokens")
                    if isinstance(tokens, list):
                        return "".join(str(token) for token in tokens)
    if isinstance(output, list):
        parts = [runpod_extract_text(item) for item in output]
        return "\n".join(part for part in parts if part)
    return str(output or "")


def parse_json_object(content: str, provider: str) -> dict[str, Any]:
    text = (content or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
        if fenced:
            text = fenced.group(1).strip()
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise RuntimeError(f"{provider} returned non-JSON content: {content[:500]}")
            text = text[start : end + 1]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{provider} returned malformed JSON: {text[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{provider} returned JSON {type(parsed).__name__}, expected object.")
    return parsed


def runpod_endpoint_snapshot(api_key: str, endpoint: str) -> str:
    try:
        res = requests.get(
            "https://rest.runpod.io/v1/endpoints?includeWorkers=true",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        res.raise_for_status()
        endpoints = res.json()
        if not isinstance(endpoints, list):
            endpoints = [endpoints]
        current = next((item for item in endpoints if item.get("id") == endpoint), None)
        if not current:
            return "endpoint_snapshot=not_found"
        workers = current.get("workers") or []
        worker_bits = []
        for worker in workers:
            if isinstance(worker, dict):
                worker_bits.append(
                    f"{worker.get('id','?')}:{worker.get('desiredStatus','?')}:"
                    f"${worker.get('costPerHr','?')}/hr"
                )
        return (
            f"endpoint workersMin={current.get('workersMin')} workersMax={current.get('workersMax')} "
            f"idleTimeout={current.get('idleTimeout')} workers={len(workers)} "
            f"worker_states={','.join(worker_bits) or '-'}"
        )
    except Exception as exc:
        return f"endpoint_snapshot_error={type(exc).__name__}:{str(exc)[:120]}"


def runpod_queue_chat(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
    api_key = args.openai_api_key or os.environ.get(args.openai_api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing API key. Set ${args.openai_api_key_env} or pass --openai-api-key.")
    if not args.runpod_endpoint_id:
        raise RuntimeError("Missing --runpod-endpoint-id.")

    endpoint = args.runpod_endpoint_id.strip()
    base_url = f"https://api.runpod.ai/v2/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "input": {
            "messages": messages,
            "sampling_params": {
                "temperature": 0,
                "max_tokens": args.num_predict,
            },
        }
    }

    started = time.time()
    next_endpoint_debug = 0.0
    if args.runpod_endpoint_debug_interval > 0:
        debug(args, "  RUNPOD_ENDPOINT " + runpod_endpoint_snapshot(api_key, endpoint))
        next_endpoint_debug = time.time() + args.runpod_endpoint_debug_interval
    submit = requests.post(base_url + "/run", headers=headers, json=payload, timeout=args.openai_timeout)
    submit.raise_for_status()
    job = submit.json()
    job_id = job.get("id")
    if not job_id:
        raise RuntimeError(f"RunPod did not return job id: {job}")

    debug(args, f"  RUNPOD_JOB id={job_id} status={job.get('status')}")
    deadline = time.time() + args.runpod_timeout
    queue_deadline = time.time() + args.runpod_queue_timeout if args.runpod_queue_timeout > 0 else 0.0
    last_status = ""
    status_json: dict[str, Any] = {}
    while time.time() < deadline:
        status_res = requests.get(base_url + f"/status/{job_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=args.openai_timeout)
        status_res.raise_for_status()
        status_json = status_res.json()
        status = str(status_json.get("status") or "")
        if status != last_status:
            debug(
                args,
                f"  RUNPOD_STATUS id={job_id} status={status} "
                f"delay_ms={status_json.get('delayTime') or '-'} exec_ms={status_json.get('executionTime') or '-'}",
            )
            last_status = status
        if args.runpod_endpoint_debug_interval > 0 and time.time() >= next_endpoint_debug:
            debug(args, "  RUNPOD_ENDPOINT " + runpod_endpoint_snapshot(api_key, endpoint))
            next_endpoint_debug = time.time() + args.runpod_endpoint_debug_interval
        if status == "COMPLETED":
            break
        if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            raise RuntimeError(f"RunPod job {status}: {status_json.get('error') or status_json}")
        if queue_deadline and status == "IN_QUEUE" and time.time() >= queue_deadline:
            try:
                requests.post(base_url + f"/cancel/{job_id}", headers=headers, timeout=args.openai_timeout)
                debug(args, f"  RUNPOD_CANCEL id={job_id} reason=queue_timeout")
            except Exception as cancel_exc:
                debug(args, f"  RUNPOD_CANCEL_ERROR id={job_id} {type(cancel_exc).__name__}:{str(cancel_exc)[:120]}")
            raise TimeoutError(f"RunPod job stayed in queue after {args.runpod_queue_timeout}s: {job_id}")
        time.sleep(args.runpod_poll_interval)
    else:
        try:
            requests.post(base_url + f"/cancel/{job_id}", headers=headers, timeout=args.openai_timeout)
            debug(args, f"  RUNPOD_CANCEL id={job_id} reason=timeout")
        except Exception as cancel_exc:
            debug(args, f"  RUNPOD_CANCEL_ERROR id={job_id} {type(cancel_exc).__name__}:{str(cancel_exc)[:120]}")
        raise TimeoutError(f"RunPod job timed out after {args.runpod_timeout}s: {job_id}")

    content = runpod_extract_text(status_json.get("output"))
    parsed = parse_json_object(content, "RunPod")
    if isinstance(parsed, dict):
        if args.runpod_endpoint_debug_interval > 0:
            debug(args, "  RUNPOD_ENDPOINT " + runpod_endpoint_snapshot(api_key, endpoint))
        parsed["_openai_metrics"] = {
            "provider": "runpod-queue",
            "total_s": round(time.time() - started, 3),
            "delay_ms": status_json.get("delayTime"),
            "execution_ms": status_json.get("executionTime"),
            "worker_id": status_json.get("workerId"),
        }
    return parsed


def llm_chat(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
    if args.llm_provider == "ollama":
        return ollama_chat(args, messages)
    if args.llm_provider == "openai":
        return openai_compatible_chat(args, messages)
    if args.llm_provider == "gemini":
        return gemini_chat(args, messages)
    if args.llm_provider == "runpod-queue":
        return runpod_queue_chat(args, messages)
    raise RuntimeError(f"Unsupported provider: {args.llm_provider}")


def validate_proposal(row: dict[str, str], proposal: dict[str, Any], pages: list[PageEvidence], docs: list[DocEvidence], crawler: Any) -> tuple[dict[str, str], str, list[str]]:
    allowed_pages = {normalize_url(page.url) for page in pages}
    allowed_docs = {normalize_url(doc.url): doc for doc in docs}
    applied = dict(row)
    issues: list[str] = []
    validation: list[str] = []

    decision = str(proposal.get("decision") or "revisar").strip()
    if decision not in {"listo", "revisar", "no_encontrado"}:
        decision = "revisar"
        issues.append("invalid_decision_normalized")

    for field in ("tipo", "orgao", "municipio", "uf", "numero"):
        value = str(proposal.get(field) or "").strip()
        if value:
            applied[field] = value

    proposed_page = normalize_url(str(proposal.get("edital_pagina") or ""))
    if proposed_page:
        if proposed_page in allowed_pages or proposed_page == normalize_url(row.get("edital_pagina", "")):
            applied["edital_pagina"] = canonical_edital_pagina(row, proposed_page)
        else:
            issues.append("rejected_invented_edital_pagina")
    elif applied.get("edital_pagina"):
        applied["edital_pagina"] = canonical_edital_pagina(row, applied["edital_pagina"])

    proposed_pdf = normalize_url(str(proposal.get("edital_pdf") or ""))
    if proposed_pdf:
        if proposed_pdf in allowed_docs or proposed_pdf == normalize_url(row.get("edital_pdf", "")):
            applied["edital_pdf"] = proposed_pdf
        else:
            issues.append("rejected_invented_edital_pdf")
    elif decision == "no_encontrado":
        applied["edital_pdf"] = ""

    doc = allowed_docs.get(normalize_url(applied.get("edital_pdf", "")))
    doc_class = str(proposal.get("document_class") or "").strip()
    confidence = safe_float(proposal.get("confidence"), 0.0)
    proposal_issues = ";".join(str(issue) for issue in (proposal.get("issues") or []))
    page_known_bad = bool(re.search(r"edital_pagina\s+is\s+404|page\s+404|pagina\s+404|pÃ¡gina\s+404", proposal_issues, flags=re.I))
    deterministic_opening_doc = bool(doc and deterministic_base_label(doc))
    strong_opening_doc = bool(doc and ((doc.opening_signal and not doc.accessory_signal) or deterministic_opening_doc))

    if doc and strong_opening_doc:
        number_from_doc = extract_numero_from_text(" ".join([doc.text_prefix, doc.anchor, doc.url]))
        if number_from_doc and normalize_numero(applied.get("numero", "")) != number_from_doc:
            applied["numero"] = number_from_doc
            validation.append("numero_from_opening_pdf")

    if decision == "revisar" and doc_class == "edital_abertura" and strong_opening_doc and applied.get("numero") and not page_known_bad:
        decision = "listo"
        validation.append("promoted_by_verified_opening_pdf")
    elif doc and strong_opening_doc and applied.get("numero") and not page_known_bad:
        decision = "listo"
        doc_class = "edital_abertura"
        validation.append("promoted_by_validator_opening_pdf")

    if applied.get("numero"):
        applied["numero"] = normalize_numero(applied["numero"])

    if decision == "listo":
        doc_num = extract_numero_from_text(" ".join([doc.text_prefix, doc.anchor, doc.url])) if doc else ""
        if doc_num and normalize_numero(applied.get("numero", "")) != doc_num and strong_opening_doc:
            applied["numero"] = doc_num
            validation.append("numero_corrected_from_pdf_before_strict_check")
        if page_known_bad:
            decision = "revisar"
            issues.append("listo_with_dead_edital_pagina_downgraded")
        if not applied.get("edital_pagina"):
            decision = "revisar"
            issues.append("listo_without_edital_pagina_downgraded")
        elif normalize_url(applied.get("edital_pagina", "")) not in allowed_pages and normalize_url(applied.get("edital_pagina", "")) != normalize_url(row.get("edital_pagina", "")):
            decision = "revisar"
            issues.append("listo_with_unverified_edital_pagina_downgraded")
        if not applied.get("edital_pdf"):
            decision = "revisar"
            issues.append("listo_without_pdf_downgraded")
        elif not doc:
            decision = "revisar"
            issues.append("listo_with_unverified_edital_pdf_downgraded")
        if not applied.get("numero"):
            decision = "revisar"
            issues.append("listo_without_numero_downgraded")
        elif not doc_num and not strong_opening_doc:
            decision = "revisar"
            issues.append("listo_without_numero_in_pdf_downgraded")
        elif doc_num and normalize_numero(applied.get("numero", "")) != doc_num:
            decision = "revisar"
            issues.append("listo_numero_mismatch_pdf_downgraded")
        if doc_class not in {"edital_abertura"} and not strong_opening_doc:
            decision = "revisar"
            issues.append("listo_without_ai_opening_class_downgraded")
        if doc and doc.accessory_signal and not doc.opening_signal and not deterministic_opening_doc:
            decision = "revisar"
            issues.append("listo_pdf_accessory_signal_downgraded")
        if doc and not doc_matches_identity(applied, doc):
            decision = "revisar"
            issues.append("listo_identity_mismatch_pdf_downgraded")
        if confidence < 0.70 and not strong_opening_doc:
            decision = "revisar"
            issues.append("low_confidence_downgraded")

    if not applied.get("uf"):
        applied["uf"] = "RS"
    applied["semaforo"] = decision
    validation.append("urls_restricted_to_evidence")
    if doc:
        validation.append(f"doc_score={doc.score}")
    return applied, ";".join(validation), issues


def extract_numero_from_text(value: str) -> str:
    text = match_text(value or "")
    patterns = [
        r"edital\s+(?:de\s+)?concurso\s+publico\s+n?\S{0,4}\s*([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
        r"concurso\s+publico\s*(?:[-:â€¢]\s*)?(?:edital\s*)?([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
        r"edital\s+([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
        r"edital\s+(?:de\s+abertura\s+)?(?:do\s+concurso\s+p[uÃº]blico\s+)?(?:n[ÂºoÂ°.]*)\s*([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
        r"concurso\s+p[uÃº]blico\s+(?:n[ÂºoÂ°.]*)\s*([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
        r"processo\s+seletivo\s+(?:simplificado\s+)?(?:n[ÂºoÂ°.]*)\s*([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
        r"(?:n[ÂºoÂ°.]*)\s*([0-9]{1,4})\s*/\s*((?:20)?[0-9]{2})",
    ]
    lowered = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered, flags=re.I)
        if match:
            seq = match.group(1).zfill(2)
            year = match.group(2)
            if len(year) == 2:
                year = "20" + year
            return normalize_numero(f"nÂº {seq}/{year}")
    return ""


def normalize_numero(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    prefix = "n" + chr(186)
    value = (
        value.replace("nÃƒâ€šÃ‚Âº", prefix)
        .replace("nÃ‚Âº", prefix)
        .replace("NÃ‚Âº", prefix)
        .replace("NÂº", prefix)
        .replace("NÃ‚Â°", prefix)
        .replace("NÂ°", prefix)
        .replace("nÃ‚Â°", prefix)
        .replace("nÂ°", prefix)
    )
    match = re.search(r"(\d{1,4})\s*[/.-]\s*((?:20)?\d{2})", value)
    if not match:
        return value
    number = match.group(1).lstrip("0") or "0"
    year = match.group(2)
    if len(year) == 2:
        year = "20" + year
    return f"{prefix} {int(number):02d}/{year}" if int(number) < 100 else f"{prefix} {number}/{year}"


def safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def row_filter(row: dict[str, str], args: argparse.Namespace) -> bool:
    mode = args.only
    if mode == "all":
        return True
    if mode == "revisar":
        return row.get("semaforo") in {"revisar", "no_encontrado"}
    if mode == "listo":
        return row.get("semaforo") == "listo"
    if mode == "suspicious":
        if row.get("semaforo") == "revisar":
            return True
        blob = " ".join([row.get("orgao", ""), row.get("numero", ""), row.get("edital_pdf", "")]).lower()
        return any(token in blob for token in ("cronograma", "homolog", "resultado", "convoca", "nominata", "pÃºblico", "publico"))
    return True


def cache_key(row: dict[str, str]) -> str:
    blob = "|".join(row.get(field, "") for field in BASE_FIELDS)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def active_model_label(args: argparse.Namespace) -> str:
    if args.llm_provider == "runpod-queue":
        return f"runpod:{args.openai_model or DEFAULT_OPENAI_MODEL}"
    if args.llm_provider == "openai":
        return f"openai:{args.openai_model or args.model}"
    if args.llm_provider == "gemini":
        return f"gemini:{args.gemini_model or args.model}"
    return f"ollama:{args.model}"


def safe_cache_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI audit/repair for RS banca layer rows.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "exports" / "bancas_base_rs_2020_2026_final.csv")
    parser.add_argument("--out-review", type=Path, default=PROJECT_ROOT / "data" / "exports" / "bancas_base_rs_2020_2026_ai_review.csv")
    parser.add_argument("--out-applied", type=Path, default=PROJECT_ROOT / "data" / "exports" / "bancas_base_rs_2020_2026_ai_applied.csv")
    parser.add_argument("--llm-provider", choices=["ollama", "openai", "gemini", "runpod-queue"], default="ollama")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--openai-base-url", default="", help="OpenAI-compatible base URL. For RunPod vLLM: https://api.runpod.ai/v2/ENDPOINT_ID/openai/v1")
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--openai-api-key-env", default="RUNPOD_API_KEY")
    parser.add_argument("--openai-api-key", default="")
    parser.add_argument("--openai-timeout", type=int, default=180)
    parser.add_argument("--openai-json-mode", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--gemini-base-url", default="https://generativelanguage.googleapis.com/v1beta")
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--gemini-api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--gemini-api-key", default="")
    parser.add_argument("--gemini-timeout", type=int, default=180)
    parser.add_argument("--runpod-endpoint-id", default="")
    parser.add_argument("--runpod-timeout", type=int, default=900)
    parser.add_argument("--runpod-queue-timeout", type=int, default=0)
    parser.add_argument("--runpod-poll-interval", type=float, default=2.0)
    parser.add_argument("--runpod-endpoint-debug-interval", type=float, default=0.0)
    parser.add_argument("--abort-on-ai-error", action="store_true")
    parser.add_argument("--only", choices=["all", "revisar", "listo", "suspicious"], default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--num-ctx", type=int, default=2048)
    parser.add_argument("--num-predict", type=int, default=260)
    parser.add_argument("--ollama-timeout", type=int, default=180)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "data" / "cache" / "ai_review")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--max-pages-per-row", type=int, default=4)
    parser.add_argument("--max-docs-per-row", type=int, default=5)
    parser.add_argument("--max-raw-docs-per-row", type=int, default=120)
    parser.add_argument("--max-pdf-texts-per-row", type=int, default=1)
    parser.add_argument("--page-text-chars", type=int, default=350)
    parser.add_argument("--pdf-text-chars", type=int, default=550)
    parser.add_argument("--pdf-pages", type=int, default=1)
    parser.add_argument("--debug-doc-limit", type=int, default=16)
    parser.add_argument("--delay", type=float, default=0.02)
    parser.add_argument("--host-delay", type=float, default=0.20)
    parser.add_argument("--lasalle-host-delay", type=float, default=8.0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-fetches-per-bank", type=int, default=1200)
    parser.add_argument("--lasalle-max-fetches", type=int, default=220)
    parser.add_argument("--no-ai", action="store_true", help="Collect evidence and write rows without calling Ollama.")
    parser.add_argument("--force-ai", action="store_true", help="Call the configured LLM even when deterministic evidence is sufficient.")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-fetch", action="store_true")
    args = parser.parse_args()

    rows = read_csv(args.input)
    crawler = load_crawler()
    crawler_args = make_crawler_args(args)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    review_rows: list[dict[str, str]] = []
    applied_rows: list[dict[str, str]] = []
    processed = 0
    selected_index = 0

    for row_num, row in enumerate(rows, start=1):
        if not row_filter(row, args):
            applied_rows.append(dict(row))
            continue
        selected_index += 1
        if selected_index <= args.start:
            applied_rows.append(dict(row))
            continue
        if args.limit and processed >= args.limit:
            applied_rows.append(dict(row))
            continue

        processed += 1
        print(
            f"AI_REVIEW row={row_num} selected={processed} ano={row.get('ano')} semaforo={row.get('semaforo')} "
            f"banca={row.get('banca')} municipio={row.get('municipio')} numero={row.get('numero') or '-'} "
            f"orgao={row.get('orgao')[:90]}",
            flush=True,
        )
        debug(args, f"  VERIFY current_pagina={compact_url(row.get('edital_pagina', '')) or '-'}")
        debug(args, f"  VERIFY current_pdf={compact_url(row.get('edital_pdf', '')) or '-'}")

        started = time.time()
        pages, docs = collect_evidence(row, crawler, crawler_args, args)
        print(f"  EVIDENCE pages={len(pages)} docs={len(docs)} {time.time() - started:.1f}s", flush=True)
        for idx, doc in enumerate(docs[:5], start=1):
            debug(
                args,
                f"  EVIDENCE_DOC#{idx} score={doc.score} opening={doc.opening_signal} accessory={doc.accessory_signal} "
                f"anchor={doc.anchor[:90] or '-'} url={compact_url(doc.url)}",
            )

        proposal: dict[str, Any]
        raw_ai = ""
        deterministic = deterministic_proposal(row, pages, docs)
        if deterministic and not args.force_ai:
            proposal = deterministic
            print(
                f"  DETERMINISTIC_DONE decision={proposal.get('decision')} "
                f"numero={proposal.get('numero') or '-'} pdf={compact_url(str(proposal.get('edital_pdf') or ''))}",
                flush=True,
            )
        elif args.no_ai:
            proposal = {
                "decision": row.get("semaforo") or "revisar",
                "confidence": 0,
                "tipo": row.get("tipo", ""),
                "orgao": row.get("orgao", ""),
                "municipio": row.get("municipio", ""),
                "uf": row.get("uf", "RS"),
                "numero": row.get("numero", ""),
                "edital_pagina": row.get("edital_pagina", ""),
                "edital_pdf": row.get("edital_pdf", ""),
                "document_class": "sem_pdf" if not row.get("edital_pdf") else "outro",
                "issues": ["no_ai_mode"],
                "evidence": "",
            }
        else:
            if deterministic and args.force_ai:
                debug(args, "  DETERMINISTIC_AVAILABLE skipped_by_force_ai")
            cache_path = args.cache_dir / f"{cache_key(row)}.{safe_cache_label(active_model_label(args))}.json"
            if cache_path.exists() and not args.refresh_cache:
                proposal = json.loads(cache_path.read_text(encoding="utf-8"))
                print("  AI_CACHE hit", flush=True)
            else:
                ai_started = time.time()
                messages = build_prompt(row, pages, docs)
                debug(args, f"  AI_PROMPT chars={sum(len(message['content']) for message in messages)}")
                try:
                    proposal = llm_chat(args, messages)
                    cache_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
                    metrics = proposal.get("_ollama_metrics") or proposal.get("_openai_metrics") or {}
                    print(
                        f"  AI_DONE decision={proposal.get('decision')} confidence={proposal.get('confidence')} "
                        f"{time.time() - ai_started:.1f}s metrics={metrics}",
                        flush=True,
                    )
                except Exception as exc:
                    if args.abort_on_ai_error:
                        print(f"  AI_ERROR_ABORT {type(exc).__name__}: {str(exc)[:220]} {time.time() - ai_started:.1f}s", flush=True)
                        raise
                    if deterministic:
                        proposal = dict(deterministic)
                        proposal.setdefault("issues", [])
                        proposal["issues"] = list(proposal.get("issues") or []) + [f"ai_unavailable_fallback:{type(exc).__name__}:{str(exc)[:160]}"]
                    else:
                        proposal = {
                            "decision": "revisar",
                            "confidence": 0,
                            "tipo": row.get("tipo", ""),
                            "orgao": row.get("orgao", ""),
                            "municipio": row.get("municipio", ""),
                            "uf": row.get("uf", "RS"),
                            "numero": row.get("numero", ""),
                            "edital_pagina": row.get("edital_pagina", ""),
                            "edital_pdf": row.get("edital_pdf", ""),
                            "document_class": "sem_pdf" if not row.get("edital_pdf") else "outro",
                            "issues": [f"ai_error:{type(exc).__name__}:{str(exc)[:220]}"],
                            "evidence": "",
                        }
                    print(f"  AI_ERROR {type(exc).__name__}: {str(exc)[:220]} {time.time() - ai_started:.1f}s", flush=True)
            raw_ai = json.dumps(proposal, ensure_ascii=False)
            debug(
                args,
                "  AI_PROPOSAL "
                f"decision={proposal.get('decision')} conf={proposal.get('confidence')} "
                f"class={proposal.get('document_class')} tipo={proposal.get('tipo')} "
                f"numero={proposal.get('numero') or '-'} orgao={(str(proposal.get('orgao') or '')[:90])}",
            )
            debug(args, f"  AI_EVIDENCE {str(proposal.get('evidence') or '')[:220] or '-'}")

        applied, validation, extra_issues = validate_proposal(row, proposal, pages, docs, crawler)
        changed = [field for field in BASE_FIELDS if str(applied.get(field, "")) != str(row.get(field, ""))]
        issues = list(proposal.get("issues") or [])
        issues.extend(extra_issues)

        audit = {
            **row,
            "ai_decision": str(proposal.get("decision") or ""),
            "ai_confidence": str(proposal.get("confidence") or ""),
            "ai_changed_fields": ",".join(changed),
            "ai_issues": ";".join(str(item) for item in issues if item),
            "ai_evidence": str(proposal.get("evidence") or "")[:900],
            "ai_validation": validation,
            "ai_model": active_model_label(args),
            "ai_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ai_raw_json": raw_ai,
        }
        review_rows.append(audit)
        applied.update({field: audit[field] for field in AI_FIELDS})
        applied_rows.append(applied)
        for diff in field_diffs(row, applied, BASE_FIELDS):
            debug(args, f"  CHANGE {diff}")
        print(f"  APPLY semaforo={applied.get('semaforo')} changed={','.join(changed) or '-'} issues={audit['ai_issues'] or '-'}", flush=True)

    # Preserve trailing rows after --limit that were skipped in the loop without audit columns.
    full_fields = BASE_FIELDS + AI_FIELDS
    review_fields = BASE_FIELDS + AI_FIELDS + ["ai_raw_json"]
    for row in applied_rows:
        for field in full_fields:
            row.setdefault(field, "")

    write_csv(args.out_review, review_rows, review_fields)
    write_csv(args.out_applied, applied_rows, full_fields)
    print(f"OUT_REVIEW {args.out_review}", flush=True)
    print(f"OUT_APPLIED {args.out_applied}", flush=True)
    print(f"PROCESSED {processed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
