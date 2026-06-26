#!/usr/bin/env python3
"""
Pipeline Ache Concursos RS -> evidence oficial.

Objetivo:
  1. Leer la pagina RS de Ache Concursos.
  2. Separar concursos "abertos" y "em andamento".
  3. Entrar en cada ficha de Ache.
  4. Extraer:
     - links oficiales directos (banca, orgao, diario oficial)
     - anexos internos de Ache (/edital-concurso/...)
     - PDFs embebidos en esos anexos
     - pagina/base oficial donde deberian vivir edital, retificacoes,
       gabaritos, resultados y otros documentos.
  5. Probar la pagina oficial especifica cuando exista y extraer documentos.

Salida:
  data/ache_rs_official_pipeline.xlsx
  data/ache_rs_official_pipeline.md

Nota: un PDF alojado en Ache se marca como evidencia/anexo, pero no como
fuente oficial. La fuente oficial debe ser dominio de banca, orgao publico o
diario oficial.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
import random
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import write_table  # noqa: E402
import fase1_v1 as f1  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_XLSX = PROJECT_ROOT / "data" / "ache_rs_official_pipeline.xlsx"
OUT_MD = PROJECT_ROOT / "data" / "ache_rs_official_pipeline.md"

RS_LIST = "https://www.acheconcursos.com.br/concursos-rio-grande-do-sul"
ACHE_HOST = "acheconcursos.com.br"
DETAIL_PREFIX = "/concursos-rio-grande-do-sul/"
ATTACHMENT_PREFIX = "/edital-concurso/"

BANCA_DOMAINS = {
    "fundatec.org.br",
    "legalleconcursos.com.br",
    "institutolegalle.org.br",
    "objetivas.com.br",
    "portalfaurgs.com.br",
    "fgv.br",
    "cebraspe.org.br",
    "concursosfcc.com.br",
    "cesgranrio.org.br",
    "vunesp.com.br",
    "quadrix.org.br",
    "fundacaolasalle.org.br",
    "ibfc.org.br",
    "institutoconsulplan.org.br",
    "access.org.br",
    "idecan.org.br",
    "gestaodeconcursos.com.br",
    "selecon.org.br",
    "avancasp.org.br",
    "institutomais.org.br",
    "nossorumo.org.br",
    "institutoaocp.org.br",
    "fadergs.org.br",
    "ibam.org.br",
    "consesp.com.br",
    "indebras.org.br",
}

# Plataformas de inscricao/publicacao usadas por bancas. No son siempre la banca
# juridica, pero funcionan como base oficial cuando estan enlazadas por la ficha.
OFFICIAL_PLATFORM_DOMAINS = {
    "selecao.net.br",
    "concursos-publicacoes.s3.amazonaws.com",
    "cdn.institutolegalle.org.br",
}

BANCA_KEYWORDS = {
    "fundatec": "fundatec",
    "legalle": "legalle",
    "faurgs": "faurgs",
    "objetiva": "objetiva",
    "objetivas": "objetiva",
    "fgv": "fgv",
    "cebraspe": "cebraspe",
    "cespe": "cebraspe",
    "fcc": "fcc",
    "cesgranrio": "cesgranrio",
    "vunesp": "vunesp",
    "quadrix": "quadrix",
    "la salle": "lasalle",
    "lasalle": "lasalle",
    "fundacao la salle": "lasalle",
    "fundação la salle": "lasalle",
    "consulplan": "consulplan",
}

EDITAL_NUM_RE = re.compile(
    r"edital\s*(?:de\s+abertura\s*)?(?:n[º°o\.]*\s*)?(\d{1,4}\s*/\s*\d{4})",
    re.IGNORECASE,
)
ORGAO_RE = re.compile(
    r"((?:prefeitura(?:\s+municipal)?|camara(?:\s+municipal)?|câmara(?:\s+municipal)?|"
    r"instituto|universidade|conselho|fundacao|fundação|secretaria|tribunal|"
    r"ministerio|ministério|autarquia|departamento|consorcio|consórcio)\s+"
    r"(?:de\s+|do\s+|da\s+|dos\s+|das\s+)?[A-Za-zÀ-ú][A-Za-zÀ-ú'\-\s]{2,55})",
    re.IGNORECASE,
)

MATCH_STOPWORDS = {
    "aberto", "aberta", "abre", "anuncia", "cargo", "cargos", "concurso",
    "concursos", "edital", "inscricao", "inscricoes", "inscrição", "inscrições",
    "municipal", "nivel", "nível", "para", "prefeitura", "processo", "publica",
    "publicado", "publico", "público", "salario", "salário", "salarios",
    "salários", "selecao", "seleção", "seletivo", "tem", "vagas", "veja",
    "rio", "grande", "sul", "2026",
}

RESOLVER_INDEXES = {
    "fundatec": [
        "https://www.fundatec.org.br/portal/concursos/",
        "https://www.fundatec.org.br/portal/concursos/concursos_abertos.php",
        "https://www.fundatec.org.br/portal/concursos/concursos_andamento.php",
        "https://www.fundatec.org.br/portal/concursos/concursos_encerrados.php",
    ],
    "legalle": [
        "https://portal.editais.legalleconcursos.com.br/edital/index/abertos",
        "https://portal.editais.legalleconcursos.com.br/edital/index/andamento",
        "https://portal.editais.legalleconcursos.com.br/edital/index/encerrados",
        "https://portal.editais.legalleconcursos.com.br/edital/index/suspensos",
        "https://portal.editais.legalleconcursos.com.br/edital/index/futuros",
        "https://portal.institutolegalle.org.br/edital/index/abertos",
        "https://portal.institutolegalle.org.br/edital/index/andamento",
        "https://portal.institutolegalle.org.br/edital/index/encerrados",
        "https://portal.institutolegalle.org.br/edital/index/suspensos",
        "https://portal.institutolegalle.org.br/edital/index/futuros",
    ],
    "quadrix": [
        "https://quadrix.org.br/",
    ],
    "lasalle": [
        "https://fundacaolasalle.org.br/filtro-concursos/page/1/?filtro=inscricoes-abertas",
        "https://fundacaolasalle.org.br/filtro-concursos/page/1/?filtro=em-breve",
        "https://fundacaolasalle.org.br/filtro-concursos/page/1/?filtro=em-andamento",
        "https://fundacaolasalle.org.br/filtro-concursos/page/1/?filtro=encerrados",
    ],
}

GENERIC_OFFICIAL_BY_BANCA = {
    "fundatec": "https://www.fundatec.org.br/portal/concursos/",
    "legalle": "https://portal.editais.legalleconcursos.com.br/edital",
    "quadrix": "https://quadrix.org.br/",
    "lasalle": "https://fundacaolasalle.org.br/concursos/",
}

FETCH_CACHE: Dict[str, "f1.FetchResult"] = {}

SKIP_HOST_PARTS = {
    "facebook",
    "instagram",
    "twitter",
    "x.com",
    "telegram",
    "t.me",
    "whatsapp",
    "youtube",
    "google",
    "googlesyndication",
    "doubleclick",
    "amazon-adsystem",
    "fonts.gstatic",
    "desenvolveweb",
    "jsuol",
    "uol.com.br",
    "schema.org",
    "w3.org",
    "apostilasopcao",
}

NOISE_URL_PARTS = {
    "termos.pdf",
    "politicas.pdf",
    "politica",
    "privacidade",
    "privacy",
    "cookies",
    "leitor-vlibras",
}

STATIC_ASSET_EXTS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".map",
)


@dataclass
class ListingRow:
    n: int
    status_ache: str
    title: str
    detail_url: str
    nivel: str = ""
    inscricoes_ate: str = ""
    vagas: str = ""
    salario_ate: str = ""


OUTPUT_FIELDS = [
    "n", "status", "orgao", "edital", "nivel", "inscricoes_ate",
    "detalle_ache", "source_role", "banca_guess",
    "tiene_pagina_oficial", "tiene_base_documentos",
    "official_base_url", "official_base_specific",
    "resolution_method", "resolution_score",
    "n_official_source_urls", "n_official_doc_urls",
    "n_ache_attachment_pages", "n_ache_attachment_pdfs",
    "ache_attachment_pages", "ache_attachment_pdfs",
    "edital_pdf", "edital_pagina", "retificacao", "gabarito",
    "resultado_classificados", "inscricao", "documento",
    "official_source_urls", "official_doc_urls", "attachment_titles",
]


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9/]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def signal_tokens(value: str) -> List[str]:
    norm = normalize_text(value)
    tokens: List[str] = []
    for token in re.findall(r"[a-z0-9]{3,}|\d{1,4}/\d{4}", norm):
        if token in MATCH_STOPWORDS:
            continue
        if token.isdigit() and len(token) < 4:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:18]


def detect_banca(value: str) -> str:
    norm = normalize_text(value)
    best_banca = ""
    best_count = 0
    for keyword, banca in BANCA_KEYWORDS.items():
        count = norm.count(keyword)
        if count > best_count:
            best_banca = banca
            best_count = count
    return best_banca


def detect_banca_from_urls(urls: Iterable[str]) -> str:
    for url in urls:
        host = urlparse(url).netloc.lower()
        if "fundatec.org.br" in host:
            return "fundatec"
        if "legalleconcursos.com.br" in host or "institutolegalle.org.br" in host:
            return "legalle"
        if "quadrix.org.br" in host or "selecao.net.br" in host:
            return "quadrix"
        if "fundacaolasalle.org.br" in host:
            return "lasalle"
        if "objetivas.com.br" in host:
            return "objetiva"
        if "cebraspe.org.br" in host:
            return "cebraspe"
        if "fgv.br" in host:
            return "fgv"
    return ""


def detect_edital_num(value: str) -> str:
    match = EDITAL_NUM_RE.search(value or "")
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = re.search(r"edital.{0,80}?(\d{1,4})\s*[-/]\s*(20\d{2})", value or "", re.I | re.S)
    if match:
        return f"{int(match.group(1))}/{match.group(2)}"
    return ""


def detect_edital_label(value: str) -> str:
    text = clean_text(value or "")
    patterns = [
        r"\b(?:Concurso\s+P[uú]blico|Processo\s+Seletivo(?:\s+Simplificado)?)[^0-9]{0,80}(?:n[º°o.]*)?\s*(\d{1,4}\s*/\s*20\d{2})",
        r"\bEdital\s+de\s+abertura[^0-9]{0,80}(?:n[º°o.]*)?\s*(\d{1,4}\s*/\s*20\d{2})",
        r"\bEdital\s+(?:de\s+abertura\s+)?(?:[A-Z]{2,}(?:/[A-Z0-9-]+){0,3})?\s*(?:n[º°o.]*)\s*(\d{1,4}\s*/\s*20\d{2})",
        r"\b([A-Z]{2,}(?:/[A-Z0-9-]+){0,3})\s*(?:n[º°o.]*)\s*(\d{1,4}\s*/\s*20\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        if not groups:
            continue
        num = re.sub(r"\s+", "", groups[-1])
        return f"nº {num}"
    return ""


def edital_label_quality(value: str) -> int:
    value = clean_text(value)
    if not value:
        return 0
    return 2 if re.match(r"^[A-Z]{2,}(?:/[A-Z0-9-]+){0,3}\s+nº\s+\d", value) else 1


def better_edital_label(current: str, candidate: str) -> str:
    candidate = clean_text(candidate)
    if edital_label_quality(candidate) > edital_label_quality(current):
        return candidate
    return current


def clean_nivel(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"^N[ií]vel:\s*", "", value, flags=re.I)
    return value


def normalize_status(value: str) -> str:
    value = (value or "").strip().lower()
    if value in {"em_andamento", "andamento"}:
        return "andamento"
    return "aberto" if value == "aberto" else value


def clean_orgao_candidate(value: str) -> str:
    candidate = clean_text(value)
    candidate = re.sub(
        r"\s+\b(?:abre|abrem|abriu|abriram|abrir|oferece|oferecem|oferta|ofertam|publica|publicou|divulga|divulgou|promove|promovem|tem|vai|lança|lanca|realiza|inscreve|paga|até|ate)\b.*$",
        "",
        candidate,
        flags=re.I,
    )
    candidate = re.sub(r"\s+20\d{2}\b.*$", "", candidate)
    candidate = candidate.strip(" -:;,.")
    norm = normalize_text(candidate)
    if norm in {"prefeitura", "prefeitura municipal", "camara", "camara municipal", "concurso", "edital"}:
        return ""
    if norm.endswith((" de", " do", " da", " dos", " das")):
        return ""
    if any(bad in norm for bad in ("possuir cnh", "conselho de classe", "cargos de niveis", "instituto legalle")):
        return ""
    if not candidate or any(bad in norm.split()[:3] for bad in ("abre", "abrem", "abriu", "oferece", "publica", "promove", "tem", "vai")):
        return ""
    return candidate[:100]


def fallback_orgao_from_title(value: str) -> str:
    title = clean_text(value)
    patterns = [
        r"(Prefeitura(?:\s+Municipal)?\s+(?:de|do|da)\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ú' -]+(?:-RS)?)",
        r"(C[aâ]mara(?:\s+Municipal)?\s+(?:de|do|da)\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ú' -]+(?:-RS)?)",
        r"Concurso\s+(?:P[uú]blico\s+)?(?:de|do|da)?\s*([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ú' -]+-RS).*?\bPrefeitura\b",
        r"Concurso\s+(?:P[uú]blico\s+)?(?:de|do|da)?\s*([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ú' -]+-RS).*?\bC[aâ]mara\b",
        r"\b(Edital\s+([A-Z]{2,}(?:-[A-Z]{2})?))\b",
        r"\b(Concurso\s+([A-Z]{2,}(?:-[A-Z]{2})?))\b",
    ]
    for idx, pattern in enumerate(patterns):
        match = re.search(pattern, title)
        if match:
            candidate = match.group(1)
            if idx == 2:
                return clean_orgao_candidate(f"Prefeitura de {candidate}")
            if idx == 3:
                return clean_orgao_candidate(f"Câmara de {candidate}")
            candidate = re.sub(r"^(Edital|Concurso)\s+", "", candidate, flags=re.I)
            return clean_orgao_candidate(candidate)
    return ""


def detect_orgao(value: str) -> str:
    match = ORGAO_RE.search(value or "")
    if match:
        return clean_orgao_candidate(match.group(1))
    return ""


def detect_city(value: str) -> str:
    patterns = [
        r"(?:de|do|da)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ú' -]{2,45})-RS",
        r"\b([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ú' -]{2,45})-RS\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, value or "")
        if match:
            city = clean_text(match.group(1))
            city = re.sub(r"^(Prefeitura|Camara|Câmara|Concurso|Edital)\s+", "", city, flags=re.I)
            return city[:80]
    return ""


def match_score(candidate_blob: str, query_text: str, edital_num: str = "") -> int:
    candidate_norm = normalize_text(candidate_blob)
    query_norm = normalize_text(query_text)
    tokens = signal_tokens(query_text)
    if not tokens:
        return 0
    score = 0
    for token in tokens:
        if token in candidate_norm:
            score += 2 if len(token) >= 6 else 1
    if edital_num and normalize_text(edital_num) in candidate_norm:
        score += 4
    for year in sorted(set(re.findall(r"20\d{2}", query_norm))):
        if year in candidate_norm:
            score += 3
        elif re.search(r"20\d{2}", candidate_norm):
            score -= 2
    if "rs" in candidate_norm or "rio grande do sul" in candidate_norm:
        score += 1
    if "crq rs" in query_norm and (
        "crq rs" in candidate_norm or "crq 05" in candidate_norm or "crq 5" in candidate_norm
    ):
        score += 6
    if "policia penal" in query_norm and (
        "policia penal" in candidate_norm or "pprs" in candidate_norm or "pp rs" in candidate_norm
    ):
        score += 6
    special_matches = [
        ("cra rs", ("cra rs", "conselho regional de administracao")),
        ("ipe prev", ("ipe prev", "instituto de previdencia do estado do rio grande do sul")),
        ("crp rs", ("crp rs", "conselho regional de psicologia", "setima regiao")),
        ("ufrgs", ("ufrgs", "universidade federal do rio grande do sul")),
        ("brigada militar", ("brigada militar", "bmrs")),
    ]
    for trigger, candidate_signals in special_matches:
        if trigger in query_norm and any(signal in candidate_norm for signal in candidate_signals):
            score += 8
    if ("crq rs" in query_norm or "crq 5" in query_norm or "crq 05" in query_norm) and (
        "crq ms" in candidate_norm or "crq 20" in candidate_norm or "crq xx" in candidate_norm
    ):
        score -= 12
    if "rs" in query_norm:
        other_ufs = [
            " ac ", " al ", " am ", " ap ", " ba ", " ce ", " df ", " es ", " go ",
            " ma ", " mg ", " ms ", " mt ", " pa ", " pb ", " pe ", " pi ", " pr ",
            " rj ", " rn ", " ro ", " rr ", " sc ", " se ", " sp ", " to ",
        ]
        padded = f" {candidate_norm} "
        if any(uf in padded for uf in other_ufs):
            score -= 4
    return score


def has_distinctive_match(candidate_blob: str, query_text: str) -> bool:
    candidate_norm = normalize_text(candidate_blob)
    for token in signal_tokens(query_text):
        if re.fullmatch(r"\d{1,4}/20\d{2}", token):
            continue
        if token in {"2026", "2027"}:
            continue
        if token in candidate_norm:
            return True
    return False


def fetch(url: str, args: argparse.Namespace) -> "f1.FetchResult":
    if getattr(args, "cache", True) and url in FETCH_CACHE:
        return FETCH_CACHE[url]
    fs = f1.Source("ache", "ache", url, "radar")
    res = f1.fetch_with_requests(fs, args.timeout, False, 1)
    if (res.result not in {"easy", "js"} or not res.body) and f1.creq is not None:
        res2 = f1.fetch_with_curl_cffi(fs, args.timeout, False, 1)
        if res2.body:
            if getattr(args, "cache", True):
                FETCH_CACHE[url] = res2
            return res2
    if getattr(args, "cache", True):
        FETCH_CACHE[url] = res
    return res


def get_links(raw_html: str, base: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for m in re.finditer(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        raw_html or "",
        re.I | re.S,
    ):
        href = html.unescape((m.group(1) or "").strip())
        anchor = clean_text(m.group(2) or "")
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absu = urljoin(base, href)
        if urlparse(absu).scheme in {"http", "https"}:
            out.append((absu, anchor[:220]))
    return out


def get_url_literals(raw_html: str) -> List[str]:
    urls = []
    for m in re.finditer(r"https?://[^\s\"'<>\\]+", raw_html or "", re.I):
        url = html.unescape(m.group(0)).rstrip(").,;")
        if urlparse(url).scheme in {"http", "https"}:
            urls.append(url)
    return list(dict.fromkeys(urls))


def host_matches(host: str, domain: str) -> bool:
    host = host.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def fundatec_concurso_id(url: str) -> str:
    parsed = urlparse(url)
    if not host_matches(parsed.netloc.lower(), "fundatec.org.br"):
        return ""
    values = parse_qs(parsed.query).get("concurso") or []
    if not values:
        return ""
    concurso_id = values[0].strip()
    return concurso_id if concurso_id.isdigit() else ""


def canonical_official_url(url: str) -> str:
    concurso_id = fundatec_concurso_id(url)
    if concurso_id:
        return f"https://www.fundatec.org.br/portal/concursos/pagina_editais.php?concurso={concurso_id}"
    parsed = urlparse(url)
    if host_matches(parsed.netloc.lower(), "fundacaolasalle.org.br"):
        match = re.search(r"^(/concurso/[^/?#]+/?)", parsed.path, re.I)
        if match:
            return f"https://fundacaolasalle.org.br{match.group(1).rstrip('/')}/"
    return url


def is_skip_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in SKIP_HOST_PARTS)


def is_noise_url(url: str) -> bool:
    low = url.lower()
    path = urlparse(url).path.lower()
    if path.endswith(STATIC_ASSET_EXTS):
        return True
    return any(part in low for part in NOISE_URL_PARTS)


def is_static_asset_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(STATIC_ASSET_EXTS)


def is_legalle_host(host: str) -> bool:
    return host_matches(host, "legalleconcursos.com.br") or host_matches(host, "institutolegalle.org.br")


def is_legalle_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    return is_legalle_host(parsed.netloc.lower()) and "/edital/ver/" in parsed.path.lower()


def is_legalle_doc_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host_matches(host, "cdn.institutolegalle.org.br") and path.endswith(".pdf"):
        return True
    if host == "s3.sa-east-1.amazonaws.com" and "cdn.legalle.com.br/edital/" in path and path.endswith(".pdf"):
        return True
    return False


def legalle_doc_rank(title: str, url: str) -> Tuple[int, str]:
    blob = normalize_text(f"{title} {url}")
    is_opening = "edital" in blob and "abertura" in blob
    if is_opening and "consolidado" in blob:
        rank = 0
    elif is_opening and "inscricoes" in blob:
        rank = 1
    elif is_opening:
        rank = 2
    elif "retific" in blob:
        rank = 3
    elif "homolog" in blob or "resultado" in blob or "classific" in blob or "convoca" in blob:
        rank = 4
    elif "gabarito" in blob:
        rank = 5
    elif "edital" in blob:
        rank = 6
    else:
        rank = 9
    return rank, blob


def legalle_doc_links(raw_html: str, base_url: str) -> List[Tuple[str, str]]:
    """Extract ordered document links from Legalle detail pages."""
    links: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    block_re = re.compile(
        r'<div\b[^>]*class=["\'][^"\']*list-group-item[^"\']*["\'][^>]*>.*?'
        r'(?=<div\b[^>]*class=["\'][^"\']*list-group-item|</div>\s*</div>\s*</div>|<br>|$)',
        re.I | re.S,
    )
    blocks = [m.group(0) for m in block_re.finditer(raw_html or "")]
    if not blocks:
        blocks = [raw_html or ""]
    for block in blocks:
        for link_m in re.finditer(
            r'<a\b[^>]*?href\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\'][^>]*>(.*?)</a>',
            block,
            re.I | re.S,
        ):
            url = urljoin(base_url, html.unescape((link_m.group(1) or "").strip()))
            if url in seen or not is_legalle_doc_url(url):
                continue
            title = clean_text(block)
            anchor = clean_text(link_m.group(2) or "")
            if anchor and anchor.lower() != "download":
                title = f"{title} {anchor}".strip()
            seen.add(url)
            links.append((url, title))
    links.sort(key=lambda item: legalle_doc_rank(item[1], item[0]))
    return links


def is_ache_url(url: str) -> bool:
    return host_matches(urlparse(url).netloc.lower(), ACHE_HOST)


def is_ache_attachment_page(url: str) -> bool:
    return is_ache_url(url) and urlparse(url).path.startswith(ATTACHMENT_PREFIX)


def is_ache_attachment_pdf(url: str) -> bool:
    parsed = urlparse(url)
    return is_ache_url(url) and parsed.path.lower().endswith(".pdf") and "/imagens/anexo/" in parsed.path


def is_official(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not host or is_skip_url(url) or is_ache_url(url):
        return False
    if is_legalle_doc_url(url):
        return True
    if host.endswith(".gov.br") or host == "gov.br":
        return True
    if any(host_matches(host, d) for d in BANCA_DOMAINS):
        return True
    if any(host_matches(host, d) for d in OFFICIAL_PLATFORM_DOMAINS):
        return True
    return False


def is_specific_official_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path_q = (parsed.path + "?" + parsed.query).lower()
    if not is_official(url):
        return False
    if parsed.path.lower().endswith(".pdf"):
        return True
    if host_matches(host, "fundatec.org.br"):
        return parsed.path.lower().endswith("/pagina_editais.php") and bool(fundatec_concurso_id(url))
    if is_legalle_detail_url(url) or is_legalle_doc_url(url):
        return True
    if host_matches(host, "fundacaolasalle.org.br"):
        return parsed.path.lower().startswith("/concurso/") or parsed.path.lower().endswith(".pdf")
    generic_paths = {"", "/", "/portal/concursos/", "/portal/concursos", "/inicial"}
    if parsed.path.lower() in generic_paths and not parsed.query:
        return False
    specific_markers = (
        "concurso=",
        "/edital/ver/",
        "/informacoes/",
        "/concurso/",
        "/concursos/",
        "/publicacoes",
        "/detalhes-diario/",
        "/selecoes",
        "/processo",
        ".pdf",
    )
    if any(marker in path_q for marker in specific_markers):
        return True
    if host.endswith(".gov.br") and len(parsed.path.strip("/")) > 8:
        return True
    return False


def categorize(url: str, anchor: str = "") -> str:
    blob = (url + " " + anchor).lower()
    if "retific" in blob or "retifica" in blob:
        return "retificacao"
    if "gabarito" in blob:
        return "gabarito"
    if any(k in blob for k in ("classificad", "resultado", "homologa", "convoca")):
        return "resultado_classificados"
    if any(k in blob for k in (
        "aviso", "comunicado", "cronograma", "prova pratica", "prova pr",
        "prova de tit", "relatorio medico", "relat", "atendimento especial",
        "autodeclara", "conteudo program", "conteAdo program", "atribuicoes",
        "atribui", "manual do candidato",
    )) and not any(k in blob for k in (
        "edital de abertura", "editaldeabertura", "edital_abertura", "editalconcurso"
    )):
        return "documento"
    if urlparse(url).path.lower().endswith(".pdf"):
        return "edital_pdf"
    if "edital" in blob:
        return "edital_pagina"
    if "inscri" in blob or "selecao" in blob or "ps-adm" in blob:
        return "inscricao"
    if any(k in blob for k in ("documento", "publicacao", "publicacoes", "arquivo")):
        return "documento"
    return "outro_oficial"


def add_unique(target: List[str], values: Iterable[str]) -> None:
    seen = set(target)
    for value in values:
        if value and value not in seen:
            target.append(value)
            seen.add(value)


def parse_listing_html(raw_html: str, base_url: str, start_n: int = 1) -> List[ListingRow]:
    rows: List[ListingRow] = []
    section_re = re.compile(
        r'<h4\b[^>]*class=["\'][^"\']*section-title[^"\']*["\'][^>]*>\s*'
        r'(?P<title>Concursos\s+(?:abertos|em andamento)[^<]*)'
        r'</h4>(?P<body>.*?)(?=<h4\b[^>]*class=["\'][^"\']*section-title|$)',
        re.I | re.S,
    )
    for section in section_re.finditer(raw_html or ""):
        section_title = clean_text(section.group("title"))
        status = "aberto" if "abertos" in section_title.lower() else "em_andamento"
        table_m = re.search(r'<table\b[^>]*class=["\'][^"\']*tbl-conc[^"\']*["\'][^>]*>(.*?)</table>', section.group("body"), re.I | re.S)
        if not table_m:
            continue
        for tr in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", table_m.group(1), re.I | re.S):
            row_html = tr.group(1)
            href_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', row_html, re.I | re.S)
            if not href_m:
                continue
            detail_url = urljoin(base_url, html.unescape(href_m.group(1)))
            parsed = urlparse(detail_url)
            if not host_matches(parsed.netloc.lower(), ACHE_HOST):
                continue
            if not parsed.path.startswith(DETAIL_PREFIX):
                continue
            slug = parsed.path[len(DETAIL_PREFIX):].strip("/")
            if not slug or "/" in slug:
                continue

            title_m = re.search(r'<span\b[^>]*class=["\'][^"\']*titulo[^"\']*["\'][^>]*>(.*?)</span>', row_html, re.I | re.S)
            nivel_m = re.search(r'<span\b[^>]*class=["\'][^"\']*vagas[^"\']*["\'][^>]*>(.*?)</span>', row_html, re.I | re.S)
            inscr_m = re.search(r'<span\b[^>]*class=["\'][^"\']*inscricao_fim[^"\']*["\'][^>]*>(.*?)</span>', row_html, re.I | re.S)
            vagas_m = re.search(r'<span\b[^>]*class=["\'][^"\']*numero_vagas[^"\']*["\'][^>]*>(.*?)</span>', row_html, re.I | re.S)
            sal_m = re.search(r'<span\b[^>]*class=["\'][^"\']*sal_max[^"\']*["\'][^>]*>(.*?)</span>', row_html, re.I | re.S)
            rows.append(ListingRow(
                n=start_n + len(rows),
                status_ache=status,
                title=clean_text(title_m.group(1) if title_m else href_m.group(2)),
                detail_url=detail_url,
                nivel=clean_text(nivel_m.group(1) if nivel_m else ""),
                inscricoes_ate=clean_text(inscr_m.group(1) if inscr_m else ""),
                vagas=clean_text(vagas_m.group(1) if vagas_m else ""),
                salario_ate=clean_text(sal_m.group(1) if sal_m else ""),
            ))
    return rows


def collect_listing_rows(args: argparse.Namespace) -> List[ListingRow]:
    seen: set[str] = set()
    all_rows: List[ListingRow] = []
    for page in range(1, args.max_pages + 1):
        url = RS_LIST if page == 1 else f"{RS_LIST}?page={page}"
        res = fetch(url, args)
        if not res.body or res.status != 200:
            break
        page_rows = parse_listing_html(res.body, url, start_n=len(all_rows) + 1)
        added = 0
        for row in page_rows:
            if row.detail_url in seen:
                continue
            seen.add(row.detail_url)
            row.n = len(all_rows) + 1
            all_rows.append(row)
            added += 1
        if added == 0:
            break
        if page < args.max_pages:
            time.sleep(random.uniform(args.delay_min, args.delay_max))
    return all_rows


def official_and_attachment_links(raw_html: str, base_url: str) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    official: List[Tuple[str, str]] = []
    attachment_pages: List[str] = []
    ache_pdfs: List[str] = []

    pairs = get_links(raw_html, base_url)
    for literal in get_url_literals(raw_html):
        pairs.append((literal, ""))

    seen_pairs = set()
    for url, anchor in pairs:
        key = (url, anchor)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        url = canonical_official_url(url)
        if is_ache_attachment_page(url):
            add_unique(attachment_pages, [url])
        elif is_ache_attachment_pdf(url):
            add_unique(ache_pdfs, [url])
        elif is_official(url) and not is_noise_url(url):
            official.append((url, anchor))

    return official, attachment_pages, ache_pdfs


def inspect_attachment(url: str, args: argparse.Namespace) -> Dict[str, object]:
    res = fetch(url, args)
    raw_html = res.body or ""
    official, nested_attachment_pages, ache_pdfs = official_and_attachment_links(raw_html, res.final_url or url)
    # Attachment pages can repeat themselves; keep only external official links and PDFs.
    text_excerpt = f1.visible_text(raw_html)[:5000] if raw_html else ""
    return {
        "url": url,
        "status": res.status,
        "title": f1.page_title(raw_html) if raw_html else "",
        "text_excerpt": text_excerpt,
        "official": official,
        "nested_attachment_pages": nested_attachment_pages,
        "ache_pdfs": ache_pdfs,
    }


def probe_official_page(url: str, args: argparse.Namespace) -> Dict[str, object]:
    if not url:
        return {"status": "", "title": "", "doc_links": []}
    res = fetch(url, args)
    raw_html = res.body or ""
    doc_links: List[Tuple[str, str]] = []
    if raw_html:
        if is_legalle_detail_url(res.final_url or url):
            doc_links.extend(legalle_doc_links(raw_html, res.final_url or url))
        pairs = get_links(raw_html, res.final_url or url)
        for literal in get_url_literals(raw_html):
            pairs.append((literal, ""))
        seen_doc_urls: Set[str] = set()
        for u, _anchor in doc_links:
            seen_doc_urls.add(u)
        for u, anchor in pairs:
            blob = (u + " " + anchor).lower()
            same_official_host = urlparse(u).netloc.lower() == urlparse(url).netloc.lower()
            if is_noise_url(u):
                continue
            if not (is_official(u) or same_official_host):
                continue
            if any(k in blob for k in ("edital", "retific", "gabarito", "resultado", "classific", "homologa", "convoca", "arquivo", ".pdf")):
                if u not in seen_doc_urls:
                    doc_links.append((u, anchor))
                    seen_doc_urls.add(u)
    elif urlparse(url).path.lower().endswith(".pdf"):
        doc_links.append((url, "pdf"))
    return {
        "status": res.status,
        "title": f1.page_title(raw_html) if raw_html else "",
        "text_excerpt": f1.visible_text(raw_html)[:8000] if raw_html else "",
        "doc_links": doc_links[:80],
    }


def deeper_edital_contexts(doc_links: List[Tuple[str, str]], args: argparse.Namespace) -> List[str]:
    contexts: List[str] = []
    fetched = 0
    for url, anchor in doc_links:
        contexts.append(f"{anchor} {url}")
        parsed = urlparse(url)
        if parsed.path.lower().endswith(".pdf"):
            continue
        if not is_official(url):
            continue
        if fetched >= args.max_edital_probe_docs:
            continue
        if args.delay_min:
            time.sleep(random.uniform(args.delay_min, args.delay_max))
        res = fetch(url, args)
        raw_html = res.body or ""
        if raw_html:
            contexts.append(" ".join([
                f1.page_title(raw_html),
                f1.visible_text(raw_html)[:5000],
            ]))
        fetched += 1
    return contexts


def candidate_links_from_index(raw_html: str, base_url: str) -> List[Tuple[str, str]]:
    """Return official candidate URLs with nearby text from an index page."""
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    if "fundacaolasalle.org.br" in urlparse(base_url).netloc.lower() or "fundacaolasalle.org.br/concurso/" in (raw_html or ""):
        for item in re.finditer(r'<article\b[^>]*class=["\'][^"\']*\bconcurso\b[^"\']*["\'][^>]*>.*?</article>', raw_html or "", re.I | re.S):
            block = item.group(0)
            url = ""
            for url_m in re.finditer(r"https?://fundacaolasalle\.org\.br/concurso/[^/\"'<&\s]+/?", block, re.I):
                url = canonical_official_url(html.unescape(url_m.group(0)))
                break
            if not url:
                for href_m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+/concurso/[^"\']+)["\']', block, re.I | re.S):
                    url = canonical_official_url(urljoin(base_url, html.unescape(href_m.group(1).strip())))
                    break
            if not url or url in seen or not is_official(url) or is_noise_url(url):
                continue
            title = ""
            desc = ""
            title_m = re.search(r'<h3\b[^>]*class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*>(.*?)</h3>', block, re.I | re.S)
            desc_m = re.search(r'<h4\b[^>]*class=["\'][^"\']*\bdescription\b[^"\']*["\'][^>]*>(.*?)</h4>', block, re.I | re.S)
            if title_m:
                title = clean_text(title_m.group(1))
            if desc_m:
                desc = clean_text(desc_m.group(1))
            doc_bits = []
            for doc_m in re.finditer(r'<a\b[^>]*class=["\'][^"\']*\blink-item\b[^"\']*["\'][^>]*>(.*?)</a>', block, re.I | re.S):
                doc_bits.append(clean_text(doc_m.group(1)))
                if len(doc_bits) >= 12:
                    break
            context = " ".join([title, desc, " | ".join(doc_bits), clean_text(block)[:1200]]).strip()
            seen.add(url)
            out.append((url, context))
        if out:
            return out

    if "fundatec.org.br" in urlparse(base_url).netloc.lower() or "index_concursos.php?concurso=" in (raw_html or ""):
        fundatec_html = re.sub(r"<!--.*?-->", " ", raw_html or "", flags=re.S)
        fundatec_re = re.compile(
            r'<a\b[^>]*href=["\'](?P<href>[^"\']*index_concursos\.php\?concurso=\d+[^"\']*)["\'][^>]*>'
            r'(?P<anchor>.*?)</a>\s*<br\s*/?>\s*(?P<desc>.*?)(?:<hr\b|<!---)',
            re.I | re.S,
        )
        for match in fundatec_re.finditer(fundatec_html):
            url = canonical_official_url(urljoin(base_url, html.unescape(match.group("href").strip())))
            if url in seen or not is_official(url) or is_noise_url(url):
                continue
            anchor = clean_text(match.group("anchor") or "")
            desc = clean_text(match.group("desc") or "")
            seen.add(url)
            out.append((url, f"{anchor} {desc}".strip()))
        if out:
            return out

    if "quadrix.org.br" in urlparse(base_url).netloc.lower() or "/informacoes/" in (raw_html or ""):
        for item in re.finditer(
            r'<li\b[^>]*class=["\'][^"\']*\bitem\b[^"\']*["\'][^>]*>(?P<body>.*?)</li>',
            raw_html or "",
            re.I | re.S,
        ):
            block = item.group("body")
            context = clean_text(block)
            for link_m in re.finditer(
                r'<a\b[^>]*?href\s*=\s*["\'](?P<href>[^"\']*/informacoes/\d+/?[^"\']*)["\'][^>]*>(?P<anchor>.*?)</a>',
                block,
                re.I | re.S,
            ):
                url = urljoin(base_url, html.unescape(link_m.group("href").strip()))
                if url in seen or not is_official(url) or is_noise_url(url):
                    continue
                anchor = clean_text(link_m.group("anchor") or "")
                seen.add(url)
                out.append((url, f"{anchor} {context}".strip()))
        if out:
            return out

    block_patterns = [
        r'<div\b[^>]*class=["\'][^"\']*lista_cont[^"\']*["\'][^>]*>.*?(?=<div\b[^>]*class=["\'][^"\']*lista_cont|</body>|$)',
        r'<div\b[^>]*class=["\'][^"\']*box-inscricoes-abertas[^"\']*["\'][^>]*>.*?(?=<div\b[^>]*class=["\'][^"\']*box-inscricoes-abertas|</body>|$)',
        r'<div\b[^>]*class=["\'][^"\']*card\s+rounded\s+mb-4[^"\']*["\'][^>]*>.*?(?=<div\b[^>]*class=["\'][^"\']*card\s+rounded\s+mb-4|</body>|$)',
        r'<article\b[^>]*>.*?</article>',
    ]
    blocks: List[str] = []
    for pattern in block_patterns:
        blocks.extend(m.group(0)[:4000] for m in re.finditer(pattern, raw_html or "", re.I | re.S))

    for link_m in re.finditer(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']*(?:legalleconcursos\.com\.br|institutolegalle\.org\.br)/(?:edital/)?ver/[^"\']+)["\'][^>]*>(.*?)</a>',
        raw_html or "",
        re.I | re.S,
    ):
        url = urljoin(base_url, html.unescape((link_m.group(1) or "").strip()))
        if url in seen or not is_official(url) or is_noise_url(url):
            continue
        prefix = raw_html[max(0, link_m.start() - 1200):link_m.start()]
        p_texts = [clean_text(x) for x in re.findall(r"<p\b[^>]*>(.*?)</p>", prefix, re.I | re.S)]
        context = " ".join([x for x in p_texts[-5:] if x])
        anchor = clean_text(link_m.group(2) or "")
        seen.add(url)
        out.append((url, f"{anchor} {context}".strip()))

    for link_m in re.finditer(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']*quadrix\.org\.br/informacoes/\d+/?[^"\']*)["\'][^>]*>(.*?)</a>',
        raw_html or "",
        re.I | re.S,
    ):
        url = urljoin(base_url, html.unescape((link_m.group(1) or "").strip()))
        if url in seen or not is_official(url) or is_noise_url(url):
            continue
        prefix = raw_html[max(0, link_m.start() - 700):link_m.start()]
        suffix = raw_html[link_m.start():min(len(raw_html), link_m.end() + 300)]
        context = clean_text(prefix + " " + suffix)
        anchor = clean_text(link_m.group(2) or "")
        seen.add(url)
        out.append((url, f"{anchor} {context}".strip()))

    def add_block_links(block: str) -> None:
        text = clean_text(block)
        for link_m in re.finditer(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, re.I | re.S):
            href = html.unescape((link_m.group(1) or "").strip())
            if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
                continue
            url = canonical_official_url(urljoin(base_url, href))
            if url in seen or not is_official(url) or is_noise_url(url):
                continue
            seen.add(url)
            anchor = clean_text(link_m.group(2) or "")
            out.append((url, f"{anchor} {text}".strip()))

    for block in blocks:
        add_block_links(block)

    if out:
        return out

    for match in re.finditer(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        raw_html or "",
        re.I | re.S,
    ):
        href = html.unescape((match.group(1) or "").strip())
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        url = canonical_official_url(urljoin(base_url, href))
        if url in seen or not is_official(url) or is_noise_url(url):
            continue
        seen.add(url)
        start = max(0, match.start() - 260)
        end = min(len(raw_html), match.end() + 360)
        nearby = clean_text(raw_html[start:end])
        anchor = clean_text(match.group(2) or "")
        out.append((url, f"{anchor} {nearby}".strip()))

    # Some pages embed useful URLs outside anchors.
    for url in get_url_literals(raw_html):
        url = canonical_official_url(url)
        if url in seen or not is_official(url) or is_noise_url(url):
            continue
        seen.add(url)
        out.append((url, url))
    return out


def resolver_indexes_for(banca: str, official_url: str) -> List[str]:
    host = urlparse(official_url).netloc.lower()
    indexes: List[str] = []
    if banca in RESOLVER_INDEXES:
        indexes.extend(RESOLVER_INDEXES[banca])
    if "fundatec.org.br" in host:
        indexes.extend(RESOLVER_INDEXES["fundatec"])
    elif "legalleconcursos.com.br" in host or "institutolegalle.org.br" in host:
        indexes.extend(RESOLVER_INDEXES["legalle"])
    elif "quadrix.org.br" in host or "selecao.net.br" in host:
        indexes.extend(RESOLVER_INDEXES["quadrix"])
    add_unique(indexes, [official_url])
    return indexes


def resolve_specific_official_url(
    official_url: str,
    query_text: str,
    banca: str,
    edital_num: str,
    args: argparse.Namespace,
) -> Dict[str, object]:
    """Resolve generic banca/orgao URL into a specific contest page when possible."""
    if not official_url:
        return {"url": "", "method": "", "score": 0}
    canonical_url = canonical_official_url(official_url)
    if is_specific_official_url(canonical_url):
        method = "fundatec_documents_page" if canonical_url != official_url else "direct_specific"
        return {"url": canonical_url, "method": method, "score": 99}

    best_url = ""
    best_context = ""
    best_score = 0
    best_index = ""
    for index_url in resolver_indexes_for(banca, official_url):
        res = fetch(index_url, args)
        if not res.body or res.status not in {200, 301, 302}:
            continue
        for url, context in candidate_links_from_index(res.body, res.final_url or index_url):
            url = canonical_official_url(url)
            if not is_specific_official_url(url):
                continue
            score = match_score(context + " " + url, query_text, edital_num)
            if score > best_score:
                best_url = url
                best_context = context[:500]
                best_score = score
                best_index = index_url
        time.sleep(random.uniform(args.delay_min, args.delay_max))

    # Require a few independent signals; otherwise keep the generic URL as generic.
    if best_score >= args.resolve_min_score and has_distinctive_match(best_context + " " + best_url, query_text):
        return {
            "url": best_url,
            "method": "resolved_from_official_index",
            "score": best_score,
            "index": best_index,
            "context": best_context,
        }
    return {
        "url": official_url,
        "method": "generic_unresolved",
        "score": best_score,
        "index": best_index,
        "context": best_context,
    }


def choose_official_base(official_links: List[Tuple[str, str]]) -> Tuple[str, str]:
    if not official_links:
        return "", ""
    specific = [(u, a) for u, a in official_links if is_specific_official_url(u)]
    chosen = specific[0] if specific else official_links[0]
    return canonical_official_url(chosen[0]), chosen[1]


def process_one(row: ListingRow, args: argparse.Namespace) -> Dict[str, object]:
    res = fetch(row.detail_url, args)
    raw_html = res.body or ""
    detail_title = f1.page_title(raw_html) if raw_html else ""
    official_links, attachment_pages, ache_pdfs = official_and_attachment_links(raw_html, res.final_url or row.detail_url)
    detail_text = f1.visible_text(raw_html)[:12000] if raw_html else ""

    attachment_titles: List[str] = []
    attachment_texts: List[str] = []
    for attachment in attachment_pages[: args.max_attachments_per_detail]:
        if args.delay_min:
            time.sleep(random.uniform(args.delay_min, args.delay_max))
        att = inspect_attachment(attachment, args)
        attachment_titles.append(str(att.get("title") or ""))
        attachment_texts.append(str(att.get("text_excerpt") or ""))
        add_unique(ache_pdfs, att.get("ache_pdfs", []))
        for official in att.get("official", []):
            if official not in official_links:
                official_links.append(official)

    field_text = " ".join([
        row.title,
        detail_title,
        row.nivel,
        row.inscricoes_ate,
        detail_text[:5000],
        " ".join(attachment_titles),
        " ".join(attachment_texts)[:5000],
    ])
    query_text = " ".join([
        row.title,
        detail_title,
        row.nivel,
        row.vagas,
        row.salario_ate,
    ])
    banca_guess = detect_banca_from_urls([u for u, _ in official_links]) or detect_banca(field_text)
    edital_label = detect_edital_label(field_text) or detect_edital_label(query_text)
    edital_num = detect_edital_num(edital_label) or detect_edital_num(query_text)
    orgao = detect_orgao(field_text) or fallback_orgao_from_title(detail_title or row.title)
    city_guess = detect_city(row.title + " " + detail_title)
    resolver_query_text = " ".join([query_text, orgao, city_guess, banca_guess])

    if not official_links and banca_guess in GENERIC_OFFICIAL_BY_BANCA:
        official_links.append((GENERIC_OFFICIAL_BY_BANCA[banca_guess], f"banca_detected:{banca_guess}"))

    official_base_url, official_base_anchor = choose_official_base(official_links)
    resolution = {"url": official_base_url, "method": "none", "score": 0}
    if official_base_url and args.resolve_official:
        resolution = resolve_specific_official_url(
            official_base_url,
            resolver_query_text,
            banca_guess,
            edital_num,
            args,
        )
        if resolution.get("url"):
            official_base_url = canonical_official_url(str(resolution["url"]))
    official_base_specific = "SI" if is_specific_official_url(official_base_url) else "NO"

    official_probe = {"status": "", "title": "", "text_excerpt": "", "doc_links": []}
    if official_base_url and (args.follow_generic_official or official_base_specific == "SI"):
        if args.delay_min:
            time.sleep(random.uniform(args.delay_min, args.delay_max))
        official_probe = probe_official_page(official_base_url, args)

    official_probe_context = " ".join([
        str(official_probe.get("title") or ""),
        str(official_probe.get("text_excerpt") or ""),
        " ".join(f"{anchor} {url}" for url, anchor in official_probe.get("doc_links", [])),
    ])
    edital_label = better_edital_label(edital_label, detect_edital_label(official_probe_context))
    if edital_label_quality(edital_label) < 2 and official_probe.get("doc_links"):
        deep_context = " ".join(deeper_edital_contexts(list(official_probe.get("doc_links", [])), args))
        edital_label = better_edital_label(edital_label, detect_edital_label(deep_context))
    edital_num = detect_edital_num(edital_label) or edital_num

    categories: Dict[str, List[str]] = {
        "edital_pdf": [],
        "edital_pagina": [],
        "retificacao": [],
        "gabarito": [],
        "resultado_classificados": [],
        "inscricao": [],
        "documento": [],
        "outro_oficial": [],
    }

    for u, anchor in official_links:
        cat = categorize(u, anchor) if is_specific_official_url(u) else "outro_oficial"
        categories.setdefault(cat, [])
        add_unique(categories[cat], [u])
    for u, anchor in official_probe.get("doc_links", []):
        categories.setdefault(categorize(u, anchor), [])
        add_unique(categories[categorize(u, anchor)], [u])

    official_doc_urls: List[str] = []
    for cat in ("edital_pdf", "edital_pagina", "retificacao", "gabarito", "resultado_classificados", "documento"):
        add_unique(official_doc_urls, categories.get(cat, []))

    official_source_urls: List[str] = []
    add_unique(official_source_urls, [u for u, _ in official_links])
    if official_base_url:
        add_unique(official_source_urls, [official_base_url])

    def first(cat: str) -> str:
        return categories.get(cat, [""])[0] if categories.get(cat) else ""

    return {
        "n": row.n,
        "status": normalize_status(row.status_ache),
        "orgao": orgao,
        "edital": edital_label,
        "nivel": clean_nivel(row.nivel),
        "inscricoes_ate": row.inscricoes_ate,
        "detalle_ache": row.detail_url,
        "source_role": "ache_preliminar",
        "banca_guess": banca_guess,
        "tiene_pagina_oficial": "SI" if official_base_url else "NO",
        "tiene_base_documentos": "SI" if (official_base_specific == "SI" or official_doc_urls) else "NO",
        "official_base_url": official_base_url,
        "official_base_specific": official_base_specific,
        "resolution_method": resolution.get("method", ""),
        "resolution_score": resolution.get("score", ""),
        "n_official_source_urls": len(official_source_urls),
        "n_official_doc_urls": len(official_doc_urls),
        "n_ache_attachment_pages": len(attachment_pages),
        "n_ache_attachment_pdfs": len(ache_pdfs),
        "ache_attachment_pages": " | ".join(attachment_pages),
        "ache_attachment_pdfs": " | ".join(ache_pdfs),
        "edital_pdf": first("edital_pdf"),
        "edital_pagina": first("edital_pagina"),
        "retificacao": first("retificacao"),
        "gabarito": first("gabarito"),
        "resultado_classificados": first("resultado_classificados"),
        "inscricao": first("inscricao"),
        "documento": first("documento"),
        "official_source_urls": " | ".join(official_source_urls),
        "official_doc_urls": " | ".join(official_doc_urls),
        "attachment_titles": " | ".join([x for x in attachment_titles if x])[:600],
    }


def write_excel(rows: List[Dict[str, object]], path: Path) -> Path:
    return write_table(rows, OUTPUT_FIELDS, path, sheet_name="Ache oficial")


def split_doc_urls(value: object) -> List[str]:
    urls: List[str] = []
    for item in str(value or "").split(" | "):
        item = item.strip()
        if item and item not in urls:
            urls.append(item)
    return urls


def expand_document_columns(rows: List[Dict[str, object]]) -> List[str]:
    max_docs = 0
    for row in rows:
        docs = split_doc_urls(row.get("official_doc_urls", ""))
        max_docs = max(max_docs, len(docs))
        row["_official_doc_list"] = docs

    fields = [field for field in OUTPUT_FIELDS if field in OUTPUT_FIELDS]
    for idx in range(1, max_docs + 1):
        fields.append(f"official_doc_{idx}")

    for row in rows:
        docs = row.pop("_official_doc_list", [])
        for idx in range(1, max_docs + 1):
            row[f"official_doc_{idx}"] = docs[idx - 1] if idx <= len(docs) else ""
    return fields


def write_outputs(rows: List[Dict[str, object]], xlsx_path: Path) -> Tuple[Path, Path]:
    fields = expand_document_columns(rows)
    xlsx_path = write_table(rows, fields, xlsx_path, sheet_name="Ache oficial")
    csv_path = write_table(rows, fields, xlsx_path.with_suffix(".csv"), sheet_name="Ache oficial")
    return xlsx_path, csv_path


def md_escape(value: object, max_len: int = 100) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "\\|").strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "..."
    return text


def write_report(rows: List[Dict[str, object]], path: Path) -> None:
    total = len(rows)
    aberto = sum(1 for r in rows if r["status"] == "aberto")
    andamento = sum(1 for r in rows if r["status"] == "andamento")
    with_official = sum(1 for r in rows if r["tiene_pagina_oficial"] == "SI")
    with_doc_base = sum(1 for r in rows if r["tiene_base_documentos"] == "SI")
    with_official_docs = sum(1 for r in rows if int(r["n_official_doc_urls"]) > 0)
    with_ache_pdf = sum(1 for r in rows if int(r["n_ache_attachment_pdfs"]) > 0)
    with_retif = sum(1 for r in rows if r["retificacao"])
    with_result = sum(1 for r in rows if r["resultado_classificados"] or r["gabarito"])
    resolved_from_index = sum(1 for r in rows if r["resolution_method"] == "resolved_from_official_index")
    direct_specific = sum(1 for r in rows if r["resolution_method"] == "direct_specific")

    lines = [
        "# Ache Concursos RS - pipeline oficial",
        "",
        "## Resumen",
        "",
        f"- Concursos procesados: {total}",
        f"- Abertos: {aberto}",
        f"- Andamento: {andamento}",
        f"- Con pagina oficial localizada: {with_official}",
        f"- Con base documental especifica o documentos oficiales: {with_doc_base}",
        f"- Con documentos oficiales extraidos desde la base: {with_official_docs}",
        f"- Con PDF/anexo encontrado en Ache: {with_ache_pdf}",
        f"- Con retificacao localizada: {with_retif}",
        f"- Con gabarito/resultado/classificados localizado: {with_result}",
        f"- URL especifica ya venia en Ache/anexo: {direct_specific}",
        f"- URL especifica resuelta desde indice oficial: {resolved_from_index}",
        "",
        "## Lista completa",
        "",
        "| # | Status | Orgao | Edital | Banca | Metodo | Oficial | Base doc | URL base oficial | PDF/Anexo Ache | Docs oficiales |",
        "|---:|---|---|---|---|---|---|---|---|---:|---:|",
    ]
    for r in rows:
        lines.append(
            "| {n} | {status} | {orgao} | {edital} | {banca} | {method} | {official} | {docbase} | {base} | {achepdf} | {docs} |".format(
                n=r["n"],
                status=md_escape(r["status"], 20),
                orgao=md_escape(r["orgao"], 88),
                edital=md_escape(r["edital"], 30),
                banca=md_escape(r["banca_guess"], 18),
                method=md_escape(r["resolution_method"], 28),
                official=r["tiene_pagina_oficial"],
                docbase=r["tiene_base_documentos"],
                base=md_escape(r["official_base_url"], 90),
                achepdf=r["n_ache_attachment_pdfs"],
                docs=r["n_official_doc_urls"],
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pipeline Ache RS -> paginas oficiales y documentos.")
    p.add_argument("--limit", type=int, default=0, help="Procesar solo los primeros N concursos.")
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--delay-min", type=float, default=0.35)
    p.add_argument("--delay-max", type=float, default=0.9)
    p.add_argument("--max-attachments-per-detail", type=int, default=8)
    p.add_argument("--max-edital-probe-docs", type=int, default=6,
                   help="Maximo de paginas oficiales enlazadas a abrir para detectar numero/prefijo de edital.")
    p.add_argument("--follow-generic-official", action="store_true", help="Tambien abrir bases oficiales genericas.")
    p.add_argument("--no-resolve-official", dest="resolve_official", action="store_false",
                   help="No convertir URLs genericas de banca en paginas especificas.")
    p.add_argument("--resolve-min-score", type=int, default=8,
                   help="Score minimo para aceptar match contra indice oficial.")
    p.add_argument("--no-cache", dest="cache", action="store_false",
                   help="Desactiva cache en memoria para requests repetidos.")
    p.add_argument("--checkpoint-every", type=int, default=25,
                   help="Escribe Excel/report parcial cada N concursos procesados.")
    p.set_defaults(resolve_official=True)
    p.set_defaults(cache=True)
    p.add_argument("--out", default=str(OUT_XLSX))
    p.add_argument("--report", default=str(OUT_MD))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    print("Ache RS official pipeline")
    print(f"  listado: {RS_LIST}")
    listing = collect_listing_rows(args)
    print(f"  concursos en listado: {len(listing)}")
    if not listing:
        print("  ERROR: no se pudo leer el listado de Ache; no se sobreescriben outputs.")
        return 2
    if args.limit:
        listing = listing[: args.limit]
        print(f"  limitado a: {len(listing)}")

    out_xlsx = Path(args.out).expanduser().resolve()
    out_md = Path(args.report).expanduser().resolve()
    rows: List[Dict[str, object]] = []
    for i, listing_row in enumerate(listing, start=1):
        result = process_one(listing_row, args)
        rows.append(result)
        marker = "OK" if result["tiene_pagina_oficial"] == "SI" else "--"
        doc_marker = "DOC" if result["tiene_base_documentos"] == "SI" else "   "
        print(
            f"  [{marker} {doc_marker}] {i:03d}/{len(listing):03d} "
            f"{result['status']:<10s} {str(result['orgao'])[:60]}"
        )
        if args.checkpoint_every and i % args.checkpoint_every == 0:
            out_xlsx, out_csv = write_outputs(rows, out_xlsx)
            write_report(rows, out_md)
            print(f"      checkpoint: {i}/{len(listing)} -> {out_xlsx.name}, {out_csv.name}")
        if i < len(listing):
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    out_xlsx, out_csv = write_outputs(rows, out_xlsx)
    write_report(rows, out_md)

    total = len(rows)
    with_official = sum(1 for r in rows if r["tiene_pagina_oficial"] == "SI")
    with_doc_base = sum(1 for r in rows if r["tiene_base_documentos"] == "SI")
    with_official_docs = sum(1 for r in rows if int(r["n_official_doc_urls"]) > 0)
    with_ache_pdf = sum(1 for r in rows if int(r["n_ache_attachment_pdfs"]) > 0)
    with_retif = sum(1 for r in rows if r["retificacao"])
    with_result = sum(1 for r in rows if r["resultado_classificados"] or r["gabarito"])
    resolved_from_index = sum(1 for r in rows if r["resolution_method"] == "resolved_from_official_index")
    direct_specific = sum(1 for r in rows if r["resolution_method"] == "direct_specific")

    print("\n=============== ACHE RS OFFICIAL PIPELINE ===============")
    print(f"  Concursos procesados                       : {total}")
    print(f"  Abertos                                    : {sum(1 for r in rows if r['status'] == 'aberto')}")
    print(f"  Andamento                                  : {sum(1 for r in rows if r['status'] == 'andamento')}")
    print(f"  Con pagina oficial localizada             : {with_official}")
    print(f"  Con base documental especifica/documentos : {with_doc_base}")
    print(f"  Con documentos oficiales extraidos        : {with_official_docs}")
    print(f"  Con PDF/anexo en Ache                     : {with_ache_pdf}")
    print(f"  Con retificacao localizada                : {with_retif}")
    print(f"  Con gabarito/resultado localizado         : {with_result}")
    print(f"  URL especifica ya venia en Ache/anexo     : {direct_specific}")
    print(f"  URL especifica resuelta desde oficial     : {resolved_from_index}")
    print(f"\n  Excel  : {out_xlsx}")
    print(f"  CSV    : {out_csv}")
    print(f"  Report : {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
