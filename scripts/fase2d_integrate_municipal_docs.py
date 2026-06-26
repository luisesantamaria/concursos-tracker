#!/usr/bin/env python3
"""
Fase 2D - integrar Ache/Fase 2 con el mapa municipal y documentos Fase 3A.

Objetivo:
  - Tomar cada concurso preliminar de Ache.
  - Detectar municipio y tipo esperado (concurso / processo seletivo).
  - Cruzar contra:
      data/sites_municipios_rs.csv
      data/fase3a_download_queue_rs.csv
  - Llenar huecos con:
      1) URL base oficial donde se publican documentos del municipio.
      2) Link del edital principal cuando el match es confiable.

Regla de precision:
  Si Ache trae numero de edital, el documento municipal debe coincidir con ese
  numero, o debe ser un edital de apertura del mismo municipio/ano con senales
  fuertes. Si no alcanza, queda como revision o base_only; no se inventa.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ache_rs_official_pipeline as ache  # noqa: E402
import fase2c_sites_municipios as sites  # noqa: E402
from excel_utils import read_csv_dicts, write_table, write_xlsx, read_xlsx_dicts  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ACHE = PROJECT_ROOT / "data" / "ache_rs_fase2_v2.csv"
DEFAULT_SITES = PROJECT_ROOT / "data" / "sites_municipios_rs.csv"
DEFAULT_QUEUE = PROJECT_ROOT / "data" / "fase3a_download_queue_rs.csv"
DEFAULT_OUT = PROJECT_ROOT / "data" / "ache_rs_fase2d_municipal_integrated.csv"
DEFAULT_XLSX = PROJECT_ROOT / "data" / "ache_rs_fase2d_municipal_integrated.xlsx"
DEFAULT_MD = PROJECT_ROOT / "data" / "ache_rs_fase2d_municipal_integrated.md"

CURRENT_YEAR = datetime.now().year
PROGRESS_LOG_PATH: Optional[Path] = None
PROGRESS_ENABLED = False
PROGRESS_ROW_N = ""
LEGALLE_INDEX_CACHE: Optional[List[Tuple[str, str, str]]] = None
LASALLE_INDEX_CACHE: Optional[List[Tuple[str, str, str]]] = None
ACHE_DETAIL_FAST_CACHE: Dict[str, "ache.f1.FetchResult"] = {}

TIPO_FIELD = "tipo"
FASE2D_FIELDS = [
    "fase2d_attempted",
    "fase2d_status",
    "fase2d_action",
    "fase2d_city",
    "fase2d_city_slug",
    "fase2d_city_match_method",
    "fase2d_expected_source_kind",
    "fase2d_expected_edital_num",
    "fase2d_base_url",
    "fase2d_base_kind",
    "fase2d_base_specific",
    "fase2d_index_url",
    "fase2d_main_edital_url",
    "fase2d_main_doc_page_url",
    "fase2d_main_doc_title",
    "fase2d_main_doc_type",
    "fase2d_main_doc_edital_num",
    "fase2d_main_doc_date",
    "fase2d_main_doc_source_kind",
    "fase2d_added_doc_urls",
    "fase2d_match_score",
    "fase2d_match_reasons",
    "fase2d_legalle_deep_status",
    "fase2d_legalle_detail_url",
    "fase2d_legalle_pdf",
    "fase2d_legalle_doc_count",
    "fase2d_lasalle_status",
    "fase2d_lasalle_detail_url",
    "fase2d_lasalle_pdf",
    "fase2d_lasalle_doc_count",
    "fase2d_depth_status",
    "fase2d_depth_page_url",
    "fase2d_depth_pdf",
    "fase2d_depth_doc_count",
]

SEMAFORO_FIELD = "semaforo"

DOC_TYPE_WEIGHT = {
    "edital_abertura": 70,
    "edital": 60,
    "processo_seletivo": 50,
    "concurso_publico": 42,
    "retificacao": 12,
    "inscricoes_homologadas": 8,
    "resultado": 6,
    "classificacao": 6,
    "homologacao": 5,
    "gabarito": 4,
    "convocacao": 2,
}

SECONDARY_TYPES = {
    "retificacao",
    "resultado",
    "classificacao",
    "homologacao",
    "gabarito",
    "inscricoes_homologadas",
    "convocacao",
}

SOURCE_KIND_LABEL = {
    "concursos_publicos": "concursos",
    "processos_seletivos": "processos_seletivos",
}

STOP_TOKENS = {
    "prefeitura", "municipal", "municipio", "camara", "câmara", "concurso",
    "concursos", "publico", "publicos", "publica", "processo", "seletivo",
    "simplificado", "edital", "aberto", "andamento", "rio", "grande", "sul",
    "para", "com", "das", "dos", "de", "do", "da", "em", "rs", "n",
}

MUNICIPAL_SCOPE_TERMS = (
    "prefeitura", "camara", "câmara", "municipal",
)

NON_MUNICIPAL_SCOPE_TERMS = (
    "universidade federal", "ufrgs", "ufsm", "unipampa", "ufcspa",
    "conselho regional", "brigada militar", "policia penal", "polícia penal",
    "grupo hospitalar", "riosaude", "rio saude", "sanep", "badesul",
    "consorcio", "consórcio", "companhia de desenvolvimento",
    "fenac", "feiras e empreendimentos",
)

NOISE_TERMS = (
    "licitacao", "licitação", "pregao", "pregão", "inexigibilidade",
    "dispensa", "leilao", "leilão", "carta de servicos", "carta_de_servicos",
    "plano municipal", "plano_municipal", "plano vacinacao", "plano_vacinacao",
    "livro erechim", "concurso cultural", "concurso literario",
    "concurso literário", "fotografia", "soberana", "rainha",
    "patrocinio", "patrocínio", "aviso", "livro", "sorteio",
)

BASE_ROUTE_MARKERS = (
    "/concursos", "/concurso", "concursos-publicos", "concursos_publicos",
    "processos-seletivos", "processo-seletivo", "processos_seletivos",
    "/editais/concursos", "/editais/processos", "/portal-da-transparencia/concursos",
    "/site/concursos", "/lista/",
)

GENERIC_BASE_PATHS = {
    "",
    "/",
    "/portal/concursos/",
    "/portal/concursos",
    "/portal/concursos/index_concursos.php",
    "/edital",
}


def split_urls(value: object) -> List[str]:
    out: List[str] = []
    for part in str(value or "").replace("\n", " | ").split(" | "):
        part = part.strip()
        if part.startswith("http") and part not in out:
            out.append(part)
    return out


def add_unique(target: List[str], values: Iterable[str]) -> None:
    for value in values:
        value = (value or "").strip()
        if value and value not in target:
            target.append(value)


def normalize(value: str) -> str:
    return sites.normalize(value or "")


def compact(value: str) -> str:
    return sites.slug_compact(value or "")


def url_pathish(url: str) -> str:
    """Path-like URL text robust to non-ASCII paths that confuse urlparse."""
    raw = str(url or "").split("#", 1)[0].split("?", 1)[0].lower()
    parsed_path = urlparse(url or "").path.lower()
    return f"{raw} {parsed_path}"


def url_is_pdf(url: str) -> bool:
    raw = str(url or "").split("#", 1)[0].lower()
    parsed_path = urlparse(url or "").path.lower()
    return ".pdf" in raw or parsed_path.endswith(".pdf")


def is_file_url(url: str) -> bool:
    path = url_pathish(url)
    return bool(re.search(r"\.(pdf|doc|docx|xls|xlsx|zip|rar|odt|ods)(?:\s|$)", path))


def is_good_base_route(url: str) -> bool:
    parsed = urlparse(url or "")
    path = (parsed.path or "/").lower()
    if not parsed.scheme or not parsed.netloc:
        return False
    if path in {"", "/"}:
        return False
    if re.match(r"^/20\d{2}/\d{2}/\d{2}/", path):
        return False
    year_match = re.search(r"(20\d{2})", path)
    if year_match and int(year_match.group(1)) < CURRENT_YEAR - 1:
        return False
    if path.count("-") > 5 and not any(
        marker in path
        for marker in (
            "/site/concursos", "/concursos/", "/concurso/", "/processo/",
            "/processoseletivo", "/processo-seletivo", "/licitacoes/detalhe",
            "/concursos/detalhe",
        )
    ):
        return False
    if "ibge" in path or "concurso-liter" in path or "concurso-cultural" in path:
        return False
    return any(marker in path for marker in BASE_ROUTE_MARKERS)


def url_domain(url: str) -> str:
    return urlparse(url or "").netloc.lower()


def parse_edital_nums(value: str) -> List[Tuple[int, int]]:
    text = str(value or "")
    found: List[Tuple[int, int]] = []
    patterns = [
        # Evitar fechas dd/mm/yyyy: en "27/05/2026" el candidato "05/2026"
        # esta precedido por "/", no por inicio/espacio/texto de edital.
        r"(?<![\d/.-])(\d{1,4})\s*/\s*(20\d{2})(?!\d)",
        r"(?:edital|n|no|nº|n°)[^\d]{0,12}(\d{1,4})\s*[-_]\s*(20\d{2})",
        r"\b(?:edital|concurso|processo[-_\s]+seletivo)[^;\n|]{0,140}?(\d{1,4})\s*[-_]\s*(20\d{2})",
        r"edital[-_\s]+(\d{1,4})[-_\s]+(20\d{2})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            try:
                num = int(match.group(1))
                year = int(match.group(2))
            except ValueError:
                continue
            pair = (num, year)
            if pair not in found:
                found.append(pair)
    return found


def tipo_from_family(family: str) -> str:
    if family == "processo":
        return "processo_seletivo"
    if family == "concurso":
        return "concurso_publico"
    return ""


def edital_label(pair: Optional[Tuple[int, int]]) -> str:
    if not pair:
        return ""
    return f"{pair[0]:02d}/{pair[1]}"


def row_edital_pair(row: Dict[str, str]) -> Optional[Tuple[int, int]]:
    values = [
        row.get("edital", ""),
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("attachment_titles", ""),
        row.get("ache_attachment_pages", ""),
        row.get("ache_attachment_pdfs", ""),
    ]
    for value in values:
        detected = ache.detect_edital_num(value or "")
        pairs = parse_edital_nums(detected or value or "")
        if pairs:
            return pairs[0]
    return None


def split_pipe_values(value: str) -> List[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"\s+\|\s+", raw) if part.strip()]


def edital_pair_from_blob(value: str) -> Optional[Tuple[int, int]]:
    pairs = parse_edital_nums(value or "")
    return pairs[0] if pairs else None


def row_attachment_items(row: Dict[str, str]) -> List[Dict[str, object]]:
    titles = split_pipe_values(row.get("attachment_titles", ""))
    pages = split_urls(row.get("ache_attachment_pages"))
    pdfs = split_urls(row.get("ache_attachment_pdfs"))
    max_len = max(len(titles), len(pages), len(pdfs), 0)
    items: List[Dict[str, object]] = []
    for idx in range(max_len):
        title = titles[idx] if idx < len(titles) else ""
        page = pages[idx] if idx < len(pages) else ""
        pdf = pdfs[idx] if idx < len(pdfs) else ""
        pair = edital_pair_from_blob(" ".join([title, page, pdf]))
        items.append({"title": title, "page": page, "pdf": pdf, "pair": pair})
    return items


def expand_multi_edital_rows(rows: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], int]:
    """Split Ache rows where one article exposes multiple edital attachments.

    The pipeline rule is one official edital per row. Ache sometimes packs two
    attachments in a single contest card, so we split only when the row has
    multiple attachment slots and at least one slot carries an edital number.
    """
    out: List[Dict[str, str]] = []
    added = 0
    for row in rows:
        items = row_attachment_items(row)
        meaningful = [
            item for item in items
            if item.get("title") or item.get("page") or item.get("pdf")
        ]
        should_split = len(meaningful) > 1 and any(item.get("pair") for item in meaningful)
        if not should_split:
            out.append(row)
            continue
        original_n = (row.get("n") or str(len(out) + 1)).strip()
        for idx, item in enumerate(meaningful, start=1):
            split_row = dict(row)
            split_row["n"] = f"{original_n}.{idx}"
            split_row["_fase2d_original_n"] = original_n
            split_row["_fase2d_split_index"] = str(idx)
            split_row["_fase2d_split_count"] = str(len(meaningful))
            split_row["attachment_titles"] = str(item.get("title") or "")
            split_row["ache_attachment_pages"] = str(item.get("page") or "")
            split_row["ache_attachment_pdfs"] = str(item.get("pdf") or "")
            pair = item.get("pair")
            if pair:
                split_row["edital"] = f"nÂº {edital_label(pair)}"
            out.append(split_row)
        added += len(meaningful) - 1
    return out, added


def doc_edital_pairs(doc: Dict[str, str]) -> List[Tuple[int, int]]:
    blob = " ".join([
        doc.get("edital_num_primary", ""),
        doc.get("edital_nums", ""),
        doc.get("doc_title", ""),
        doc.get("candidate_url", ""),
        doc.get("best_download_url", ""),
        doc.get("download_urls", ""),
    ])
    return parse_edital_nums(blob)


def doc_is_main_edital_like(doc: Dict[str, str]) -> bool:
    dtype = (doc.get("doc_type") or "").strip()
    title = normalize(doc.get("doc_title", ""))
    blob = normalize(" ".join([
        doc.get("doc_title", ""),
        doc.get("candidate_url", ""),
        doc.get("best_download_url", ""),
    ]))
    if any(term in blob for term in NOISE_TERMS):
        return False
    if "clique para mais informacoes" in title or "acessar os editais" in title:
        return False
    if title.startswith("aviso ") or title.startswith("livro "):
        return False
    if dtype in {"edital_abertura", "edital"}:
        return True
    if dtype == "processo_seletivo":
        return "edital" in blob or "processo seletivo" in title
    if dtype == "concurso_publico":
        if "extrato" in blob or "portaria" in blob:
            return False
        return "edital de abertura" in blob or "concurso publico" in title
    if "convoca candidato" in title or "nomeacao" in title or "nomear" in title:
        return False
    if "edital" in title and ("processo seletivo" in title or "concurso publico" in title):
        return True
    return False


def row_is_municipal_scope(row: Dict[str, str]) -> bool:
    blob = normalize(" ".join([
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("edital", ""),
    ]))
    if any(term in blob for term in NON_MUNICIPAL_SCOPE_TERMS):
        return False
    return any(term in blob for term in MUNICIPAL_SCOPE_TERMS)


def row_is_non_municipal_scope(row: Dict[str, str]) -> bool:
    blob = normalize(" ".join([
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("edital", ""),
        row.get("attachment_titles", ""),
    ]))
    return any(term in blob for term in NON_MUNICIPAL_SCOPE_TERMS)


def clear_stale_municipal_base_for_non_municipal(row: Dict[str, str]) -> None:
    """Remove generic prefeitura bases from non-municipal entities.

    A non-municipal row can still be official through a banca or a specific
    public entity page. What we should not keep is a generic municipal list
    such as /concursos when it did not produce a matching edital.
    """
    if (row.get("edital_pdf") or "").strip():
        return
    base = (row.get("official_base_url") or "").strip()
    if not base:
        return
    parsed = urlparse(base)
    host = parsed.netloc.lower()
    if not (host.endswith(".rs.gov.br") or host.endswith(".gov.br")):
        return
    if not is_generic_base(base):
        return
    if (row.get("edital_pagina") or "").strip() == base:
        row["edital_pagina"] = ""
    row["official_base_url"] = ""
    row["official_base_specific"] = ""
    row["tiene_pagina_oficial"] = ""
    docs = [
        url for url in split_urls(row.get("official_doc_urls"))
        if is_file_url(url) or "legalle" in url.lower() or "fundacaolasalle" in url.lower()
    ]
    row["official_doc_urls"] = " | ".join(docs)
    row["n_official_doc_urls"] = str(len(docs))
    sources = [
        url for url in split_urls(row.get("official_source_urls"))
        if url != base and not (urlparse(url).netloc.lower() == host and is_generic_base(url))
    ]
    row["official_source_urls"] = " | ".join(sources)
    row["n_official_source_urls"] = str(len(sources))
    if not docs:
        row["tiene_base_documentos"] = ""


def row_family(row: Dict[str, str]) -> str:
    blob = normalize(" ".join([
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("edital", ""),
        row.get("attachment_titles", ""),
    ]))
    if "processo seletivo" in blob or "processo seletivo simplificado" in blob or " pss " in f" {blob} ":
        return "processo"
    if "concurso" in blob:
        return "concurso"
    return ""


def context_family(context: str) -> str:
    blob = normalize(context or "")
    if "processo seletivo" in blob or "processo seletivo simplificado" in blob or " pss " in f" {blob} ":
        return "processo"
    if "concurso publico" in blob or "concurso p blico" in blob or "concurso pã blico" in blob:
        return "concurso"
    return ""


def family_matches_row(row: Dict[str, str], context: str) -> bool:
    row_fam = row_family(row)
    ctx_fam = context_family(context)
    return not row_fam or not ctx_fam or row_fam == ctx_fam


def populate_tipo(row: Dict[str, str]) -> None:
    family = row_family(row)
    if not family:
        family = context_family(" ".join([
            row.get("fase2d_main_doc_title", ""),
            row.get("fase2d_main_doc_type", ""),
            row.get("fase2d_expected_source_kind", ""),
            row.get("official_doc_urls", ""),
            row.get("edital_pagina", ""),
        ]))
    tipo = tipo_from_family(family)
    if tipo:
        row[TIPO_FIELD] = tipo


def doc_family(doc: Dict[str, str]) -> str:
    blob = normalize(" ".join([
        doc.get("doc_title", ""),
        doc.get("candidate_url", ""),
        doc.get("best_download_url", ""),
    ]))
    if "processo seletivo" in blob or "processo seletivo simplificado" in blob or " pss " in f" {blob} ":
        return "processo"
    if "concurso publico" in blob or "concurso público" in blob or "concursos publicos" in blob or " cp " in f" {blob} ":
        return "concurso"
    return ""


def parse_date(value: str) -> Tuple[int, int, int]:
    value = (value or "").strip()
    for pattern in ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, pattern)
            return (dt.year, dt.month, dt.day)
        except ValueError:
            pass
    match = re.search(r"(20\d{2})", value)
    if match:
        return (int(match.group(1)), 1, 1)
    return (0, 0, 0)


def infer_source_kind(row: Dict[str, str], docs: Sequence[Dict[str, str]], site_row: Optional[Dict[str, str]]) -> str:
    blob = normalize(" ".join([
        row.get("orgao", ""),
        row.get("edital", ""),
        row.get("detalle_ache", ""),
        row.get("attachment_titles", ""),
    ]))
    if "processo seletivo" in blob or "processo seletivo simplificado" in blob or " pss " in f" {blob} ":
        return "processos_seletivos"
    if "concurso" in blob:
        return "concursos_publicos"
    kinds = sorted({d.get("source_kind", "") for d in docs if d.get("source_kind")})
    if len(kinds) == 1:
        return kinds[0]
    if site_row:
        if site_row.get("concursos_url") and not site_row.get("processos_seletivos_url"):
            return "concursos_publicos"
        if site_row.get("processos_seletivos_url") and not site_row.get("concursos_url"):
            return "processos_seletivos"
    return "concursos_publicos"


def tokenize_signal(value: str) -> List[str]:
    tokens: List[str] = []
    for token in normalize(value).split():
        if len(token) < 4 or token in STOP_TOKENS or token.isdigit():
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def row_signal_tokens(row: Dict[str, str], city_slug: str) -> List[str]:
    blob = " ".join([
        row.get("orgao", ""),
        row.get("edital", ""),
        row.get("detalle_ache", ""),
        row.get("banca_guess", ""),
    ])
    tokens = tokenize_signal(blob)
    city_tokens = set(tokenize_signal(city_slug))
    return [t for t in tokens if t not in city_tokens][:12]


def find_city(row: Dict[str, str], municipality_rows: Sequence[Dict[str, str]]) -> Tuple[str, str, str]:
    blob = " ".join([
        row.get("orgao", ""),
        row.get("edital", ""),
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
    ])
    city = ache.detect_city(blob)
    if city:
        slug = compact(city)
        for item in municipality_rows:
            if item.get("municipio_slug") == slug:
                return item.get("municipio", city), slug, "ache_detect_city"

    norm_blob = normalize(blob)
    compact_blob = compact(blob)
    candidates = sorted(municipality_rows, key=lambda r: len(r.get("municipio", "")), reverse=True)
    for item in candidates:
        name = normalize(item.get("municipio", ""))
        slug = item.get("municipio_slug", "")
        if not name or not slug:
            continue
        if name in norm_blob or slug in compact_blob:
            return item.get("municipio", ""), slug, "municipality_scan"
    return "", "", "no_city"


def base_urls_for(site_row: Optional[Dict[str, str]], expected_kind: str) -> List[Tuple[str, str]]:
    if not site_row:
        return []
    order = [expected_kind]
    other = "processos_seletivos" if expected_kind == "concursos_publicos" else "concursos_publicos"
    order.append(other)
    out: List[Tuple[str, str]] = []
    for kind in order:
        field = "concursos_url" if kind == "concursos_publicos" else "processos_seletivos_url"
        url = (site_row.get(field) or "").strip()
        if url and is_good_base_route(url) and (url, kind) not in out:
            out.append((url, kind))
    return out


def is_generic_base(url: str) -> bool:
    if not url:
        return True
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/").lower()
    if parsed.query:
        # pagina_editais.php?concurso=... y /informacoes/... son especificas.
        return False
    if path in GENERIC_BASE_PATHS:
        return True
    if ("legalleconcursos.com.br" in host or "institutolegalle.org.br" in host) and path in {"", "/", "/edital"}:
        return True
    if "fundatec.org.br" in host and "pagina_editais.php" not in parsed.path.lower():
        return True
    return False


def best_base_from_match(match: Optional[Dict[str, object]], fallback_bases: Sequence[Tuple[str, str]]) -> Tuple[str, str, str]:
    if match:
        doc = match["doc"]
        candidate = str(doc.get("candidate_url") or "")
        if candidate and not is_file_url(candidate):
            return candidate, str(doc.get("source_kind") or ""), "SI"
        source = str(doc.get("source_page_url") or doc.get("index_url") or "")
        if source:
            return source, str(doc.get("source_kind") or ""), "NO"
    if fallback_bases:
        return fallback_bases[0][0], fallback_bases[0][1], "NO"
    return "", "", ""


def score_doc(
    row: Dict[str, str],
    doc: Dict[str, str],
    expected_kind: str,
    expected_pair: Optional[Tuple[int, int]],
    tokens: Sequence[str],
) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []
    dtype = (doc.get("doc_type") or "").strip()
    title_blob = " ".join([
        doc.get("doc_title", ""),
        doc.get("candidate_url", ""),
        doc.get("best_download_url", ""),
        doc.get("context", ""),
    ])
    norm_blob = normalize(title_blob)
    pairs = doc_edital_pairs(doc)
    exact = bool(expected_pair and expected_pair in pairs)
    row_fam = row_family(row)
    doc_fam = doc_family(doc)

    if not expected_pair and not (row.get("orgao") or "").strip():
        score -= 100
        reasons.append("missing_orgao_no_edital")

    if doc.get("source_kind") == expected_kind:
        score += 18
        reasons.append("source_kind")
    elif expected_kind and doc.get("source_kind"):
        score -= 8
        reasons.append("source_kind_mismatch")

    type_weight = DOC_TYPE_WEIGHT.get(dtype, 0)
    main_like = doc_is_main_edital_like(doc)
    if main_like:
        score += type_weight
        reasons.append(f"type:{dtype or 'edital_like'}")
    else:
        score += type_weight
        if dtype in SECONDARY_TYPES:
            score -= 12
            reasons.append(f"secondary:{dtype}")

    if exact:
        score += 95
        reasons.append("edital_num_exact")
    elif expected_pair:
        expected_year = expected_pair[1]
        if main_like and dtype in {"edital_abertura", "edital"} and not pairs and str(expected_year) in title_blob:
            score += 26
            reasons.append("main_edital_same_year")
        if pairs and all(pair != expected_pair for pair in pairs):
            score -= 35
            reasons.append("different_edital_num")

    if row_fam and doc_fam and row_fam != doc_fam:
        score -= 85
        reasons.append("certame_family_mismatch")

    overlap = 0
    for token in tokens:
        if token and token in norm_blob:
            overlap += 1
    if overlap:
        score += min(overlap * 6, 24)
        reasons.append(f"token_overlap:{overlap}")

    doc_year = (doc.get("year_guess") or "").strip()
    if not doc_year:
        parsed_year = parse_date(doc.get("date_guess", ""))[0]
        doc_year = str(parsed_year) if parsed_year else ""
    if doc_year and int(doc_year) >= CURRENT_YEAR - 1:
        score += 6
        reasons.append("recent_year")

    try:
        source_score = int(float(doc.get("score") or 0))
    except ValueError:
        source_score = 0
    score += min(max(source_score, 0), 18)

    if any(term in norm_blob for term in NOISE_TERMS):
        score -= 80
        reasons.append("noise")

    return score, reasons


def match_passes_acceptance(match: Dict[str, object], expected_pair: Optional[Tuple[int, int]]) -> bool:
    score = int(match.get("score") or 0)
    doc = match["doc"]
    reasons = set(match.get("reasons", []))
    pairs = doc_edital_pairs(doc)
    if "certame_family_mismatch" in reasons or "noise" in reasons:
        return False
    if expected_pair:
        if "edital_num_exact" in reasons:
            return score >= 90 and doc_is_main_edital_like(doc)
        if not pairs and (doc.get("doc_type") or "") in {"edital_abertura", "edital"}:
            return (
                score >= 95
                and doc_is_main_edital_like(doc)
                and "main_edital_same_year" in reasons
            )
        return False
    return score >= 82 and doc_is_main_edital_like(doc) and "recent_year" in reasons


def choose_doc_match(
    row: Dict[str, str],
    docs: Sequence[Dict[str, str]],
    expected_kind: str,
    expected_pair: Optional[Tuple[int, int]],
    city_slug: str,
) -> Optional[Dict[str, object]]:
    if not docs:
        return None
    tokens = row_signal_tokens(row, city_slug)
    scored: List[Dict[str, object]] = []
    for doc in docs:
        if not (doc.get("best_download_url") or "").strip():
            continue
        score, reasons = score_doc(row, doc, expected_kind, expected_pair, tokens)
        date_key = parse_date(doc.get("date_guess", ""))
        scored.append({
            "doc": doc,
            "score": score,
            "reasons": reasons,
            "date_key": date_key,
        })
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            int(item["score"]),
            1 if doc_is_main_edital_like(item["doc"]) else 0,
            item["date_key"],
        ),
        reverse=True,
    )
    for item in scored:
        if match_passes_acceptance(item, expected_pair):
            return item
    best = scored[0]
    threshold = 90 if expected_pair else 68
    if int(best["score"]) >= threshold:
        return best
    return best


def accepted_match(match: Optional[Dict[str, object]], expected_pair: Optional[Tuple[int, int]]) -> bool:
    if not match:
        return False
    return match_passes_acceptance(match, expected_pair)


def download_urls_for_doc(doc: Dict[str, str]) -> List[str]:
    urls: List[str] = []
    add_unique(urls, [doc.get("best_download_url", "")])
    raw = doc.get("download_urls", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                add_unique(urls, [str(x) for x in parsed])
        except json.JSONDecodeError:
            add_unique(urls, split_urls(raw))
    return urls


def apply_base(row: Dict[str, str], base_url: str, base_specific: str, update_core: bool) -> None:
    if not base_url or not update_core:
        return
    current = (row.get("official_base_url") or "").strip()
    if is_generic_base(current) or base_specific == "SI":
        row["official_base_url"] = base_url
        row["official_base_specific"] = base_specific or "NO"
    row["tiene_pagina_oficial"] = "SI"
    row["tiene_base_documentos"] = "SI"
    sources = split_urls(row.get("official_source_urls"))
    add_unique(sources, [base_url])
    row["official_source_urls"] = " | ".join(sources)
    row["n_official_source_urls"] = str(len(sources))


def apply_doc(row: Dict[str, str], doc_urls: Sequence[str]) -> List[str]:
    doc_urls = [url for url in doc_urls if url and not ache.is_noise_url(url)]
    docs = split_urls(row.get("official_doc_urls"))
    docs = [url for url in docs if url and not ache.is_noise_url(url)]
    before = set(docs)
    add_unique(docs, doc_urls)
    row["official_doc_urls"] = " | ".join(docs)
    row["n_official_doc_urls"] = str(len(docs))
    row["tiene_base_documentos"] = "SI" if docs else row.get("tiene_base_documentos", "")
    added = [url for url in docs if url not in before]
    if doc_urls:
        best = doc_urls[0]
        if url_is_pdf(best) and not (row.get("edital_pdf") or "").strip():
            row["edital_pdf"] = best
        if not is_file_url(best) and not (row.get("edital_pagina") or "").strip():
            row["edital_pagina"] = best
    return added


def detect_edital_label_relaxed(context: str) -> str:
    text = ache.clean_text(context or "")[:4000]
    patterns = [
        r"\bEdital\b[^0-9]{0,140}(\d{1,2})\s*[-/]\s*(20\d{2})",
        r"\bConcurso\b[^0-9]{0,140}(\d{1,2})\s*[-/]\s*(20\d{2})",
        r"\bProcesso\s+Seletivo\b[^0-9]{0,140}(\d{1,2})\s*[-/]\s*(20\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if not match:
            continue
        num = int(match.group(1))
        year = int(match.group(2))
        if 1 <= num <= 99 and year >= CURRENT_YEAR - 1:
            return f"nº {num:02d}/{year}"
    return ""


def update_edital_from_row_context(row: Dict[str, str]) -> None:
    if not row_edital_is_suspicious(row):
        return
    label = detect_edital_label_relaxed(" ".join([
        row.get("fase2d_main_doc_title", ""),
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
    ]))
    if label:
        row["edital"] = label
    elif row.get("edital"):
        row["edital"] = normalize_edital_cell(row.get("edital", ""))


def cebraspe_page_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower()
    if not (host_matches := getattr(ache, "host_matches", None)):
        return ""
    if not host_matches(host, "cebraspe.org.br"):
        return ""
    match = re.search(r"/concursos/([^/]+)/arquivos/", parsed.path, re.I)
    if not match:
        return ""
    return f"https://www.cebraspe.org.br/concursos/{match.group(1).upper()}"


def enrich_cebraspe_from_cdn(row: Dict[str, str]) -> str:
    urls: List[str] = []
    for field in ("edital_pdf", "official_base_url", "official_doc_urls", "official_source_urls"):
        add_unique(urls, split_urls(row.get(field)))
    page = ""
    for url in urls:
        page = cebraspe_page_from_url(url)
        if page:
            break
    if not page:
        return ""
    pdf = (row.get("edital_pdf") or "").strip()
    if not pdf:
        pdfs = [url for url in urls if url_is_pdf(url)]
        pdf = pdfs[0] if pdfs else ""
    apply_base(row, page, "SI", update_core=True)
    row["official_base_url"] = page
    row["official_base_specific"] = "SI"
    row["edital_pagina"] = page
    if pdf:
        apply_doc(row, [pdf])
        row["edital_pdf"] = pdf
        row["fase2d_main_edital_url"] = pdf
        row["fase2d_main_doc_title"] = row.get("fase2d_main_doc_title") or "Edital de abertura - Cebraspe"
        row["fase2d_main_doc_type"] = row.get("fase2d_main_doc_type") or "edital_abertura"
        row["fase2d_main_doc_page_url"] = page
        row["tiene_pagina_oficial"] = "SI"
        row["tiene_base_documentos"] = "SI"
        update_edital_from_row_context(row)
        row["fase2d_status"] = row.get("fase2d_status") or "filled_doc"
        row["fase2d_action"] = row.get("fase2d_action") or "updated_core"
        return "filled_doc"
    return "base_only"


def fetch_ache_detail_fast(url: str, fetch_args: argparse.Namespace):
    """Fetch Ache fallback pages with a strict, cheap path.

    The normal project fetcher may try requests and then multiple curl-cffi
    browser impersonations. That is useful for primary sources, but Ache is a
    last-resort radar page here; waiting 30+ seconds per unresolved row makes
    the whole pipeline unusable.
    """
    if url in ACHE_DETAIL_FAST_CACHE:
        return ACHE_DETAIL_FAST_CACHE[url]
    timeout = float(getattr(fetch_args, "ache_fallback_timeout", 2.5))
    timeout = max(0.8, min(timeout, float(getattr(fetch_args, "timeout", timeout))))
    source = ache.f1.Source("ache", "ache", url, "radar")
    started = time.time()
    res = ache.f1.fetch_with_requests(source, timeout, False, 0)
    progress_log(
        PROGRESS_ENABLED,
        "    ACHE_FAST n={row_n} status={status} result={result} seconds={seconds:.2f} url={url}".format(
            row_n=PROGRESS_ROW_N or "-",
            status=getattr(res, "status", ""),
            result=getattr(res, "result", ""),
            seconds=time.time() - started,
            url=url,
        ),
    )
    ACHE_DETAIL_FAST_CACHE[url] = res
    return res


def inspect_ache_attachment_fast(url: str, fetch_args: argparse.Namespace) -> Dict[str, object]:
    res = fetch_ache_detail_fast(url, fetch_args)
    raw_html = res.body or ""
    official, nested_attachment_pages, ache_pdfs = ache.official_and_attachment_links(raw_html, res.final_url or url)
    text_excerpt = ache.f1.visible_text(raw_html)[:5000] if raw_html else ""
    return {
        "url": url,
        "status": res.status,
        "title": ache.f1.page_title(raw_html) if raw_html else "",
        "text_excerpt": text_excerpt,
        "official": official,
        "nested_attachment_pages": nested_attachment_pages,
        "ache_pdfs": ache_pdfs,
    }


def canonical_doc_page_for_row(
    row: Dict[str, str],
    page_url: str,
    expected_kind: str,
    fetch_args: argparse.Namespace,
) -> str:
    page_url = (page_url or "").strip()
    if not page_url or is_file_url(page_url):
        return page_url
    parsed = urlparse(page_url)
    path = parsed.path
    if expected_kind == "processos_seletivos" and "/licitacoes/detalhe/" in path:
        alt = page_url.replace("/licitacoes/detalhe/", "/processo_seletivo/detalhe/")
        try:
            res = ache.fetch(alt, fetch_args)
        except Exception:
            res = None
        if res and res.status == 200:
            text = normalize(" ".join([
                ache.f1.page_title(res.body or ""),
                ache.f1.visible_text(res.body or "")[:3000],
                alt,
            ]))
            if "processo seletivo" in text and "edital" in text:
                return alt
    return page_url


def enrich_from_ache_detail(row: Dict[str, str], fetch_args: argparse.Namespace) -> str:
    detail = (row.get("detalle_ache") or "").strip()
    if not detail or not ache.is_ache_url(detail):
        return ""
    base = (row.get("official_base_url") or "").strip()
    has_pdf = bool((row.get("edital_pdf") or "").strip())
    needs = (
        not base
        or is_generic_base(base)
        or not has_pdf
    )
    if not needs:
        return ""
    res = fetch_ache_detail_fast(detail, fetch_args)
    raw_html = res.body or ""
    if not raw_html:
        return "ache_empty"
    official, attachment_pages, ache_pdfs = ache.official_and_attachment_links(raw_html, res.final_url or detail)
    for attachment in attachment_pages[:3]:
        inspected = inspect_ache_attachment_fast(attachment, fetch_args)
        official.extend(inspected.get("official", []))
        add_unique(ache_pdfs, inspected.get("ache_pdfs", []))

    urls = [url for url, _anchor in official if ache.is_official(url) and not ache.is_noise_url(url)]
    if not urls and ache_pdfs:
        # Ache PDFs remain radar evidence only; keep them in the audit columns,
        # but do not promote them as official edital_pdf.
        row["ache_attachment_pdfs"] = " | ".join(split_urls(row.get("ache_attachment_pdfs")) + [u for u in ache_pdfs if u not in split_urls(row.get("ache_attachment_pdfs"))])
        row["n_ache_attachment_pdfs"] = str(len(split_urls(row.get("ache_attachment_pdfs"))))
        return "ache_pdf_only"

    if not urls:
        return "ache_no_official"

    detected_banca = ache.detect_banca_from_urls(urls)
    if detected_banca:
        row["banca_guess"] = detected_banca

    preferred = ""
    for url in urls:
        if ache.is_specific_official_url(url) and not is_file_url(url):
            preferred = url
            break
    if not preferred:
        preferred = urls[0]

    add_unique(urls, split_urls(row.get("official_source_urls")))
    row["official_source_urls"] = " | ".join(urls)
    row["n_official_source_urls"] = str(len(urls))
    if preferred and not is_file_url(preferred):
        apply_base(row, preferred, "SI" if ache.is_specific_official_url(preferred) else "NO", update_core=True)
        row["edital_pagina"] = preferred
    docs = [url for url in urls if is_file_url(url)]
    if docs:
        apply_doc(row, docs)
    update_edital_from_row_context(row)
    return "ache_official"


def make_fetch_args(timeout: float = 30, delay_min: float = 0.05, delay_max: float = 0.15) -> argparse.Namespace:
    return argparse.Namespace(
        timeout=timeout,
        cache=True,
        delay_min=delay_min,
        delay_max=delay_max,
        resolve_min_score=8,
        max_edital_probe_docs=5,
    )


def probe_official_page_stable(url: str, args: argparse.Namespace) -> Dict[str, object]:
    probe = ache.probe_official_page(url, args)
    if probe.get("doc_links") or probe.get("text_excerpt") or probe.get("status"):
        return probe
    try:
        ache.FETCH_CACHE.pop(url, None)
    except Exception:
        pass
    old_cache = getattr(args, "cache", True)
    try:
        setattr(args, "cache", False)
        retry = ache.probe_official_page(url, args)
    finally:
        setattr(args, "cache", old_cache)
    return retry if (retry.get("doc_links") or retry.get("text_excerpt") or retry.get("status")) else probe


def legalle_row_hint(row: Dict[str, str]) -> bool:
    blob = normalize(" ".join([
        row.get("banca_guess", ""),
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("official_base_url", ""),
        row.get("edital_pagina", ""),
        row.get("official_source_urls", ""),
        row.get("official_doc_urls", ""),
    ]))
    return "legalle" in blob or "institutolegalle" in blob


def legalle_discovery_signal(row: Dict[str, str], city: str) -> bool:
    """Cheap signal to search Legalle even when Ache guessed the banca wrong."""
    if not city:
        return False
    if (row.get("edital_pdf") or "").strip() and not current_pdf_needs_repair(row):
        return False
    blob = normalize(" ".join([
        row.get("ache_attachment_pages", ""),
        row.get("ache_attachment_pdfs", ""),
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
        row.get("orgao", ""),
    ]))
    has_ache_edital_signal = (
        bool(split_urls(row.get("ache_attachment_pages")))
        or bool(split_urls(row.get("ache_attachment_pdfs")))
        or "edital concurso" in blob
        or "edital divulgado" in blob
        or "edital processo seletivo" in blob
        or "dois editais" in blob
        or "2 editais" in blob
        or "editais" in blob
    )
    if not has_ache_edital_signal:
        return False
    # Keep this discovery narrow. It is a resolver for rows where Ache already
    # saw an edital/anexo, not a general blind search across every unresolved row.
    return "concurso" in blob or "processo seletivo" in blob or "edital" in blob


def legalle_urls_from_row(row: Dict[str, str]) -> List[str]:
    urls: List[str] = []
    fields = [
        "official_base_url",
        "edital_pagina",
        "fase2d_base_url",
        "fase2d_main_doc_page_url",
        "official_source_urls",
        "official_doc_urls",
    ]
    for field in fields:
        add_unique(urls, split_urls(row.get(field)))
    for key, value in row.items():
        if key.startswith("official_doc_"):
            add_unique(urls, split_urls(value))
    filtered: List[str] = []
    for url in urls:
        low = url.lower()
        if "legalle" not in low and "institutolegalle" not in low:
            continue
        if ache.is_noise_url(url):
            continue
        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        if ache.is_legalle_detail_url(url) or ache.is_legalle_doc_url(url):
            add_unique(filtered, [url])
            continue
        if path in {"", "/", "/edital"} or path.startswith("/edital/index/"):
            add_unique(filtered, [url])
    return filtered


def legalle_query_text(row: Dict[str, str], city: str) -> str:
    return " ".join([
        row.get("orgao", ""),
        row.get("edital", ""),
        row.get("detalle_ache", ""),
        row.get("attachment_titles", ""),
        city or "",
    ]).strip()


def city_context_matches(city: str, text: str) -> bool:
    city_norm = normalize(city or "")
    if not city_norm:
        return True
    text_norm = normalize(text or "")
    if city_norm in text_norm:
        return True
    tokens = [
        token for token in city_norm.split()
        if token not in {"de", "do", "da", "dos", "das"} and len(token) >= 4
    ]
    if not tokens:
        return True
    return all(token in text_norm for token in tokens)


def legalle_detail_matches_row(row: Dict[str, str], city: str, detail_url: str, probe: Dict[str, object]) -> bool:
    context = " ".join([
        str(probe.get("title") or ""),
        str(probe.get("text_excerpt") or ""),
        " ".join(title for _url, title in probe.get("doc_links", [])[:8]),
        detail_url,
    ])
    if not family_matches_row(row, context):
        return False
    if city and not city_context_matches(city, context):
        return False
    expected_pair = row_edital_pair(row)
    if expected_pair:
        label = edital_label(expected_pair)
        label_norm = normalize(label)
        context_norm = normalize(context)
        # A missing edital number is not fatal if the municipality matches, but a
        # different municipality on Legalle is fatal. The number is used as a
        # second positive signal, not as the only gate.
        if label_norm in context_norm:
            return True
    return True


def legalle_index_candidates(fetch_args: argparse.Namespace) -> List[Tuple[str, str, str]]:
    """Build a reusable Legalle candidate index once per run.

    Legalle has two active portals and several status tabs. Fetching every tab
    for every row is the main runtime killer, so we cache the parsed detail
    candidates and only probe the top row-specific matches later.
    """
    global LEGALLE_INDEX_CACHE
    if LEGALLE_INDEX_CACHE is not None:
        return LEGALLE_INDEX_CACHE

    started = time.time()
    progress_log(PROGRESS_ENABLED, "LEGALLE_CACHE start")
    candidates: List[Tuple[str, str, str]] = []
    seen: set[str] = set()
    index_urls = list(dict.fromkeys([
        *ache.resolver_indexes_for("legalle", "https://portal.editais.legalleconcursos.com.br/edital"),
        *ache.resolver_indexes_for("legalle", "https://portal.institutolegalle.org.br/edital"),
    ]))
    for index_url in index_urls:
        fetch_started = time.time()
        res = ache.fetch(index_url, fetch_args)
        parsed = 0
        if res.body:
            for candidate_url, context in ache.candidate_links_from_index(res.body, res.final_url or index_url):
                if not ache.is_legalle_detail_url(candidate_url) or candidate_url in seen:
                    continue
                if ache.is_noise_url(candidate_url):
                    continue
                seen.add(candidate_url)
                parsed += 1
                candidates.append((candidate_url, context, index_url))
        progress_log(
            PROGRESS_ENABLED,
            "LEGALLE_CACHE index seconds={seconds:.2f} status={status} parsed={parsed} url={url}".format(
                seconds=time.time() - fetch_started,
                status=getattr(res, "status", ""),
                parsed=parsed,
                url=index_url,
            ),
        )
    LEGALLE_INDEX_CACHE = candidates
    progress_log(
        PROGRESS_ENABLED,
        f"LEGALLE_CACHE end candidates={len(candidates)} seconds={time.time() - started:.2f}",
    )
    return candidates


def resolve_legalle_detail_for_row(
    row: Dict[str, str],
    city: str,
    fetch_args: argparse.Namespace,
) -> Tuple[str, Dict[str, object]]:
    query_text = legalle_query_text(row, city)
    expected_pair = row_edital_pair(row)
    edital_num = edital_label(expected_pair) if expected_pair else ""
    row_fam = row_family(row)
    candidates: List[Tuple[int, str, str]] = []
    for candidate_url, context, _index_url in legalle_index_candidates(fetch_args):
        context_blob = f"{context} {candidate_url}"
        score = ache.match_score(context_blob, query_text, edital_num)
        if city and city_context_matches(city, context_blob):
            score += 35
        if expected_pair and expected_pair in parse_edital_nums(context_blob):
            score += 60
        candidate_fam = context_family(context_blob)
        if row_fam and candidate_fam == row_fam:
            score += 80
        elif row_fam and candidate_fam and candidate_fam != row_fam:
            score -= 80
        if score > 0 or city_context_matches(city, context_blob):
            candidates.append((score, candidate_url, context))
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _score, candidate_url, _context in candidates[:40]:
        probe = probe_official_page_stable(candidate_url, fetch_args)
        if legalle_detail_matches_row(row, city, candidate_url, probe):
            return candidate_url, probe
    return "", {}


def enrich_legalle_deep(
    row: Dict[str, str],
    city: str,
    fetch_args: argparse.Namespace,
    allow_discovery: bool = False,
) -> str:
    """Resolve Legalle detail pages and opening-edital PDFs through the pipeline."""
    row["fase2d_legalle_deep_status"] = ""
    row["fase2d_legalle_detail_url"] = ""
    row["fase2d_legalle_pdf"] = ""
    row["fase2d_legalle_doc_count"] = ""
    hinted = legalle_row_hint(row)
    if not hinted and not (allow_discovery and legalle_discovery_signal(row, city)):
        return ""

    # Remove old static assets that may have been collected before the Legalle
    # extractor knew the platform document pattern.
    docs = [url for url in split_urls(row.get("official_doc_urls")) if not ache.is_noise_url(url)]
    row["official_doc_urls"] = " | ".join(docs)
    row["n_official_doc_urls"] = str(len(docs))

    detail_url = ""
    for url in legalle_urls_from_row(row):
        if ache.is_legalle_detail_url(url):
            detail_url = url
            break

    query_text = legalle_query_text(row, city)
    expected_pair = row_edital_pair(row)
    edital_num = edital_label(expected_pair) if expected_pair else ""

    probe: Dict[str, object] = {}
    if detail_url:
        probe = probe_official_page_stable(detail_url, fetch_args)
        if not legalle_detail_matches_row(row, city, detail_url, probe):
            detail_url = ""
            probe = {}

    if not detail_url:
        detail_url, probe = resolve_legalle_detail_for_row(row, city, fetch_args)

    if not detail_url:
        row["fase2d_legalle_deep_status"] = "not_resolved"
        return "not_resolved"

    if not hinted:
        row["banca_guess"] = "legalle"

    legalle_context = " ".join([
        str(probe.get("title") or ""),
        str(probe.get("text_excerpt") or ""),
        detail_url,
    ])
    update_metadata_from_official_context(row, legalle_context, detail_url)
    doc_links = [(url, title) for url, title in probe.get("doc_links", []) if ache.is_legalle_doc_url(url)]
    if not doc_links and probe.get("text_excerpt"):
        row["fase2d_legalle_deep_status"] = "detail_no_docs"
    elif not doc_links:
        row["fase2d_legalle_deep_status"] = "detail_fetch_empty"

    doc_links.sort(key=lambda item: ache.legalle_doc_rank(item[1], item[0]))
    doc_urls = [url for url, _title in doc_links]
    best_pdf = ""
    if doc_links and ache.legalle_doc_rank(doc_links[0][1], doc_links[0][0])[0] <= 2:
        best_pdf = doc_links[0][0]

    row["fase2d_legalle_detail_url"] = detail_url
    row["fase2d_legalle_pdf"] = best_pdf
    row["fase2d_legalle_doc_count"] = str(len(doc_urls))
    row["fase2d_base_url"] = detail_url
    row["fase2d_base_kind"] = "legalle_detail"
    row["fase2d_base_specific"] = "SI"
    row["fase2d_main_doc_page_url"] = detail_url
    row["fase2d_main_edital_url"] = best_pdf
    if doc_links:
        row["fase2d_main_doc_title"] = doc_links[0][1]
        row["fase2d_main_doc_type"] = "edital_abertura" if best_pdf else "documento_revisar"
        row["fase2d_main_doc_source_kind"] = "legalle"
        if best_pdf:
            update_metadata_from_official_context(row, f"{doc_links[0][1]} {best_pdf}", detail_url)

    prior_pdf = (row.get("edital_pdf") or "").strip()
    apply_base(row, detail_url, "SI", update_core=True)
    row["edital_pagina"] = detail_url
    added = apply_doc(row, doc_urls)
    if not best_pdf and not prior_pdf:
        row["edital_pdf"] = ""
    row["fase2d_added_doc_urls"] = " | ".join(added)
    if best_pdf:
        row["edital_pdf"] = best_pdf
        row["tiene_pagina_oficial"] = "SI"
        row["tiene_base_documentos"] = "SI"
        row["fase2d_legalle_deep_status"] = "filled_doc"
        row["fase2d_status"] = "filled_doc"
        row["fase2d_action"] = "updated_core"
        return "filled_doc"

    row["fase2d_legalle_deep_status"] = row["fase2d_legalle_deep_status"] or "detail_no_pdf"
    if not row.get("fase2d_status"):
        row["fase2d_status"] = "base_only"
        row["fase2d_action"] = "updated_base_only"
    return row["fase2d_legalle_deep_status"]


def lasalle_row_hint(row: Dict[str, str]) -> bool:
    blob = normalize(" ".join([
        row.get("banca_guess", ""),
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("official_base_url", ""),
        row.get("edital_pagina", ""),
        row.get("official_source_urls", ""),
        row.get("official_doc_urls", ""),
    ]))
    return (
        "lasalle" in blob
        or "la salle" in blob
        or "fundacao la salle" in blob
        or "fundacaolasalle" in blob
    )


def lasalle_discovery_signal(row: Dict[str, str], city: str) -> bool:
    """Search La Salle when Ache saw an edital/anexo but the core URL is missing."""
    if not city:
        return False
    if (row.get("edital_pdf") or "").strip() and not current_pdf_needs_repair(row):
        return False
    blob = normalize(" ".join([
        row.get("ache_attachment_pages", ""),
        row.get("ache_attachment_pdfs", ""),
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
        row.get("orgao", ""),
        row.get("banca_guess", ""),
    ]))
    if "la salle" in blob or "lasalle" in blob or "fundacaolasalle" in blob:
        return True
    has_ache_edital_signal = (
        bool(split_urls(row.get("ache_attachment_pages")))
        or bool(split_urls(row.get("ache_attachment_pdfs")))
        or "edital concurso" in blob
        or "edital divulgado" in blob
        or "edital processo seletivo" in blob
        or "dois editais" in blob
        or "2 editais" in blob
        or "editais" in blob
    )
    if not has_ache_edital_signal:
        return False
    return "concurso" in blob or "processo seletivo" in blob or "edital" in blob


def lasalle_urls_from_row(row: Dict[str, str]) -> List[str]:
    urls: List[str] = []
    fields = [
        "official_base_url",
        "edital_pagina",
        "fase2d_base_url",
        "fase2d_main_doc_page_url",
        "official_source_urls",
        "official_doc_urls",
    ]
    for field in fields:
        add_unique(urls, split_urls(row.get(field)))
    filtered: List[str] = []
    for url in urls:
        low = url.lower()
        if "fundacaolasalle.org.br" not in low:
            continue
        if ache.is_noise_url(url):
            continue
        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        if path.startswith("/concurso/") or is_file_url(url) or path in {"", "/", "/concursos/"}:
            add_unique(filtered, [ache.canonical_official_url(url)])
    return filtered


def lasalle_query_text(row: Dict[str, str], city: str) -> str:
    return " ".join([
        row.get("orgao", ""),
        row.get("edital", ""),
        row.get("detalle_ache", ""),
        row.get("attachment_titles", ""),
        row.get("banca_guess", ""),
        city or "",
    ]).strip()


def lasalle_doc_rank(title: str, url: str, row: Dict[str, str]) -> Tuple[int, str]:
    blob = normalize(f"{title} {url}")
    is_pdf = url_is_pdf(url)
    expected_pair = row_edital_pair(row)
    exact_num = bool(expected_pair and expected_pair in parse_edital_nums(f"{title} {url}"))
    bad_terms = (
        "retific", "gabarito", "resultado", "homolog", "classific",
        "convoca", "inscric", "isen", "local de prova", "data hora local",
        "cronograma", "aviso", "nota", "recurso",
    )
    if not is_pdf:
        return 99, blob
    if "ed_abert" in blob or "edital de abertura" in blob:
        if not any(term in blob for term in bad_terms):
            return (0 if exact_num else 1), blob
        return 6, blob
    if "abertura" in blob and "edital" in blob and not any(term in blob for term in bad_terms):
        return (0 if exact_num else 2), blob
    if "retific" in blob:
        return 20, blob
    if any(term in blob for term in ("resultado", "homolog", "classific", "gabarito", "convoca")):
        return 30, blob
    if "edital" in blob:
        return 8 if exact_num else 12, blob
    return 60, blob


def lasalle_orgao_kind(text: str) -> str:
    blob = normalize(text or "")
    if "fundacao hospital centenario" in blob or re.search(r"\bfhc\b", blob):
        return "fundacao_hospital"
    if "camara" in blob:
        return "camara"
    if "municipio" in blob or "prefeitura" in blob:
        return "municipio"
    return ""


def lasalle_candidate_context_matches_row(row: Dict[str, str], context: str, detail_url: str = "") -> bool:
    row_blob = " ".join([
        row.get("orgao", ""),
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
    ])
    row_kind = lasalle_orgao_kind(row_blob)
    candidate_kind = lasalle_orgao_kind(f"{context} {detail_url}")
    if row_kind and candidate_kind and row_kind != candidate_kind:
        return False
    return True


def lasalle_detail_matches_row(row: Dict[str, str], city: str, detail_url: str, probe: Dict[str, object]) -> bool:
    context = " ".join([
        str(probe.get("title") or ""),
        str(probe.get("text_excerpt") or ""),
        " ".join(title for _url, title in probe.get("doc_links", [])[:12]),
        detail_url,
    ])
    if not family_matches_row(row, context):
        return False
    if not lasalle_candidate_context_matches_row(row, context, detail_url):
        return False
    if city and not city_context_matches(city, context):
        return False
    expected_pair = row_edital_pair(row)
    if expected_pair:
        return expected_pair in parse_edital_nums(context)
    return True


def lasalle_index_candidates(fetch_args: argparse.Namespace) -> List[Tuple[str, str, str]]:
    """Build a reusable La Salle candidate index from all public status tabs."""
    global LASALLE_INDEX_CACHE
    if LASALLE_INDEX_CACHE is not None:
        return LASALLE_INDEX_CACHE

    started = time.time()
    progress_log(PROGRESS_ENABLED, "LASALLE_CACHE start")
    candidates: List[Tuple[str, str, str]] = []
    seen: set[str] = set()
    index_urls = list(dict.fromkeys(
        ache.resolver_indexes_for("lasalle", "https://fundacaolasalle.org.br/concursos/")
    ))
    for index_url in index_urls:
        fetch_started = time.time()
        res = ache.fetch(index_url, fetch_args)
        parsed = 0
        if res.body:
            for candidate_url, context in ache.candidate_links_from_index(res.body, res.final_url or index_url):
                candidate_url = ache.canonical_official_url(candidate_url)
                parsed_path = urlparse(candidate_url).path.lower()
                if not parsed_path.startswith("/concurso/") or candidate_url in seen:
                    continue
                if "fundacaolasalle.org.br" not in url_domain(candidate_url) or ache.is_noise_url(candidate_url):
                    continue
                seen.add(candidate_url)
                parsed += 1
                candidates.append((candidate_url, context, index_url))
        progress_log(
            PROGRESS_ENABLED,
            "LASALLE_CACHE index seconds={seconds:.2f} status={status} parsed={parsed} url={url}".format(
                seconds=time.time() - fetch_started,
                status=getattr(res, "status", ""),
                parsed=parsed,
                url=index_url,
            ),
        )
    LASALLE_INDEX_CACHE = candidates
    progress_log(
        PROGRESS_ENABLED,
        f"LASALLE_CACHE end candidates={len(candidates)} seconds={time.time() - started:.2f}",
    )
    return candidates


def resolve_lasalle_detail_for_row(
    row: Dict[str, str],
    city: str,
    fetch_args: argparse.Namespace,
) -> Tuple[str, Dict[str, object]]:
    query_text = lasalle_query_text(row, city)
    expected_pair = row_edital_pair(row)
    edital_num = edital_label(expected_pair) if expected_pair else ""
    row_fam = row_family(row)
    candidates: List[Tuple[int, str, str]] = []
    for candidate_url, context, _index_url in lasalle_index_candidates(fetch_args):
        context_blob = f"{context} {candidate_url}"
        if not lasalle_candidate_context_matches_row(row, context_blob, candidate_url):
            continue
        score = ache.match_score(context_blob, query_text, edital_num)
        if city and city_context_matches(city, context_blob):
            score += 35
        if expected_pair and expected_pair in parse_edital_nums(context_blob):
            score += 60
        candidate_fam = context_family(context_blob)
        if row_fam and candidate_fam == row_fam:
            score += 80
        elif row_fam and candidate_fam and candidate_fam != row_fam:
            score -= 80
        row_kind = lasalle_orgao_kind(lasalle_query_text(row, city))
        candidate_kind = lasalle_orgao_kind(context_blob)
        if row_kind and candidate_kind and row_kind == candidate_kind:
            score += 25
        if score > 0 or city_context_matches(city, context_blob):
            candidates.append((score, candidate_url, context))
    if not expected_pair:
        same_city_same_kind = [
            item for item in candidates
            if city_context_matches(city, f"{item[2]} {item[1]}")
            and lasalle_candidate_context_matches_row(row, item[2], item[1])
        ]
        unique_same_city_urls = {item[1] for item in same_city_same_kind}
        if len(unique_same_city_urls) > 1:
            return "", {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _score, candidate_url, _context in candidates[:20]:
        probe = probe_official_page_stable(candidate_url, fetch_args)
        if lasalle_detail_matches_row(row, city, candidate_url, probe):
            return candidate_url, probe
    return "", {}


def enrich_lasalle_deep(
    row: Dict[str, str],
    city: str,
    fetch_args: argparse.Namespace,
    allow_discovery: bool = False,
) -> str:
    """Resolve La Salle detail pages and opening-edital PDFs through the pipeline."""
    row["fase2d_lasalle_status"] = ""
    row["fase2d_lasalle_detail_url"] = ""
    row["fase2d_lasalle_pdf"] = ""
    row["fase2d_lasalle_doc_count"] = ""
    hinted = lasalle_row_hint(row)
    forced_detail_url = (row.get("_fase2d_lasalle_forced_detail") or "").strip()
    if not hinted and not forced_detail_url and not (allow_discovery and lasalle_discovery_signal(row, city)):
        return ""

    docs = [url for url in split_urls(row.get("official_doc_urls")) if not ache.is_noise_url(url)]
    row["official_doc_urls"] = " | ".join(docs)
    row["n_official_doc_urls"] = str(len(docs))

    detail_url = ache.canonical_official_url(forced_detail_url) if forced_detail_url else ""
    for url in lasalle_urls_from_row(row):
        if detail_url:
            break
        parsed = urlparse(url)
        if parsed.path.lower().startswith("/concurso/"):
            detail_url = url
            break

    probe: Dict[str, object] = {}
    if detail_url:
        probe = probe_official_page_stable(detail_url, fetch_args)
        if not lasalle_detail_matches_row(row, city, detail_url, probe):
            detail_url = ""
            probe = {}

    if not detail_url:
        detail_url, probe = resolve_lasalle_detail_for_row(row, city, fetch_args)

    if not detail_url:
        row["fase2d_lasalle_status"] = "not_resolved"
        return "not_resolved"

    if not hinted:
        row["banca_guess"] = "lasalle"

    lasalle_context = " ".join([
        str(probe.get("title") or ""),
        str(probe.get("text_excerpt") or ""),
        " ".join(title for _url, title in probe.get("doc_links", [])[:12]),
        detail_url,
    ])
    update_metadata_from_official_context(row, lasalle_context, detail_url)

    doc_links = [
        (url, title)
        for url, title in probe.get("doc_links", [])
        if "fundacaolasalle.org.br" in url.lower() and is_file_url(url)
    ]
    if not doc_links and probe.get("text_excerpt"):
        row["fase2d_lasalle_status"] = "detail_no_docs"
    elif not doc_links:
        row["fase2d_lasalle_status"] = "detail_fetch_empty"

    doc_links.sort(key=lambda item: lasalle_doc_rank(item[1], item[0], row))
    doc_urls = [url for url, _title in doc_links]
    best_pdf = ""
    if doc_links and lasalle_doc_rank(doc_links[0][1], doc_links[0][0], row)[0] <= 2:
        best_pdf = doc_links[0][0]

    row["fase2d_lasalle_detail_url"] = detail_url
    row["fase2d_lasalle_pdf"] = best_pdf
    row["fase2d_lasalle_doc_count"] = str(len(doc_urls))
    row["fase2d_base_url"] = detail_url
    row["fase2d_base_kind"] = "lasalle_detail"
    row["fase2d_base_specific"] = "SI"
    row["fase2d_main_doc_page_url"] = detail_url
    row["fase2d_main_edital_url"] = best_pdf
    if doc_links:
        row["fase2d_main_doc_title"] = doc_links[0][1]
        row["fase2d_main_doc_type"] = "edital_abertura" if best_pdf else "documento_revisar"
        row["fase2d_main_doc_source_kind"] = "lasalle"
        pairs = parse_edital_nums(f"{doc_links[0][1]} {doc_links[0][0]}")
        if pairs:
            row["fase2d_main_doc_edital_num"] = edital_label(pairs[0])
        if best_pdf:
            update_metadata_from_official_context(row, f"{doc_links[0][1]} {best_pdf}", detail_url)

    apply_base(row, detail_url, "SI", update_core=True)
    row["edital_pagina"] = detail_url
    added = apply_doc(row, doc_urls)
    row["fase2d_added_doc_urls"] = " | ".join(added)
    if best_pdf:
        row["edital_pdf"] = best_pdf
        row["tiene_pagina_oficial"] = "SI"
        row["tiene_base_documentos"] = "SI"
        row["fase2d_lasalle_status"] = "filled_doc"
        row["fase2d_status"] = "filled_doc"
        row["fase2d_action"] = "updated_core"
        return "filled_doc"

    row["fase2d_lasalle_status"] = row["fase2d_lasalle_status"] or "detail_no_pdf"
    if not row.get("fase2d_status"):
        row["fase2d_status"] = "base_only"
        row["fase2d_action"] = "updated_base_only"
    return row["fase2d_lasalle_status"]


def row_deep_page_urls(row: Dict[str, str]) -> List[str]:
    urls: List[str] = []
    for field in ("edital_pagina", "official_base_url", "fase2d_main_doc_page_url", "official_source_urls"):
        add_unique(urls, split_urls(row.get(field)))
    out: List[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if ache.is_ache_url(url) or ache.is_noise_url(url) or is_file_url(url):
            continue
        if is_generic_base(url) and not ache.is_specific_official_url(url):
            continue
        add_unique(out, [url])
    return out


GENERIC_DOC_NOISE = (
    "organograma",
    "tutorial",
    "lgpd",
    "politica",
    "privacidade",
    "manual",
    "cronograma",
    "modelo",
    "aviso",
    "comunicado",
    "resultado",
    "homologacao",
    "homologacao",
    "gabarito",
    "convocacao",
    "retificacao",
    "retifica",
    "impugnacao",
    "isencao",
    "sorteio",
    "pericia",
    "devolucao",
    "data hora local",
    "notas",
    "prova pratica",
    "prova de titulos",
    "relatorio medico",
    "atendimento especial",
    "autodeclaracao",
    "conteudos programaticos",
    "atribuicoes",
)


def is_main_edital_signal(blob: str) -> bool:
    blob = normalize(blob)
    secondary = ("retific", "resultado", "homolog", "gabarito", "convoca", "impugnacao", "isencao")
    if any(term in blob for term in secondary) and "abertura" not in blob:
        return False
    if ("etapa 01" in blob or "ato 01" in blob) and "edital" in blob:
        return True
    if "edital" in blob and "abertura" in blob:
        return True
    if "edital" in blob and ("concurso publico" in blob or "concurso p blico" in blob or "processo seletivo" in blob):
        return True
    return any(term in blob for term in (
        "edital de abertura",
        "edital abertura",
        "editaldeabertura",
        "edital_abertura",
        "edital concurso",
        "editalconcurso",
        "primeira versao publicada",
    ))


def generic_doc_rank(row: Dict[str, str], url: str, title: str) -> Tuple[int, str]:
    raw = f"{title} {url}"
    blob = normalize(raw)
    is_pdf = url_is_pdf(url)
    expected = row_edital_pair(row)
    expected_label = edital_label(expected).replace("/", " ") if expected else ""
    exact_num = bool(expected and (normalize(expected_label) in blob or raw_has_expected_pair(row, raw)))
    noise = any(term in blob for term in GENERIC_DOC_NOISE)
    main_signal = is_main_edital_signal(blob)
    if noise and not main_signal:
        base_rank = 90
    elif is_pdf and exact_num and main_signal:
        base_rank = 0
    elif is_pdf and "edital" in blob and "abertura" in blob and "consolid" in blob:
        base_rank = 0
    elif is_pdf and main_signal:
        base_rank = 1
    elif is_pdf and "retific" in blob:
        base_rank = 30
    elif is_pdf and ("resultado" in blob or "classific" in blob or "homolog" in blob or "convoca" in blob):
        base_rank = 40
    elif is_pdf and "edital" in blob and exact_num and "extrato" not in blob:
        base_rank = 10
    elif is_pdf and "edital" in blob and "extrato" not in blob:
        base_rank = 20
    elif is_pdf:
        base_rank = 60
    elif "tipo edital" in blob or "edital" in blob:
        base_rank = 70
    else:
        base_rank = 80
    return base_rank, blob


def current_pdf_needs_repair(row: Dict[str, str]) -> bool:
    pdf = (row.get("edital_pdf") or "").strip()
    if not pdf:
        return True
    title = row.get("fase2d_main_doc_title", "")
    if row.get("fase2d_legalle_deep_status") == "filled_doc" and pdf == row.get("fase2d_legalle_pdf"):
        return False
    if row.get("fase2d_lasalle_status") == "filled_doc" and pdf == row.get("fase2d_lasalle_pdf"):
        return False
    rank, blob = generic_doc_rank(row, pdf, title)
    if rank > 5:
        return True
    url_blob = normalize(pdf)
    title_blob = normalize(title)
    bad_terms = ("aviso", "cronograma", "resultado", "homologacao", "retificacao", "gabarito", "data hora local")
    if is_main_edital_signal(url_blob) and not any(term in url_blob for term in bad_terms):
        return False
    if any(term in url_blob for term in bad_terms) and not is_main_edital_signal(url_blob):
        return True
    if any(term in title_blob for term in bad_terms) and not is_main_edital_signal(title_blob):
        return True
    return False


def fix_common_mojibake(value: str) -> str:
    text = str(value or "")
    replacements = {
        "nÃ‚Âº": "nº",
        "NÃ‚Âº": "Nº",
        "nÂº": "nº",
        "NÂº": "Nº",
        "nÃ‚Â°": "nº",
        "NÃ‚Â°": "Nº",
        "nÂ°": "nº",
        "NÂ°": "Nº",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def normalize_edital_cell(value: str) -> str:
    value = fix_common_mojibake(value)
    label = ache.detect_edital_label(value or "")
    if label:
        return clean_edital_prefix(label)
    pairs = parse_edital_nums(value or "")
    if pairs:
        return f"nº {edital_label(pairs[0])}"
    return clean_edital_prefix(value)


def clean_edital_prefix(value: str) -> str:
    text = fix_common_mojibake(value).strip()
    match = re.search(r"(?:n[º°ÂºÂ°o.]*)\s*(\d{1,4}\s*/\s*20\d{2})", text, re.I)
    if match:
        return f"nº {re.sub(r'\\s+', '', match.group(1))}"
    match = re.fullmatch(r"\s*(\d{1,4})\s*/\s*(20\d{2})\s*", text)
    if match:
        return f"nº {int(match.group(1)):02d}/{match.group(2)}"
    return text


def row_pair_from_label(value: str) -> Optional[Tuple[int, int]]:
    pairs = parse_edital_nums(value or "")
    return pairs[0] if pairs else None


def row_edital_is_suspicious(row: Dict[str, str]) -> bool:
    value = (row.get("edital") or "").strip()
    if not value:
        return True
    normalized = clean_edital_prefix(value)
    if normalized != value:
        return True
    pair = row_pair_from_label(value)
    if not pair:
        return True
    num, year = pair
    if year < CURRENT_YEAR - 1:
        return True
    # Publication/control numbers from the portal can be large (e.g. 783/2026)
    # and are not the certame edital number shown on the official contest page.
    return num > 99


def raw_has_expected_pair(row: Dict[str, str], raw: str) -> bool:
    expected = row_edital_pair(row)
    if not expected:
        return False
    if expected in parse_edital_nums(raw or ""):
        return True
    num, year = expected
    num_pattern = rf"0*{num}\b"
    year_pattern = rf"\b{year}\b"
    patterns = [
        rf"\bedital\s*(?:n[ÂºÂ°o.]*)?\s*{num_pattern}.*?{year_pattern}",
        rf"\b(?:ato|etapa)\s*0?1\b.*?\bedital\b.*?{num_pattern}.*?{year_pattern}",
    ]
    return any(re.search(pattern, raw or "", re.I | re.S) for pattern in patterns)


def detect_official_edital_label(context: str, page_url: str = "") -> str:
    parsed = urlparse(page_url or "")
    for pattern in (
        r"/editais/0*(\d{1,4})-(20\d{2})(?:/|$)",
        r"/edital(?:-|_)?0*(\d{1,4})-(20\d{2})(?:/|$)",
    ):
        match = re.search(pattern, parsed.path, re.I)
        if match:
            return f"nº {int(match.group(1)):02d}/{match.group(2)}"

    head = ache.clean_text(context or "")[:2500]
    opening_edital = re.search(
        r"\bEdital\s*(?:n[^\d]{0,8})?\s*(\d{1,4})\s*/\s*(20\d{2})[^.]{0,100}\bAbertura\b",
        head,
        re.I,
    )
    if opening_edital:
        year = int(opening_edital.group(2))
        if year >= CURRENT_YEAR - 1:
            return f"nº {int(opening_edital.group(1)):02d}/{year}"
    for pattern in (
        r"\b(?:Concurso\s+P[uú]blico|Processo\s+Seletivo(?:\s+Simplificado)?)[^0-9]{0,80}(?:n[º°o.]*)?\s*(\d{1,4})\s*/\s*(20\d{2})",
        r"\bEdital\s*:\s*(\d{1,4})\s*/\s*(20\d{2})",
        r"^\s*(\d{1,4})\s*/\s*(20\d{2})\b",
        r"\bEdital\s+de\s+abertura[^0-9]{0,80}(?:n[º°o.]*)?\s*(\d{1,4})\s*/\s*(20\d{2})",
    ):
        match = re.search(pattern, head, re.I)
        if match:
            year = int(match.group(2))
            if year >= CURRENT_YEAR - 1:
                return f"nº {int(match.group(1)):02d}/{year}"

    candidate = ache.detect_edital_label(head)
    pair = row_pair_from_label(candidate)
    if pair and pair[1] >= CURRENT_YEAR - 1:
        return f"nº {edital_label(pair)}"
    return ""


def orgao_from_official_context(context: str) -> str:
    found = ache.detect_orgao(context)
    if found:
        return found
    cleaned = ache.clean_text(context)
    cleaned = re.sub(r"^.*Fundatec Concursos\s*:\.\s*", "", cleaned, flags=re.I)
    patterns = [
        r"(?P<org>.+?)\s+Concurso\s+P[uú]blico\s+n[º°o.]?\s*\d{1,4}\s*/\s*20\d{2}",
        r"(?P<org>.+?)\s+Processo\s+Seletivo(?:\s+Simplificado)?\s+\d{1,4}\s*/\s*20\d{2}",
        r"(?P<org>.+?)\s+Processo\s+Seletivo(?:\s+Simplificado)?\s+n[º°o.]?\s*\d{1,4}\s*/\s*20\d{2}",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.I)
        if not match:
            continue
        org = match.group("org").strip(" -:;,.")
        org = re.sub(r"^[A-Z]{2,6}\s*-\s*", "", org).strip()
        if 4 <= len(org) <= 140:
            return org
    return ""


def weak_orgao(value: str) -> bool:
    norm = normalize(value or "")
    if not norm:
        return True
    if "menu menu" in norm or " a cidade arrow" in norm:
        return True
    return norm in {
        "prefeitura",
        "prefeitura gaucha",
        "prefeitura gaucho",
        "prefeitura rs",
        "prefeitura do rs",
        "prefeitura do rio grande do sul",
        "prefeitura do estado do rio grande do sul",
        "prefeitura municipal",
        "prefeitura municipal rs",
        "conselho profissional",
        "crq",
        "pgm",
        "crp rs",
        "crefito",
        "badesul",
        "consorcio consisa rs",
        "consisa rs",
        "alrs",
    }


def orgao_from_attachment_title(row: Dict[str, str]) -> str:
    text = ache.clean_text(" ".join([
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
    ]))
    patterns = [
        r"((?:Prefeitura|Prefeitura\s+Municipal)\s+(?:de|do|da)?\s*[A-Za-zÀ-ÿ'´` -]{2,80}?)-\s*RS\b",
        r"((?:Câmara|Camara|Câmara\s+Municipal|Camara\s+Municipal)\s+(?:de|do|da)?\s*[A-Za-zÀ-ÿ'´` -]{2,80}?)-\s*RS\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        org = re.sub(r"\s+", " ", match.group(1)).strip(" -:;,.")
        org = re.sub(r"^(?:Edital|Concurso|Processo\s+Seletivo)\s+", "", org, flags=re.I).strip()
        if 4 <= len(org) <= 100:
            return org + "-RS"
    return ""


def update_orgao_from_ache_attachment(row: Dict[str, str]) -> None:
    if not weak_orgao(row.get("orgao", "")):
        return
    candidate = orgao_from_attachment_title(row)
    if candidate:
        row["orgao"] = candidate


TITLE_LOWER_WORDS = {"a", "as", "ao", "aos", "da", "das", "de", "do", "dos", "e", "em", "na", "nas", "no", "nos", "para", "por"}


def titlecase_orgao(value: str) -> str:
    text = fix_common_mojibake(ache.clean_text(value))
    text = re.sub(r"\s+-\s*RS\b", "-RS", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -:;,.")
    if not text:
        return ""

    has_rs_suffix = bool(re.search(r"-RS$", text, re.I))
    if has_rs_suffix:
        text = re.sub(r"-RS$", "", text, flags=re.I).rstrip(" -")

    words: List[str] = []
    for idx, word in enumerate(text.split()):
        clean = word.strip()
        if not clean:
            continue
        if clean == "-":
            words.append("-")
            continue
        norm = normalize(clean)
        if idx > 0 and norm in TITLE_LOWER_WORDS:
            words.append(norm)
            continue
        if re.fullmatch(r"S\.?A\.?", clean, re.I):
            words.append("S.A.")
            continue
        if re.fullmatch(r"\d+[ªº]?", clean):
            words.append(clean.lower())
            continue
        parts = []
        for part in clean.split("-"):
            if not part:
                continue
            part_norm = normalize(part)
            if part_norm in TITLE_LOWER_WORDS:
                parts.append(part_norm)
            else:
                parts.append(part[:1].upper() + part[1:].lower())
        words.append("-".join(parts))

    out = " ".join(words)
    out = re.sub(r"\s+/\s*", "/", out)
    out = re.sub(r"\s+-\s*", " - ", out)
    out = out.replace(" - Rs", "-RS")
    out = re.sub(r"/rs\b", "/RS", out, flags=re.I)
    out = out.replace("Riosaúde", "RioSaúde")
    if has_rs_suffix and not out.endswith("-RS"):
        out = f"{out}-RS"
    return out.strip()


def known_orgao_from_context(row: Dict[str, str]) -> str:
    blob_raw = " ".join([
        row.get("orgao", ""),
        row.get("attachment_titles", ""),
        row.get("detalle_ache", ""),
        row.get("edital_pagina", ""),
        row.get("official_base_url", ""),
    ])
    blob = normalize(blob_raw)
    current = normalize(row.get("orgao", ""))

    if current == "pgm" or "pgm porto alegre" in blob:
        return "Procuradoria-Geral do Município de Porto Alegre-RS"
    if current == "crq" or re.search(r"\bcrq(?:\s|-)?rs\b", blob):
        return "Conselho Regional de Química da 5ª Região"
    if "crp-rs" in blob_raw.lower() or "crp rs" in blob:
        return "Conselho Regional de Psicologia do Rio Grande do Sul"
    if "crefito" in blob:
        return "Conselho Regional de Fisioterapia e Terapia Ocupacional da 5ª Região"
    if "consisa" in blob:
        return "Consórcio Intermunicipal de Serviços do Vale do Taquari - Lajeado/RS"
    if "fenac" in blob:
        return "Feiras e Empreendimentos Turísticos de Novo Hamburgo S.A."
    if "codepas" in blob:
        return "Companhia de Desenvolvimento de Passo Fundo-RS"
    if "badesul" in blob:
        return "Badesul Desenvolvimento S.A. - Agência de Fomento/RS"
    if "dmae porto alegre" in blob or current == "departamento municipal de agua e esgoto":
        return "Departamento Municipal de Água e Esgotos de Porto Alegre-RS"
    if "ghc-rs" in blob or "ghc rs" in blob:
        return "Grupo Hospitalar Conceição-RS"
    if "riosaude" in blob or "rio saude" in blob:
        return "Empresa Pública de Saúde do Rio de Janeiro - RioSaúde"
    if "sanep pelotas" in blob:
        return "Serviço Autônomo de Saneamento de Pelotas-RS"
    if "ufsm" in blob:
        return "Universidade Federal de Santa Maria-RS"
    if "ufrgs" in blob:
        return "Universidade Federal do Rio Grande do Sul"
    if "unipampa" in blob:
        return "Universidade Federal do Pampa-RS"
    if current == "alrs" or "assembleia legislativa-rs" in blob_raw.lower() or "assembleia legislativa rs" in blob:
        return "Assembleia Legislativa do Estado do Rio Grande do Sul"
    if "brigada-rs" in blob or "brigada rs" in blob:
        return "Brigada Militar do Estado do Rio Grande do Sul"
    return ""


def cleanup_prefeitura_camara_name(value: str, row: Dict[str, str]) -> str:
    text = fix_common_mojibake(ache.clean_text(value))
    text = re.sub(r"\s+-\s*RS\b", "-RS", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -:;,.")
    if not text:
        return ""

    # Cut article-style tails accidentally captured as part of the orgao.
    text = re.sub(r"(-RS)\b.*$", r"\1", text, flags=re.I)
    text = re.sub(
        r"\s+\b(?:anuncia|abre|abrem|abriu|abriram|abrir|oferece|oferecem|publica|publicou|divulga|divulgou|lança|lanca|tem|vai)\b.*$",
        "",
        text,
        flags=re.I,
    ).strip(" -:;,.")

    text = re.sub(r"^Prefeitura\s+Edital\s+", "Prefeitura de ", text, flags=re.I)
    text = re.sub(r"^Câmara\s+Edital\s+", "Câmara de ", text, flags=re.I)
    text = re.sub(r"^Camara\s+Edital\s+", "Câmara de ", text, flags=re.I)

    if re.match(r"^Prefeitura\s+(?!Municipal\b|de\b|do\b|da\b|dos\b|das\b)", text, re.I):
        text = re.sub(r"^Prefeitura\s+", "Prefeitura de ", text, flags=re.I)
    if re.match(r"^(?:Câmara|Camara)\s+(?!Municipal\b|de\b|do\b|da\b|dos\b|das\b)", text, re.I):
        text = re.sub(r"^(?:Câmara|Camara)\s+", "Câmara de ", text, flags=re.I)

    if re.match(r"^(?:Prefeitura|Prefeitura Municipal|Câmara|Camara|Câmara Municipal|Camara Municipal)\b", text, re.I):
        if not re.search(r"-RS$", text, re.I):
            text = f"{text}-RS"

    return titlecase_orgao(text)


def normalize_orgao_cell(row: Dict[str, str]) -> None:
    candidate_from_known = known_orgao_from_context(row)
    candidate_from_attachment = orgao_from_attachment_title(row)
    current = row.get("orgao", "")

    if candidate_from_known:
        current = candidate_from_known
    elif candidate_from_attachment and weak_orgao(current):
        current = candidate_from_attachment

    if weak_orgao(current) and candidate_from_attachment:
        current = candidate_from_attachment

    current = cleanup_prefeitura_camara_name(current, row)
    if current:
        row["orgao"] = current


def finalize_row_text(row: Dict[str, str]) -> None:
    normalize_orgao_cell(row)
    row["edital"] = normalize_edital_cell(row.get("edital", ""))
    populate_tipo(row)
    for field in ("fase2d_expected_edital_num", "fase2d_main_doc_edital_num"):
        if row.get(field):
            row[field] = clean_edital_prefix(row[field]).replace("nº ", "")


def update_metadata_from_official_context(row: Dict[str, str], context: str, page_url: str = "") -> None:
    candidate_edital = detect_official_edital_label(context, page_url)
    if candidate_edital:
        row["edital"] = candidate_edital
    elif row.get("edital"):
        row["edital"] = normalize_edital_cell(row.get("edital", ""))

    current_org = (row.get("orgao") or "").strip()
    if weak_orgao(current_org):
        candidate_org = orgao_from_official_context(context)
        if candidate_org:
            row["orgao"] = candidate_org
    context_tipo = tipo_from_family(context_family(context))
    if context_tipo:
        row[TIPO_FIELD] = context_tipo


def enrich_existing_doc_page_deep(row: Dict[str, str], fetch_args: argparse.Namespace) -> str:
    row["fase2d_depth_status"] = ""
    row["fase2d_depth_page_url"] = ""
    row["fase2d_depth_pdf"] = ""
    row["fase2d_depth_doc_count"] = ""
    needs_repair = current_pdf_needs_repair(row)
    needs_metadata = (
        not (row.get("orgao") or "").strip()
        or normalize(row.get("orgao", "")) in {"conselho profissional", "crq", "alrs"}
        or clean_edital_prefix(row.get("edital", "")) != (row.get("edital", "") or "").strip()
        or row_edital_is_suspicious(row)
        or bool(row_edital_pair(row) and row_edital_pair(row)[1] < CURRENT_YEAR - 1)
    )
    if not needs_repair and not needs_metadata:
        return ""
    if legalle_row_hint(row) or lasalle_row_hint(row):
        return ""

    best_page = ""
    best_docs: List[Tuple[str, str]] = []
    for page_url in row_deep_page_urls(row):
        probe = probe_official_page_stable(page_url, fetch_args)
        doc_links = [
            (url, title) for url, title in probe.get("doc_links", [])
            if url and not ache.is_noise_url(url) and not ache.is_ache_url(url)
        ]
        context = " ".join([
            str(probe.get("title") or ""),
            str(probe.get("text_excerpt") or ""),
            " ".join(f"{title} {url}" for url, title in doc_links[:20]),
        ])
        update_metadata_from_official_context(row, context, page_url)
        if not doc_links:
            continue
        doc_links.sort(key=lambda item: generic_doc_rank(row, item[0], item[1]))
        if not best_docs or generic_doc_rank(row, doc_links[0][0], doc_links[0][1]) < generic_doc_rank(row, best_docs[0][0], best_docs[0][1]):
            best_page = page_url
            best_docs = doc_links

    if not best_docs:
        row["fase2d_depth_status"] = "no_docs"
        return "no_docs"

    pdf_docs = [item for item in best_docs if url_is_pdf(item[0])]
    best_pdf = ""
    best_title = ""
    if pdf_docs and generic_doc_rank(row, pdf_docs[0][0], pdf_docs[0][1])[0] <= 5:
        best_pdf, best_title = pdf_docs[0]
    row["fase2d_depth_page_url"] = best_page
    row["fase2d_depth_pdf"] = best_pdf
    row["fase2d_depth_doc_count"] = str(len(best_docs))
    if best_page:
        apply_base(row, best_page, "SI", update_core=True)
        row["edital_pagina"] = best_page
    added = apply_doc(row, [url for url, _title in best_docs])
    if added:
        row["fase2d_added_doc_urls"] = " | ".join(split_urls(row.get("fase2d_added_doc_urls")) + [u for u in added if u not in split_urls(row.get("fase2d_added_doc_urls"))])
    if best_pdf:
        row["edital_pdf"] = best_pdf
        row["fase2d_main_doc_title"] = best_title
        row["fase2d_main_doc_type"] = "edital_abertura"
        row["fase2d_main_edital_url"] = best_pdf
        row["fase2d_main_doc_page_url"] = best_page
        row["tiene_pagina_oficial"] = "SI"
        row["tiene_base_documentos"] = "SI"
        row["fase2d_depth_status"] = "filled_doc"
        if not row.get("fase2d_status") or row.get("fase2d_status") in {"", "not_municipal_scope", "no_city", "base_only", "review_candidate"}:
            row["fase2d_status"] = "filled_doc"
            row["fase2d_action"] = "updated_core"
        return "filled_doc"
    row["fase2d_depth_status"] = "docs_no_pdf"
    return "docs_no_pdf"


def compute_semaforo(row: Dict[str, str]) -> str:
    """Estado humano para revisar el Excel.

    - listo: tenemos link de edital directo y confiable.
    - revisar: hay base/candidato/documentos, pero no suficiente certeza.
    - No encontrado: no hay fuente oficial util localizada.
    """
    edital_pdf = (row.get("edital_pdf") or "").strip()
    main_url = (row.get("fase2d_main_edital_url") or "").strip()
    fase2d_status = (row.get("fase2d_status") or "").strip()
    official_base = (row.get("official_base_url") or "").strip()
    official_docs = split_urls(row.get("official_doc_urls"))
    has_official = (row.get("tiene_pagina_oficial") or "").strip().upper() == "SI"

    if edital_pdf:
        return "revisar" if current_pdf_needs_repair(row) else "listo"
    if fase2d_status in {"filled_doc", "matched_existing"} and main_url:
        return "listo"
    if (
        "review" in fase2d_status
        or fase2d_status == "base_only"
        or official_base
        or official_docs
        or main_url
        or has_official
    ):
        return "revisar"
    return "No encontrado"


def recompute_doc_columns(rows: List[Dict[str, str]], base_fields: Sequence[str]) -> List[str]:
    max_docs = 0
    for row in rows:
        for key in list(row.keys()):
            if key.startswith("_"):
                row.pop(key, None)
        docs = split_urls(row.get("official_doc_urls"))
        max_docs = max(max_docs, len(docs))
        for key in list(row.keys()):
            if key.startswith("official_doc_") and key[len("official_doc_"):].isdigit():
                row.pop(key, None)
        for idx, url in enumerate(docs, start=1):
            row[f"official_doc_{idx}"] = url

    fields = [f for f in base_fields if not (f.startswith("official_doc_") and f[len("official_doc_"):].isdigit())]
    for idx in range(1, max_docs + 1):
        fields.append(f"official_doc_{idx}")
    for row in rows:
        for idx in range(1, max_docs + 1):
            row.setdefault(f"official_doc_{idx}", "")
    return fields


def progress_log(enabled: bool, message: str) -> None:
    if enabled:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        print(line, flush=True)
        if PROGRESS_LOG_PATH:
            PROGRESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PROGRESS_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


def install_fetch_logger(enabled: bool) -> None:
    if not enabled or getattr(ache.fetch, "_fase2d_logged", False):
        return
    original_fetch = ache.fetch

    def logged_fetch(url: str, args: argparse.Namespace):
        start = time.time()
        progress_log(True, f"    FETCH n={PROGRESS_ROW_N or '-'} start url={url}")
        try:
            result = original_fetch(url, args)
            progress_log(
                True,
                "    FETCH n={row_n} end status={status} seconds={seconds:.2f} final={final} url={url}".format(
                    row_n=PROGRESS_ROW_N or "-",
                    status=getattr(result, "status", ""),
                    seconds=time.time() - start,
                    final=(getattr(result, "final_url", "") or "")[:160],
                    url=url,
                ),
            )
            return result
        except Exception as exc:
            progress_log(
                True,
                f"    FETCH n={PROGRESS_ROW_N or '-'} error seconds={time.time() - start:.2f} url={url} error={type(exc).__name__}: {exc}",
            )
            raise

    logged_fetch._fase2d_logged = True  # type: ignore[attr-defined]
    ache.fetch = logged_fetch  # type: ignore[assignment]


def maybe_run_ache_fallback(
    row: Dict[str, str],
    stats: Dict[str, int],
    fetch_args: argparse.Namespace,
    should_log: bool,
    row_n: str,
) -> str:
    started = time.time()
    progress_log(should_log, f"  n={row_n} stage=ache_fallback_late start")
    status = enrich_from_ache_detail(row, fetch_args)
    if status == "ache_official":
        stats["ache_fallback_used"] += 1
    progress_log(
        should_log,
        f"  n={row_n} stage=ache_fallback_late end status={status or '-'} seconds={time.time() - started:.2f}",
    )
    return status


def maybe_run_legalle_discovery(
    row: Dict[str, str],
    stats: Dict[str, int],
    fetch_args: argparse.Namespace,
    should_log: bool,
    row_n: str,
    city: str,
) -> str:
    if legalle_row_hint(row) or not legalle_discovery_signal(row, city):
        return ""
    started = time.time()
    progress_log(should_log, f"  n={row_n} stage=legalle_discovery_late start")
    status = enrich_legalle_deep(row, city, fetch_args, allow_discovery=True)
    if status == "filled_doc":
        stats["legalle_deep_filled"] += 1
        stats["legalle_discovery_filled"] += 1
    progress_log(
        should_log,
        f"  n={row_n} stage=legalle_discovery_late end status={status or '-'} seconds={time.time() - started:.2f}",
    )
    return status


def maybe_run_lasalle_discovery(
    row: Dict[str, str],
    stats: Dict[str, int],
    fetch_args: argparse.Namespace,
    should_log: bool,
    row_n: str,
    city: str,
) -> str:
    if lasalle_row_hint(row) or not lasalle_discovery_signal(row, city):
        return ""
    started = time.time()
    progress_log(should_log, f"  n={row_n} stage=lasalle_discovery_late start")
    status = enrich_lasalle_deep(row, city, fetch_args, allow_discovery=True)
    if status == "filled_doc":
        stats["lasalle_deep_filled"] += 1
        stats["lasalle_discovery_filled"] += 1
    progress_log(
        should_log,
        f"  n={row_n} stage=lasalle_discovery_late end status={status or '-'} seconds={time.time() - started:.2f}",
    )
    return status


def assign_lasalle_sibling_candidates(
    rows: List[Dict[str, str]],
    site_rows: List[Dict[str, str]],
    fetch_args: argparse.Namespace,
) -> int:
    """Pre-assign La Salle rows when sibling rows split a city's editais.

    Example: one São Leopoldo row has "02-2026" and another older Ache row has
    no explicit number. La Salle lists both 01/2026 and 02/2026. After assigning
    02/2026 to the explicit row, the remaining municipal candidate can safely
    fill the no-number sibling.
    """
    prepared: List[Dict[str, object]] = []
    for row in rows:
        update_orgao_from_ache_attachment(row)
        finalize_row_text(row)
        city, _slug, _method = find_city(row, site_rows)
        if not city:
            continue
        if not (lasalle_row_hint(row) or lasalle_discovery_signal(row, city)):
            continue
        kind = lasalle_orgao_kind(lasalle_query_text(row, city)) or "generic"
        prepared.append({
            "row": row,
            "city": city,
            "kind": kind,
            "pair": row_edital_pair(row),
        })

    by_group: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
    for item in prepared:
        by_group.setdefault((normalize(str(item["city"])), str(item["kind"])), []).append(item)

    assigned_count = 0
    all_candidates = lasalle_index_candidates(fetch_args)
    for (city_norm, kind), group in by_group.items():
        if len(group) < 2:
            continue
        city = str(group[0]["city"])
        candidates: List[Dict[str, object]] = []
        seen_urls: set[str] = set()
        for url, context, _index_url in all_candidates:
            context_blob = f"{context} {url}"
            if url in seen_urls:
                continue
            if not city_context_matches(city, context_blob):
                continue
            candidate_kind = lasalle_orgao_kind(context_blob) or "generic"
            if kind != "generic" and candidate_kind != "generic" and candidate_kind != kind:
                continue
            pairs = parse_edital_nums(context_blob)
            seen_urls.add(url)
            candidates.append({"url": url, "context": context, "pairs": pairs})
        if len(candidates) < 2:
            continue

        assigned_urls: set[str] = set()
        for item in group:
            row = item["row"]
            pair = item["pair"]
            if not pair:
                continue
            exact = [cand for cand in candidates if pair in cand["pairs"]]
            if len(exact) != 1:
                continue
            url = str(exact[0]["url"])
            row["_fase2d_lasalle_forced_detail"] = url
            row["_fase2d_lasalle_forced_reason"] = "sibling_exact_edital"
            assigned_urls.add(url)
            assigned_count += 1

        remaining_rows = [
            item for item in group
            if not item["pair"] and not (item["row"].get("_fase2d_lasalle_forced_detail") or "")
        ]
        remaining_candidates = [cand for cand in candidates if str(cand["url"]) not in assigned_urls]
        if len(remaining_rows) == 1 and len(remaining_candidates) == 1:
            row = remaining_rows[0]["row"]
            candidate = remaining_candidates[0]
            row["_fase2d_lasalle_forced_detail"] = str(candidate["url"])
            row["_fase2d_lasalle_forced_reason"] = "sibling_remaining_edital"
            pairs = candidate.get("pairs") or []
            if pairs and not row.get("edital"):
                row["edital"] = f"nÂº {edital_label(pairs[0])}"
            assigned_count += 1
    return assigned_count


def multi_edital_signal(row: Dict[str, str]) -> bool:
    blob = normalize(" ".join([
        row.get("orgao", ""),
        row.get("edital", ""),
        row.get("detalle_ache", ""),
        row.get("attachment_titles", ""),
    ]))
    return any(term in blob for term in (
        "dois editais",
        "2 editais",
        "dois concursos",
        "duas selecoes",
        "duas selecoes",
        "lanca dois",
        "lanca 2",
        "lan a dois",
    ))


def probe_context(probe: Dict[str, object], detail_url: str, max_links: int = 14) -> str:
    return " ".join([
        str(probe.get("title") or ""),
        str(probe.get("text_excerpt") or ""),
        " ".join(title for _url, title in probe.get("doc_links", [])[:max_links]),
        detail_url,
    ])


def legalle_best_opening_pdf(row: Dict[str, str], probe: Dict[str, object]) -> str:
    doc_links = [
        (url, title)
        for url, title in probe.get("doc_links", [])
        if ache.is_legalle_doc_url(url)
    ]
    doc_links.sort(key=lambda item: ache.legalle_doc_rank(item[1], item[0]))
    if doc_links and ache.legalle_doc_rank(doc_links[0][1], doc_links[0][0])[0] <= 2:
        return doc_links[0][0]
    return ""


def lasalle_best_opening_pdf(row: Dict[str, str], probe: Dict[str, object]) -> str:
    doc_links = [
        (url, title)
        for url, title in probe.get("doc_links", [])
        if "fundacaolasalle.org.br" in url.lower() and is_file_url(url)
    ]
    doc_links.sort(key=lambda item: lasalle_doc_rank(item[1], item[0], row))
    if doc_links and lasalle_doc_rank(doc_links[0][1], doc_links[0][0], row)[0] <= 2:
        return doc_links[0][0]
    return ""


def orgao_from_bank_context(context: str, city: str) -> str:
    cleaned = ache.clean_text(context or "")
    city_clean = city.strip()
    if not city_clean:
        return ""
    patterns = [
        r"INFORMAÇÕES\s+(?:Concurso\s+Público|Concurso\s+Publico|Processo\s+Seletivo(?:\s+Simplificado|\s+Público|\s+Publico)?)\s+\d{1,4}/20\d{2}\s*-\s*(?P<tail>.+?)\s+Prefeitura\s+Municipal\s+de\s+" + re.escape(city_clean),
        r"INFORMAÇÕES\s+(?:Concurso\s+Público|Concurso\s+Publico|Processo\s+Seletivo(?:\s+Simplificado|\s+Público|\s+Publico)?)\s+\d{1,4}/20\d{2}\s+(?P<tail>Prefeitura\s+Municipal\s+de\s+" + re.escape(city_clean) + r")",
        r"(?P<tail>.+?)\s+(?:Concurso\s+Público|Concurso\s+Publico|Processo\s+Seletivo).*?Edital\s+de\s+Abertura",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.I)
        if not match:
            continue
        tail = match.group("tail").strip(" -:;,.")
        if not tail:
            continue
        if normalize(tail) in {"prefeitura", "prefeitura municipal"} or "prefeitura municipal de" in normalize(tail):
            return f"Prefeitura de {city_clean}-RS"
        if normalize(tail) == "prefeitura":
            return f"Prefeitura de {city_clean}-RS"
        return titlecase_orgao(tail)
    if re.search(r"Prefeitura\s+Municipal\s+de\s+" + re.escape(city_clean), cleaned, re.I):
        return f"Prefeitura de {city_clean}-RS"
    return ""


def bank_split_candidates(
    row: Dict[str, str],
    city: str,
    fetch_args: argparse.Namespace,
) -> List[Dict[str, str]]:
    row_fam = row_family(row)
    expected_pair = row_edital_pair(row)
    matches: List[Dict[str, str]] = []

    def accept_candidate(bank: str, detail_url: str, probe: Dict[str, object], pdf: str) -> None:
        if not pdf:
            return
        context = probe_context(probe, detail_url)
        if city and not city_context_matches(city, context):
            return
        if not family_matches_row(row, context):
            return
        if expected_pair and expected_pair not in parse_edital_nums(context):
            return
        orgao = orgao_from_bank_context(context, city) or row.get("orgao", "")
        edital = detect_official_edital_label(context, detail_url) or row.get("edital", "")
        key = f"{bank}|{detail_url}"
        if any(item["key"] == key for item in matches):
            return
        matches.append({
            "key": key,
            "bank": bank,
            "detail_url": detail_url,
            "pdf": pdf,
            "orgao": orgao,
            "edital": edital,
            "family": context_family(context) or row_fam,
        })

    if legalle_discovery_signal(row, city) or legalle_row_hint(row) or multi_edital_signal(row):
        for detail_url, index_context, _index_url in legalle_index_candidates(fetch_args):
            index_blob = f"{index_context} {detail_url}"
            if city and not city_context_matches(city, index_blob):
                continue
            index_fam = context_family(index_blob)
            if row_fam and index_fam and index_fam != row_fam:
                continue
            probe = probe_official_page_stable(detail_url, fetch_args)
            accept_candidate("legalle", detail_url, probe, legalle_best_opening_pdf(row, probe))

    if lasalle_discovery_signal(row, city) or lasalle_row_hint(row) or multi_edital_signal(row):
        for detail_url, index_context, _index_url in lasalle_index_candidates(fetch_args):
            index_blob = f"{index_context} {detail_url}"
            if city and not city_context_matches(city, index_blob):
                continue
            index_fam = context_family(index_blob)
            if row_fam and index_fam and index_fam != row_fam:
                continue
            probe = probe_official_page_stable(detail_url, fetch_args)
            accept_candidate("lasalle", detail_url, probe, lasalle_best_opening_pdf(row, probe))

    # Keep only coherent families when the source row has one.
    if row_fam:
        matches = [item for item in matches if not item.get("family") or item["family"] == row_fam]
    matches.sort(key=lambda item: (item.get("orgao", ""), item.get("detail_url", "")))
    return matches


def expand_bank_multi_edital_rows(
    rows: List[Dict[str, str]],
    site_rows: List[Dict[str, str]],
    fetch_args: argparse.Namespace,
) -> Tuple[List[Dict[str, str]], int]:
    out: List[Dict[str, str]] = []
    added = 0
    for row in rows:
        update_orgao_from_ache_attachment(row)
        finalize_row_text(row)
        if not multi_edital_signal(row):
            out.append(row)
            continue
        city, _slug, _method = find_city(row, site_rows)
        if not city:
            out.append(row)
            continue
        matches = bank_split_candidates(row, city, fetch_args)
        if len(matches) < 2 or len(matches) > 4:
            out.append(row)
            continue
        original_n = (row.get("n") or str(len(out) + 1)).strip()
        for idx, match in enumerate(matches, start=1):
            split_row = dict(row)
            split_row["n"] = f"{original_n}.{idx}"
            split_row["_fase2d_original_n"] = original_n
            split_row["_fase2d_bank_split"] = "SI"
            split_row["_fase2d_bank_split_index"] = str(idx)
            split_row["_fase2d_bank_split_count"] = str(len(matches))
            split_row["banca_guess"] = match["bank"]
            split_row["orgao"] = match.get("orgao") or split_row.get("orgao", "")
            if match.get("edital"):
                split_row["edital"] = match["edital"]
            if match["bank"] == "legalle":
                split_row["_fase2d_legalle_forced_detail"] = match["detail_url"]
                split_row["_fase2d_legalle_forced_reason"] = "bank_multi_edital_split"
            elif match["bank"] == "lasalle":
                split_row["_fase2d_lasalle_forced_detail"] = match["detail_url"]
                split_row["_fase2d_lasalle_forced_reason"] = "bank_multi_edital_split"
            split_row["official_base_url"] = match["detail_url"]
            split_row["edital_pagina"] = match["detail_url"]
            split_row["edital_pdf"] = ""
            populate_tipo(split_row)
            out.append(split_row)
        added += len(matches) - 1
    return out, added


def integrate_rows(
    ache_rows: List[Dict[str, str]],
    site_rows: List[Dict[str, str]],
    docs_rows: List[Dict[str, str]],
    debug_progress: bool = False,
    progress_every: int = 1,
    fetch_timeout: float = 30,
    delay_min: float = 0.05,
    delay_max: float = 0.15,
    ache_fallback_timeout: float = 2.5,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    global PROGRESS_ENABLED, PROGRESS_ROW_N
    PROGRESS_ENABLED = debug_progress
    sites_by_slug = {row.get("municipio_slug", ""): row for row in site_rows if row.get("municipio_slug")}
    docs_by_slug: Dict[str, List[Dict[str, str]]] = {}
    for idx, doc in enumerate(docs_rows):
        doc["_fase2d_order"] = str(idx)
        slug = doc.get("municipio_slug", "")
        if slug:
            docs_by_slug.setdefault(slug, []).append(doc)

    stats = {
        "total": len(ache_rows),
        "rows_with_city": 0,
        "base_only": 0,
        "filled_doc": 0,
        "enriched_existing": 0,
        "review_candidate": 0,
        "not_municipal_scope": 0,
        "no_city": 0,
        "no_municipal_base": 0,
        "no_municipal_docs": 0,
        "no_confident_doc": 0,
        "official_before": 0,
        "official_after": 0,
        "edital_pdf_before": 0,
        "edital_pdf_after": 0,
        "legalle_deep_filled": 0,
        "legalle_discovery_filled": 0,
        "lasalle_deep_filled": 0,
        "lasalle_discovery_filled": 0,
        "lasalle_sibling_assigned": 0,
        "depth_page_filled": 0,
        "ache_fallback_used": 0,
        "cebraspe_fixed": 0,
        "semaforo_listo": 0,
        "semaforo_revisar": 0,
        "semaforo_no_encontrado": 0,
    }

    stats["official_before"] = sum(1 for r in ache_rows if (r.get("tiene_pagina_oficial") or "").upper() == "SI")
    stats["edital_pdf_before"] = sum(1 for r in ache_rows if (r.get("edital_pdf") or "").strip())
    fetch_args = make_fetch_args(timeout=fetch_timeout, delay_min=delay_min, delay_max=delay_max)
    setattr(fetch_args, "ache_fallback_timeout", ache_fallback_timeout)
    stats["lasalle_sibling_assigned"] = assign_lasalle_sibling_candidates(ache_rows, site_rows, fetch_args)
    progress_log(
        debug_progress,
        f"LASALLE sibling assignments={stats['lasalle_sibling_assigned']}",
    )

    total_rows = len(ache_rows)
    started_at = time.time()

    for idx, row in enumerate(ache_rows, start=1):
        row_started = time.time()
        row_n = row.get("n", str(idx))
        PROGRESS_ROW_N = row_n
        update_orgao_from_ache_attachment(row)
        finalize_row_text(row)
        should_log = debug_progress and (idx == 1 or idx == total_rows or idx % max(progress_every, 1) == 0)
        progress_log(
            should_log,
            "ROW {idx}/{total} n={n} status={status} orgao={orgao} edital={edital} banca={banca}".format(
                idx=idx,
                total=total_rows,
                n=row_n,
                status=row.get("status", ""),
                orgao=(row.get("orgao", "") or "")[:70],
                edital=row.get("edital", ""),
                banca=row.get("banca_guess", ""),
            ),
        )
        for field in FASE2D_FIELDS:
            row.setdefault(field, "")
        row["fase2d_attempted"] = "SI"
        row["fase2d_status"] = ""
        row["fase2d_action"] = ""

        stage_started = time.time()
        progress_log(should_log, f"  n={row_n} stage=cebraspe start")
        cebraspe_status = enrich_cebraspe_from_cdn(row)
        if cebraspe_status == "filled_doc":
            stats["cebraspe_fixed"] += 1
        progress_log(
            should_log,
            f"  n={row_n} stage=cebraspe end status={cebraspe_status or '-'} seconds={time.time() - stage_started:.2f}",
        )

        stage_started = time.time()
        progress_log(should_log, f"  n={row_n} stage=depth+1 start")
        depth_status = enrich_existing_doc_page_deep(row, fetch_args)
        if depth_status == "filled_doc":
            stats["depth_page_filled"] += 1
        progress_log(
            should_log,
            f"  n={row_n} stage=depth+1 end status={depth_status or '-'} seconds={time.time() - stage_started:.2f}",
        )

        stage_started = time.time()
        progress_log(should_log, f"  n={row_n} stage=scope start")
        if not row_is_municipal_scope(row):
            city, slug, method = find_city(row, site_rows)
            row["fase2d_city"] = city
            row["fase2d_city_slug"] = slug
            row["fase2d_city_match_method"] = method or "non_municipal_city_hint"
            clear_stale_municipal_base_for_non_municipal(row)
            progress_log(
                should_log,
                f"  n={row_n} stage=scope non_municipal city={city or '-'} slug={slug or '-'} method={method}",
            )
            stage_started = time.time()
            progress_log(should_log, f"  n={row_n} stage=nonmunicipal_lasalle_pre start")
            lasalle_status = enrich_lasalle_deep(
                row,
                city,
                fetch_args,
                allow_discovery=lasalle_discovery_signal(row, city),
            )
            if lasalle_status == "filled_doc":
                stats["lasalle_deep_filled"] += 1
            progress_log(
                should_log,
                f"  n={row_n} stage=nonmunicipal_lasalle_pre end status={lasalle_status or '-'} seconds={time.time() - stage_started:.2f}",
            )
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                stats["not_municipal_scope"] += 1
                progress_log(should_log, f"  n={row_n} stage=done non_municipal_banca_filled elapsed={time.time() - row_started:.1f}s")
                continue

            stage_started = time.time()
            progress_log(should_log, f"  n={row_n} stage=nonmunicipal_legalle start")
            legalle_status = enrich_legalle_deep(
                row,
                city,
                fetch_args,
                allow_discovery=legalle_discovery_signal(row, city),
            )
            if legalle_status == "filled_doc":
                stats["legalle_deep_filled"] += 1
            progress_log(
                should_log,
                f"  n={row_n} stage=nonmunicipal_legalle end status={legalle_status or '-'} seconds={time.time() - stage_started:.2f}",
            )
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                stats["not_municipal_scope"] += 1
                progress_log(should_log, f"  n={row_n} stage=done non_municipal_banca_filled elapsed={time.time() - row_started:.1f}s")
                continue

            stage_started = time.time()
            progress_log(should_log, f"  n={row_n} stage=nonmunicipal_lasalle start")
            lasalle_status = enrich_lasalle_deep(
                row,
                city,
                fetch_args,
                allow_discovery=lasalle_discovery_signal(row, city),
            )
            if lasalle_status == "filled_doc":
                stats["lasalle_deep_filled"] += 1
            progress_log(
                should_log,
                f"  n={row_n} stage=nonmunicipal_lasalle end status={lasalle_status or '-'} seconds={time.time() - stage_started:.2f}",
            )
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                stats["not_municipal_scope"] += 1
                progress_log(should_log, f"  n={row_n} stage=done non_municipal_banca_filled elapsed={time.time() - row_started:.1f}s")
                continue

            maybe_run_ache_fallback(row, stats, fetch_args, should_log, row_n)
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                stats["not_municipal_scope"] += 1
                progress_log(should_log, f"  n={row_n} stage=done non_municipal_ache_filled elapsed={time.time() - row_started:.1f}s")
                continue
            if row.get("fase2d_status") != "filled_doc":
                row["fase2d_status"] = "not_municipal_scope"
            row["fase2d_city_match_method"] = "skipped_non_municipal"
            stats["not_municipal_scope"] += 1
            progress_log(should_log, f"  n={row_n} stage=done non_municipal elapsed={time.time() - row_started:.1f}s")
            continue

        city, slug, method = find_city(row, site_rows)
        row["fase2d_city"] = city
        row["fase2d_city_slug"] = slug
        row["fase2d_city_match_method"] = method
        progress_log(
            should_log,
            f"  n={row_n} stage=scope end seconds={time.time() - stage_started:.2f}",
        )
        progress_log(should_log, f"  n={row_n} stage=city city={city or '-'} slug={slug or '-'} method={method}")
        if not slug:
            stage_started = time.time()
            progress_log(should_log, f"  n={row_n} stage=legalle_no_city start")
            legalle_status = enrich_legalle_deep(row, city, fetch_args)
            if legalle_status == "filled_doc":
                stats["legalle_deep_filled"] += 1
            progress_log(
                should_log,
                f"  n={row_n} stage=legalle_no_city end status={legalle_status or '-'} seconds={time.time() - stage_started:.2f}",
            )
            stage_started = time.time()
            progress_log(should_log, f"  n={row_n} stage=lasalle_no_city start")
            lasalle_status = enrich_lasalle_deep(row, city, fetch_args)
            if lasalle_status == "filled_doc":
                stats["lasalle_deep_filled"] += 1
            progress_log(
                should_log,
                f"  n={row_n} stage=lasalle_no_city end status={lasalle_status or '-'} seconds={time.time() - stage_started:.2f}",
            )
            if row.get("fase2d_status") != "filled_doc":
                maybe_run_ache_fallback(row, stats, fetch_args, should_log, row_n)
            if row.get("fase2d_status") != "filled_doc":
                row["fase2d_status"] = "no_city"
            stats["no_city"] += 1
            progress_log(should_log, f"  n={row_n} stage=done no_city elapsed={time.time() - row_started:.1f}s")
            continue
        stats["rows_with_city"] += 1

        stage_started = time.time()
        progress_log(should_log, f"  n={row_n} stage=legalle start")
        allow_legalle_discovery = False
        progress_log(
            should_log,
            f"  n={row_n} stage=legalle hint={'SI' if legalle_row_hint(row) else 'NO'} discovery_candidate={'SI' if legalle_discovery_signal(row, city) else 'NO'}",
        )
        legalle_status = enrich_legalle_deep(row, city, fetch_args, allow_discovery=allow_legalle_discovery)
        if legalle_status == "filled_doc":
            stats["legalle_deep_filled"] += 1
        progress_log(
            should_log,
            f"  n={row_n} stage=legalle end status={legalle_status or '-'} seconds={time.time() - stage_started:.2f}",
        )

        stage_started = time.time()
        progress_log(should_log, f"  n={row_n} stage=lasalle start")
        allow_lasalle_discovery = lasalle_discovery_signal(row, city)
        progress_log(
            should_log,
            f"  n={row_n} stage=lasalle hint={'SI' if lasalle_row_hint(row) else 'NO'} discovery_candidate={'SI' if allow_lasalle_discovery else 'NO'}",
        )
        lasalle_status = enrich_lasalle_deep(row, city, fetch_args, allow_discovery=allow_lasalle_discovery)
        if lasalle_status == "filled_doc":
            stats["lasalle_deep_filled"] += 1
        progress_log(
            should_log,
            f"  n={row_n} stage=lasalle end status={lasalle_status or '-'} seconds={time.time() - stage_started:.2f}",
        )
        if (
            row.get("fase2d_status") == "filled_doc"
            and (row.get("edital_pdf") or "").strip()
            and not current_pdf_needs_repair(row)
        ):
            done_reason = "lasalle_filled" if lasalle_status == "filled_doc" else "already_filled"
            progress_log(should_log, f"  n={row_n} stage=done {done_reason} elapsed={time.time() - row_started:.1f}s")
            continue

        site_row = sites_by_slug.get(slug)
        docs = docs_by_slug.get(slug, [])
        expected_pair = row_edital_pair(row)
        expected_kind = infer_source_kind(row, docs, site_row)
        row["fase2d_expected_source_kind"] = expected_kind
        row["fase2d_expected_edital_num"] = edital_label(expected_pair)
        progress_log(
            should_log,
            f"  n={row_n} stage=municipal_context kind={expected_kind} docs={len(docs)} expected={row['fase2d_expected_edital_num'] or '-'}",
        )

        bases = base_urls_for(site_row, expected_kind)
        if (
            row.get("fase2d_status") == "filled_doc"
            and (row.get("edital_pdf") or "").strip()
            and not current_pdf_needs_repair(row)
        ):
            progress_log(should_log, f"  n={row_n} stage=done already_filled elapsed={time.time() - row_started:.1f}s")
            continue
        if not bases and not docs:
            maybe_run_legalle_discovery(row, stats, fetch_args, should_log, row_n, city)
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                progress_log(should_log, f"  n={row_n} stage=done legalle_discovery_filled elapsed={time.time() - row_started:.1f}s")
                continue
            maybe_run_lasalle_discovery(row, stats, fetch_args, should_log, row_n, city)
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                progress_log(should_log, f"  n={row_n} stage=done lasalle_discovery_filled elapsed={time.time() - row_started:.1f}s")
                continue
            maybe_run_ache_fallback(row, stats, fetch_args, should_log, row_n)
            if (
                row.get("fase2d_status") == "filled_doc"
                and (row.get("edital_pdf") or "").strip()
                and not current_pdf_needs_repair(row)
            ):
                progress_log(should_log, f"  n={row_n} stage=done ache_filled elapsed={time.time() - row_started:.1f}s")
                continue
            if row.get("fase2d_status") != "filled_doc":
                row["fase2d_status"] = "no_municipal_base"
            stats["no_municipal_base"] += 1
            progress_log(should_log, f"  n={row_n} stage=done no_base elapsed={time.time() - row_started:.1f}s")
            continue

        stage_started = time.time()
        progress_log(should_log, f"  n={row_n} stage=choose_doc_match start bases={len(bases)} docs={len(docs)}")
        match = choose_doc_match(row, docs, expected_kind, expected_pair, slug) if docs else None
        is_accepted = accepted_match(match, expected_pair)
        base_url, base_kind, base_specific = best_base_from_match(match if is_accepted else None, bases)
        progress_log(
            should_log,
            f"  n={row_n} stage=choose_doc_match end accepted={is_accepted} score={match.get('score') if match else '-'} seconds={time.time() - stage_started:.2f} base={base_url[:90] if base_url else '-'}",
        )

        row["fase2d_base_url"] = base_url
        row["fase2d_base_kind"] = base_kind
        row["fase2d_base_specific"] = base_specific
        row["fase2d_index_url"] = (bases[0][0] if bases else (match["doc"].get("index_url", "") if match else ""))

        needs_base = (
            not (row.get("official_base_url") or "").strip()
            or is_generic_base(row.get("official_base_url", ""))
            or (is_accepted and base_specific == "SI" and row.get("official_base_url") != base_url)
        )

        if is_accepted and match:
            doc = match["doc"]
            doc_urls = download_urls_for_doc(doc)
            stage_started = time.time()
            progress_log(should_log, f"  n={row_n} stage=canonical_doc_page start candidate={doc.get('candidate_url', '')[:100]}")
            page_url = canonical_doc_page_for_row(row, doc.get("candidate_url", ""), expected_kind, fetch_args)
            progress_log(
                should_log,
                f"  n={row_n} stage=canonical_doc_page end seconds={time.time() - stage_started:.2f} page={page_url[:100] if page_url else '-'}",
            )
            row["fase2d_main_edital_url"] = doc_urls[0] if doc_urls else ""
            row["fase2d_main_doc_page_url"] = page_url
            row["fase2d_main_doc_title"] = doc.get("doc_title", "")
            row["fase2d_main_doc_type"] = doc.get("doc_type", "")
            row["fase2d_main_doc_edital_num"] = edital_label(doc_edital_pairs(doc)[0]) if doc_edital_pairs(doc) else ""
            row["fase2d_main_doc_date"] = doc.get("date_guess", "")
            row["fase2d_main_doc_source_kind"] = doc.get("source_kind", "")
            row["fase2d_match_score"] = str(match.get("score", ""))
            row["fase2d_match_reasons"] = ",".join(match.get("reasons", []))
            update_metadata_from_official_context(row, " ".join([row["fase2d_main_doc_title"], page_url, row["fase2d_main_edital_url"]]), page_url)

            apply_base(row, page_url or base_url, "SI" if page_url and not is_file_url(page_url) else base_specific, update_core=True)
            if page_url and not is_file_url(page_url):
                row["edital_pagina"] = page_url
            added = apply_doc(row, doc_urls)
            row["fase2d_added_doc_urls"] = " | ".join(added)
            if added or needs_base:
                row["fase2d_status"] = "filled_doc"
                row["fase2d_action"] = "updated_core"
                stats["filled_doc"] += 1
            else:
                row["fase2d_status"] = "matched_existing"
                row["fase2d_action"] = "kept_core"
                stats["enriched_existing"] += 1
            progress_log(should_log, f"  n={row_n} stage=done accepted elapsed={time.time() - row_started:.1f}s")
            continue

        if match:
            doc = match["doc"]
            row["fase2d_main_edital_url"] = doc.get("best_download_url", "")
            row["fase2d_main_doc_page_url"] = doc.get("candidate_url", "")
            row["fase2d_main_doc_title"] = doc.get("doc_title", "")
            row["fase2d_main_doc_type"] = doc.get("doc_type", "")
            row["fase2d_main_doc_edital_num"] = edital_label(doc_edital_pairs(doc)[0]) if doc_edital_pairs(doc) else ""
            row["fase2d_main_doc_date"] = doc.get("date_guess", "")
            row["fase2d_main_doc_source_kind"] = doc.get("source_kind", "")
            row["fase2d_match_score"] = str(match.get("score", ""))
            row["fase2d_match_reasons"] = ",".join(match.get("reasons", []))
            row["fase2d_status"] = "review_candidate"
            row["fase2d_action"] = "no_core_update"
            stats["review_candidate"] += 1
        elif docs:
            row["fase2d_status"] = "no_confident_doc"
            stats["no_confident_doc"] += 1
        else:
            row["fase2d_status"] = "no_municipal_docs"
            stats["no_municipal_docs"] += 1

        maybe_run_legalle_discovery(row, stats, fetch_args, should_log, row_n, city)
        if (
            row.get("fase2d_status") == "filled_doc"
            and (row.get("edital_pdf") or "").strip()
            and not current_pdf_needs_repair(row)
        ):
            progress_log(should_log, f"  n={row_n} stage=done legalle_discovery_filled elapsed={time.time() - row_started:.1f}s")
            continue

        maybe_run_lasalle_discovery(row, stats, fetch_args, should_log, row_n, city)
        if (
            row.get("fase2d_status") == "filled_doc"
            and (row.get("edital_pdf") or "").strip()
            and not current_pdf_needs_repair(row)
        ):
            progress_log(should_log, f"  n={row_n} stage=done lasalle_discovery_filled elapsed={time.time() - row_started:.1f}s")
            continue

        maybe_run_ache_fallback(row, stats, fetch_args, should_log, row_n)
        if (
            row.get("fase2d_status") == "filled_doc"
            and (row.get("edital_pdf") or "").strip()
            and not current_pdf_needs_repair(row)
        ):
            progress_log(should_log, f"  n={row_n} stage=done ache_filled elapsed={time.time() - row_started:.1f}s")
            continue

        if base_url and needs_base:
            apply_base(row, base_url, "NO", update_core=True)
            row["fase2d_status"] = "base_only" if row["fase2d_status"] != "review_candidate" else "base_only_review_doc"
            row["fase2d_action"] = "updated_base_only"
            stats["base_only"] += 1
        progress_log(should_log, f"  n={row_n} stage=done status={row.get('fase2d_status', '-')} elapsed={time.time() - row_started:.1f}s")

    for row in ache_rows:
        finalize_row_text(row)
    stats["official_after"] = sum(1 for r in ache_rows if (r.get("tiene_pagina_oficial") or "").upper() == "SI")
    stats["edital_pdf_after"] = sum(1 for r in ache_rows if (r.get("edital_pdf") or "").strip())
    for row in ache_rows:
        row[SEMAFORO_FIELD] = compute_semaforo(row)
    stats["semaforo_listo"] = sum(1 for r in ache_rows if r.get(SEMAFORO_FIELD) == "listo")
    stats["semaforo_revisar"] = sum(1 for r in ache_rows if r.get(SEMAFORO_FIELD) == "revisar")
    stats["semaforo_no_encontrado"] = sum(1 for r in ache_rows if r.get(SEMAFORO_FIELD) == "No encontrado")
    progress_log(debug_progress, f"DONE rows={total_rows} elapsed={time.time() - started_at:.1f}s")
    return ache_rows, stats


def write_report(rows: List[Dict[str, str]], stats: Dict[str, int], path: Path) -> None:
    lines = [
        "# Fase 2D - Ache + municipios + documentos municipales",
        "",
        "## Resumen",
        "",
        f"- Filas Ache procesadas: {stats['total']}",
        f"- Filas agregadas por split de anexos multiples: {stats.get('split_rows_added', 0)}",
        f"- Filas agregadas por split de catalogo de banca: {stats.get('bank_split_rows_added', 0)}",
        f"- Filas con municipio detectado: {stats['rows_with_city']}",
        f"- Pagina oficial antes/despues: {stats['official_before']}/{stats['official_after']}",
        f"- Edital PDF antes/despues: {stats['edital_pdf_before']}/{stats['edital_pdf_after']}",
        f"- Semaforo listo/revisar/No encontrado: {stats['semaforo_listo']}/{stats['semaforo_revisar']}/{stats['semaforo_no_encontrado']}",
        f"- Documentos principales llenados: {stats['filled_doc']}",
        f"- PDFs llenados por Legalle profundo: {stats.get('legalle_deep_filled', 0)}",
        f"- PDFs llenados por Legalle discovery: {stats.get('legalle_discovery_filled', 0)}",
        f"- PDFs llenados por La Salle profundo: {stats.get('lasalle_deep_filled', 0)}",
        f"- PDFs llenados por La Salle discovery: {stats.get('lasalle_discovery_filled', 0)}",
        f"- Asignaciones La Salle por filas hermanas: {stats.get('lasalle_sibling_assigned', 0)}",
        f"- PDFs llenados por profundidad +1: {stats.get('depth_page_filled', 0)}",
        f"- Fallback Ache verificado: {stats.get('ache_fallback_used', 0)}",
        f"- Cebraspe normalizado desde CDN: {stats.get('cebraspe_fixed', 0)}",
        f"- Solo base oficial llenada: {stats['base_only']}",
        f"- Candidatos a revisar sin tocar campos core: {stats['review_candidate']}",
        f"- Fuera de scope municipal: {stats['not_municipal_scope']}",
        f"- Sin municipio: {stats['no_city']}",
        f"- Sin ruta/documentos municipales: {stats['no_municipal_base'] + stats['no_municipal_docs']}",
        f"- Sin documento confiable: {stats['no_confident_doc']}",
        "",
        "## Filas actualizadas",
        "",
        "| # | Status | Orgao | Edital | Fase2D | Base oficial | Edital principal | Score |",
        "|---:|---|---|---|---|---|---|---:|",
    ]
    for row in rows:
        if row.get("fase2d_action") not in {"updated_core", "updated_base_only"}:
            continue
        lines.append(
            "| {n} | {status} | {orgao} | {edital} | {fase2d} | {base} | {main} | {score} |".format(
                n=row.get("n", ""),
                status=ache.md_escape(row.get("status", ""), 12),
                orgao=ache.md_escape(row.get("orgao", ""), 50),
                edital=ache.md_escape(row.get("edital", ""), 20),
                fase2d=ache.md_escape(row.get("fase2d_status", ""), 22),
                base=ache.md_escape(row.get("fase2d_base_url", ""), 70),
                main=ache.md_escape(row.get("fase2d_main_edital_url", ""), 70),
                score=ache.md_escape(row.get("fase2d_match_score", ""), 8),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fase 2D - integrar Ache con municipios y documentos oficiales.")
    parser.add_argument("--ache", default=str(DEFAULT_ACHE))
    parser.add_argument("--sites", default=str(DEFAULT_SITES))
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    parser.add_argument("--report", default=str(DEFAULT_MD))
    parser.add_argument("--debug-progress", action="store_true", help="Imprime progreso por fila y etapa.")
    parser.add_argument("--debug-fetch", action="store_true", help="Imprime cada request HTTP con URL y duracion.")
    parser.add_argument("--debug-log", default="", help="Archivo donde escribir el log de progreso/debug.")
    parser.add_argument("--progress-every", type=int, default=1, help="Frecuencia de filas para imprimir progreso.")
    parser.add_argument("--limit", type=int, default=0, help="Procesa solo las primeras N filas para debug.")
    parser.add_argument("--fetch-timeout", type=float, default=30, help="Timeout HTTP en segundos.")
    parser.add_argument("--ache-fallback-timeout", type=float, default=2.5, help="Timeout corto para leer Ache como ultimo recurso.")
    parser.add_argument("--delay-min", type=float, default=0.05, help="Delay minimo entre requests.")
    parser.add_argument("--delay-max", type=float, default=0.15, help="Delay maximo entre requests.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    global PROGRESS_LOG_PATH
    args = build_args(argv)
    if args.debug_log:
        PROGRESS_LOG_PATH = Path(args.debug_log).expanduser().resolve()
        PROGRESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_LOG_PATH.write_text("", encoding="utf-8")
    install_fetch_logger(args.debug_fetch)
    ache_path = Path(args.ache).expanduser().resolve()
    sites_path = Path(args.sites).expanduser().resolve()
    queue_path = Path(args.queue).expanduser().resolve()

    ache_rows = read_csv_dicts(ache_path)
    site_rows = read_csv_dicts(sites_path)
    queue_rows = read_csv_dicts(queue_path)
    if not ache_rows:
        print(f"Entrada Ache vacia: {ache_path}")
        return 1
    if not site_rows:
        print(f"Catalogo municipal vacio: {sites_path}")
        return 1
    if not queue_rows:
        print(f"Queue municipal vacia: {queue_path}")
        return 1
    if args.limit and args.limit > 0:
        ache_rows = ache_rows[:args.limit]
        if args.debug_progress:
            print(f"[debug] Limitando corrida a {len(ache_rows)} filas", flush=True)
    ache_rows, split_rows_added = expand_multi_edital_rows(ache_rows)
    if split_rows_added and args.debug_progress:
        print(f"[debug] Split de anexos multiples agrego {split_rows_added} filas", flush=True)
    bank_split_args = make_fetch_args(timeout=args.fetch_timeout, delay_min=args.delay_min, delay_max=args.delay_max)
    ache_rows, bank_split_rows_added = expand_bank_multi_edital_rows(ache_rows, site_rows, bank_split_args)
    if bank_split_rows_added and args.debug_progress:
        print(f"[debug] Split por catalogo de banca agrego {bank_split_rows_added} filas", flush=True)

    base_fields = [
        f for f in ache_rows[0].keys()
        if not f.startswith("_")
        and f != SEMAFORO_FIELD
        and f != TIPO_FIELD
        and not (f.startswith("official_doc_") and f[len("official_doc_"):].isdigit())
    ]
    base_fields.insert(0, SEMAFORO_FIELD)
    if "orgao" in base_fields:
        base_fields.insert(base_fields.index("orgao"), TIPO_FIELD)
    else:
        base_fields.insert(2, TIPO_FIELD)
    for field in FASE2D_FIELDS:
        if field not in base_fields:
            base_fields.append(field)

    rows, stats = integrate_rows(
        ache_rows,
        site_rows,
        queue_rows,
        debug_progress=args.debug_progress,
        progress_every=max(args.progress_every, 1),
        fetch_timeout=args.fetch_timeout,
        ache_fallback_timeout=args.ache_fallback_timeout,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )
    stats["split_rows_added"] = split_rows_added
    stats["bank_split_rows_added"] = bank_split_rows_added
    fields = recompute_doc_columns(rows, base_fields)

    out_path = Path(args.out).expanduser().resolve()
    xlsx_path = Path(args.xlsx).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()

    write_table(rows, fields, out_path, sheet_name="Fase2D integrada")
    write_xlsx(rows, fields, xlsx_path, sheet_name="Fase2D integrada")
    write_report(rows, stats, report_path)

    # Verificacion ligera: abrir el XLSX generado con nuestro lector XML.
    loaded = read_xlsx_dicts(xlsx_path)
    if len(loaded) != len(rows):
        print(f"ADVERTENCIA: XLSX leido con {len(loaded)} filas; esperado {len(rows)}")

    print("=============== FASE 2D - INTEGRACION MUNICIPAL ===============")
    print(f"  Filas Ache                    : {stats['total']}")
    print(f"  Filas agregadas por split     : {stats.get('split_rows_added', 0)}")
    print(f"  Filas agregadas por banca     : {stats.get('bank_split_rows_added', 0)}")
    print(f"  Municipios detectados          : {stats['rows_with_city']}")
    print(f"  Pagina oficial antes/despues   : {stats['official_before']}/{stats['official_after']}")
    print(f"  Edital PDF antes/despues       : {stats['edital_pdf_before']}/{stats['edital_pdf_after']}")
    print(f"  Semaforo listo/revisar/no enc. : {stats['semaforo_listo']}/{stats['semaforo_revisar']}/{stats['semaforo_no_encontrado']}")
    print(f"  Documentos principales llenados: {stats['filled_doc']}")
    print(f"  PDFs Legalle profundo          : {stats.get('legalle_deep_filled', 0)}")
    print(f"  PDFs Legalle discovery         : {stats.get('legalle_discovery_filled', 0)}")
    print(f"  PDFs La Salle profundo         : {stats.get('lasalle_deep_filled', 0)}")
    print(f"  PDFs La Salle discovery        : {stats.get('lasalle_discovery_filled', 0)}")
    print(f"  La Salle filas hermanas        : {stats.get('lasalle_sibling_assigned', 0)}")
    print(f"  PDFs profundidad +1            : {stats.get('depth_page_filled', 0)}")
    print(f"  Fallback Ache verificado       : {stats.get('ache_fallback_used', 0)}")
    print(f"  Cebraspe normalizado desde CDN : {stats.get('cebraspe_fixed', 0)}")
    print(f"  Solo base oficial llenada      : {stats['base_only']}")
    print(f"  Candidatos revision            : {stats['review_candidate']}")
    print(f"  CSV                            : {out_path}")
    print(f"  Excel                          : {xlsx_path}")
    print(f"  Report                         : {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
