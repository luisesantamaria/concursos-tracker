from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse


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


def load_crawler() -> Any:
    spec = importlib.util.spec_from_file_location("bancas_crawler", CRAWLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load crawler: {CRAWLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bancas_crawler"] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        return []
    if lines[0].startswith("sep="):
        delimiter = lines[0].split("=", 1)[1] or ";"
        lines = lines[1:]
    else:
        sample = "\n".join(lines[:5])
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [{k: (v or "") for k, v in row.items()} for row in csv.DictReader(lines, delimiter=delimiter)]


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        return url.strip()
    return urlunparse(parsed._replace(fragment=""))


def key_url(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return urlunparse(parsed._replace(query="", fragment="")).lower().rstrip("/")


def match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = value.replace("Âº", "o").replace("Â°", "o").replace("º", "o").replace("°", "o")
    return re.sub(r"\s+", " ", value).strip()


def compact(value: str, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value if len(value) <= limit else value[:limit] + "..."


def numero_parts(value: str) -> tuple[int, str] | None:
    text = match_text(value)
    match = re.search(r"(?:edital\s*)?(?:n[o.]*)\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})", text)
    if not match:
        match = re.search(r"\b([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})\b", text)
    if not match:
        return None
    seq = int(match.group(1).lstrip("0") or "0")
    year = match.group(2)
    if len(year) == 2:
        year = "20" + year
    return seq, year


def format_numero(seq: int, year: str) -> str:
    return f"n\u00ba {seq:02d}/{year}"


def normalize_numero(value: str) -> str:
    parts = numero_parts(value)
    if not parts:
        return ""
    return format_numero(parts[0], parts[1])


def same_numero(left: str, right: str) -> bool:
    left_parts = numero_parts(left)
    right_parts = numero_parts(right)
    return bool(left_parts and right_parts and left_parts == right_parts)


def extract_numero_with_context(value: str, ano: str = "") -> str:
    text = match_text(value)
    patterns = [
        r"(?:edital|processo seletivo|concurso publico|pss)[\w\s/.-]{0,90}?n[o.]*\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})",
        r"n[o.]*\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})[\w\s/.-]{0,90}?(?:edital|processo seletivo|concurso publico|pss)",
        r"edital\s+([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            year = match.group(2)
            if len(year) == 2:
                year = "20" + year
            if ano and year != str(ano):
                continue
            return format_numero(int(match.group(1).lstrip("0") or "0"), year)
    return ""


def extract_numero(value: str) -> str:
    text = match_text(value)
    patterns = [
        r"edital\s+(?:de\s+abertura\s+)?(?:do\s+concurso\s+publico\s+)?(?:n[o.]*)\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})",
        r"processo\s+seletivo\s+(?:simplificado\s+)?(?:n[o.]*)\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})",
        r"concurso\s+publico\s+(?:n[o.]*)\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})",
        r"\b(?:n[o.]*)\s*([0-9]{1,4})\s*[/.-]\s*((?:20)?[0-9]{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_numero(f"n\u00ba {match.group(1)}/{match.group(2)}")
    return ""


def extract_numero_for_audit(label: str, pdf_text: str, ano: str = "") -> str:
    """Extract likely edital numbers, not random law/article numbers in PDF bodies."""
    label_num = extract_numero_with_context(label, ano)
    if label_num:
        return label_num
    lines = [line.strip() for line in re.split(r"[\r\n]+", pdf_text or "") if line.strip()]
    head = "\n".join(lines[:18])
    head_num = extract_numero_with_context(head, ano)
    if head_num:
        return head_num
    return extract_numero_with_context((pdf_text or "")[:1200], ano)


def has_accessory_signal(label: str, body: str = "") -> bool:
    # Body text can mention cronograma, classificacao, recursos, etc. inside a
    # valid opening edital. Judge accessory status from the visible link/filename
    # first, and only use a short body prefix if the label is empty.
    text = match_text(label or "") or match_text((body or "")[:360])
    if re.search(r"edital\s+n[o.]?\s*\d{1,4}\s*[/.-]\s*20\d{2}.*(?:edital|concurso publico|processo seletivo|pss)", text):
        return False
    if re.search(r"(?:edital de abertura|edital pss|abertura e inscr|edital\s+[-:]\s*(?:concurso publico|processo seletivo))", text):
        return False
    # Do not penalize the CDN path segment "anexos"; only visible labels/text.
    bad = (
        "ato oficial",
        "retificacao",
        "retifica",
        "adendo",
        "cronograma",
        "resultado",
        "homolog",
        "gabarito",
        "convoca",
        "classific",
        "isencao",
        "comunicado",
        "impugnacao",
        "termo de aprovacao",
        "orientacoes gerais",
        "legislacao",
        "lei organica",
        "regime juridico",
        "plano de cargos",
        "codigo tributario",
    )
    return any(token in text for token in bad)


def has_base_edital_signal(label: str, body: str) -> bool:
    label_n = match_text(label)
    body_n = match_text(body)
    text = " ".join([label_n, body_n])
    strong_base = bool(
        "edital de abertura" in text
        or "edital abertura" in text
        or "abertura e inscr" in text
        or "edital pss" in text
        or re.search(r"processo seletivo simplificado\s+[-–]\s+edital\s+n[o.]?\s*\d{1,4}\s*/\s*20\d{2}", text)
        or re.search(r"edital\s+n[o.]?\s*\d{1,4}\s*[/.-]\s*20\d{2}\s*[-–]\s*edital\s*[-–]\s*concurso publico", text)
        or re.search(r"edital\s+n[o.]?\s*\d{1,4}\s*\(\s*abertura\s*\)", text)
        or re.search(r"edital\s*[-–:]\s*(?:concurso publico|processo seletivo)", text)
        or re.search(r"edital\s+n[o.]?\s*\d{1,4}\s*[/.-]\s*20\d{2}.*(?:concurso publico|processo seletivo|inscricoes|pgm)", text)
    )
    if has_accessory_signal(label_n) and not strong_base:
        return False
    return strong_base


def source_page_links_pdf(crawler: Any, raw: str, page_url: str, pdf_url: str) -> tuple[bool, str]:
    pdf_key = key_url(pdf_url)
    parsed_pdf = urlparse(pdf_url)
    pdf_tail = parsed_pdf.path.rsplit("/", 1)[-1].lower()
    links = crawler.enrich_links_with_context(raw, crawler.extract_links(raw, page_url))
    best_anchor = ""
    for link in links:
        link_url = normalize_url(getattr(link, "url", ""))
        if not link_url:
            continue
        if key_url(link_url) == pdf_key or (pdf_tail and urlparse(link_url).path.lower().endswith(pdf_tail)):
            best_anchor = getattr(link, "text", "")
            return True, best_anchor
    return False, best_anchor


def row_tokens(row: dict[str, str]) -> list[str]:
    ignore = {
        "prefeitura",
        "municipal",
        "municipio",
        "camara",
        "conselho",
        "regional",
        "estado",
        "rio",
        "grande",
        "sul",
        "rs",
        "publico",
        "processo",
        "seletivo",
    }
    raw = " ".join([row.get("orgao", ""), row.get("municipio", "")])
    tokens = []
    for token in re.findall(r"[a-z0-9]{4,}", match_text(raw)):
        if token not in ignore and token not in tokens:
            tokens.append(token)
    return tokens


def quick_audit_row(row: dict[str, str], crawler: Any, crawler_args: argparse.Namespace, args: argparse.Namespace) -> tuple[dict[str, str], list[str], list[str]]:
    out = dict(row)
    issues: list[str] = []
    validation: list[str] = []
    page_url = normalize_url(row.get("edital_pagina", ""))
    pdf_url = normalize_url(row.get("edital_pdf", ""))

    if not page_url:
        issues.append("missing_edital_pagina")
    if not pdf_url:
        issues.append("missing_edital_pdf")
    if issues:
        return out, issues, validation

    status, raw, final_url = crawler.fetch(page_url, crawler_args, row.get("banca", ""))
    final_url = normalize_url(final_url or page_url)
    validation.append(f"page_status={status}")
    if status < 200 or status >= 400 or not raw:
        issues.append(f"page_fetch_failed:{status}")
        return out, issues, validation

    linked, anchor = source_page_links_pdf(crawler, raw, final_url, pdf_url)
    if linked:
        validation.append("pdf_linked_from_page")
    else:
        validation.append("pdf_not_linked_from_page")

    pdf_text = ""
    if crawler.is_document_url(pdf_url):
        pdf_text = crawler.extract_pdf_text_prefix(pdf_url, crawler_args, max_pages=args.pdf_pages)
        validation.append(f"pdf_text_chars={len(pdf_text)}")
    if not pdf_text:
        issues.append("pdf_text_empty_or_unreadable")

    label = " ".join([anchor, unquote(pdf_url)])
    if has_accessory_signal(anchor, pdf_text) and not has_base_edital_signal(anchor, pdf_text):
        issues.append("pdf_looks_like_accessory_document")
    if not has_base_edital_signal(label, pdf_text):
        issues.append("pdf_lacks_base_edital_signal")

    expected_num = normalize_numero(row.get("numero", ""))
    found_num = extract_numero_for_audit(label, pdf_text, row.get("ano", ""))
    if found_num:
        validation.append(f"pdf_numero={found_num}")
        if expected_num and not same_numero(expected_num, found_num):
            issues.append(f"numero_mismatch_pdf:{expected_num}->{found_num}")
            out["numero"] = found_num

    evidence_text = match_text(" ".join([crawler.page_title(raw), crawler.meaningful_heading(raw), crawler.strip_tags(raw)[:1200], pdf_text[:1200]]))
    if row.get("municipio", "").lower() != "estatal":
        municipio = match_text(row.get("municipio", ""))
        if municipio and municipio not in evidence_text:
            issues.append("municipio_not_seen_in_page_or_pdf")
    tokens = row_tokens(row)
    if tokens and not any(token in evidence_text for token in tokens[:6]):
        issues.append("orgao_tokens_not_seen_in_page_or_pdf")

    if not linked and not pdf_text:
        issues.append("pdf_not_verified_against_page")
    return out, issues, validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast deterministic audit for rows currently marked listo.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ano", default="")
    parser.add_argument("--only-semaforo", default="listo")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.02)
    parser.add_argument("--host-delay", type=float, default=0.05)
    parser.add_argument("--lasalle-host-delay", type=float, default=1.0)
    parser.add_argument("--max-fetches-per-bank", type=int, default=1200)
    parser.add_argument("--lasalle-max-fetches", type=int, default=220)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "authority_first" / "data" / "cache" / "quick_audit")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--pdf-pages", type=int, default=2)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    crawler = load_crawler()
    crawler_args = argparse.Namespace(
        debug=False,
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
    rows = read_csv(args.input)
    fields = list(dict.fromkeys(BASE_FIELDS + AI_FIELDS + [field for row in rows for field in row.keys()]))
    processed = 0
    flagged = 0
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    for index, row in enumerate(rows, start=1):
        if args.ano and row.get("ano") != args.ano:
            continue
        if args.only_semaforo and row.get("semaforo") != args.only_semaforo:
            continue
        if args.limit and processed >= args.limit:
            break
        processed += 1
        started = time.time()
        print(
            f"QUICK_AUDIT row={index} selected={processed} banca={row.get('banca','')} "
            f"orgao={compact(row.get('orgao',''), 80)} numero={row.get('numero','') or '-'}",
            flush=True,
        )
        new_row, issues, validation = quick_audit_row(row, crawler, crawler_args, args)
        if issues:
            flagged += 1
            new_row["semaforo"] = "revisar"
            new_row["ai_decision"] = "revisar"
            new_row["ai_issues"] = ";".join(dict.fromkeys(issues))
            new_row["ai_changed_fields"] = ",".join(
                field for field in BASE_FIELDS if str(new_row.get(field, "")) != str(row.get(field, ""))
            )
            print(f"  FLAG issues={new_row['ai_issues']} {time.time() - started:.1f}s", flush=True)
        else:
            new_row["ai_decision"] = "listo"
            new_row["ai_issues"] = ""
            new_row["ai_changed_fields"] = ""
            print(f"  OK {time.time() - started:.1f}s", flush=True)
        new_row["ai_validation"] = ";".join(validation)
        new_row["ai_model"] = "quick-audit:v2"
        new_row["ai_checked_at"] = checked_at
        rows[index - 1] = new_row

    write_csv(args.output, rows, fields)
    print(f"OUT {args.output}", flush=True)
    print(f"PROCESSED {processed} FLAGGED {flagged}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
