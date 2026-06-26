#!/usr/bin/env python3
"""
Fase 2 v2 - Resolver de huecos del CSV semilla de Ache.

Lee el CSV ajustado (salida del pipeline de Ache) y, SOLO para las filas sin
pagina oficial localizada (`tiene_pagina_oficial == NO`), intenta resolver la
fuente oficial de forma autonoma, sin pegar links a mano.

El script NO inventa ni copia URLs: para cada hueco aplica dos expertises que
extienden la maquinaria ya validada en `ache_rs_official_pipeline.py`:

  1. Indice de banca (incluye Objetiva, que faltaba): entra al indice oficial de
     la banca detectada y matchea el concurso por municipio / nº de edital usando
     el mismo `match_score` del pipeline. Si no hay match con señales
     distintivas suficientes, lo deja sin resolver (honesto).

  2. Construct-and-verify de prefeitura: deriva el slug del municipio desde el
     `orgao`, construye los hosts candidatos `<slug>.rs.gov.br`, los descarga y
     SOLO los acepta si la pagina carga y se verifica como prefeitura real. Luego
     cosecha enlaces de concurso/edital de esa pagina oficial. Si el host no
     existe o no verifica, el hueco queda sin resolver (honesto).

Las filas que ya tenian pagina oficial pasan sin cambios.

Salida:
  data/ache_rs_fase2_v2.csv
  data/ache_rs_fase2_v2.xlsx
  data/ache_rs_fase2_v2.md

Uso:
  python fase2_v2.py
  python fase2_v2.py --only-status aberto      # solo cerrar huecos de abertos
  python fase2_v2.py --limit 20                 # primeras 20 filas con hueco
"""
from __future__ import annotations

import argparse
import re
import time
import random
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ache_rs_official_pipeline as ache  # noqa: E402
import fase1_v1 as f1  # noqa: E402
from excel_utils import read_csv_dicts, write_table, csv_to_xlsx  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "data" / "ache_rs_official_pipeline_ajustado.csv"
DEFAULT_OUT = PROJECT_ROOT / "data" / "ache_rs_fase2_v2.csv"
DEFAULT_XLSX = PROJECT_ROOT / "data" / "ache_rs_fase2_v2.xlsx"
DEFAULT_MD = PROJECT_ROOT / "data" / "ache_rs_fase2_v2.md"

V2_FIELDS = [
    "v2_attempted", "v2_strategy", "v2_method", "v2_official_url",
    "v2_doc_urls", "v2_score", "v2_status",
]

CONCURSO_HINT = (
    "concurso", "processo seletivo", "processo-seletivo", "processoseletivo",
    "edital", "selecao", "seletivo", "inscri", "pss",
)

CERTAME_HINT = (
    "concurso", "processo seletivo", "processo-seletivo", "processoseletivo",
    "seletivo", "pss", "inscri",
)

# Editais que NO son concurso publico / processo seletivo de vagas. Un sitio de
# prefeitura mezcla todo esto; sin filtro, el resolver agarra una "soberana" o
# una licitacao en vez del concurso. Filtrar es precision, no trampa.
PREF_NOISE = (
    "soberana", "rainha", "bolsa", "patrocinio", "licitac", "pregao",
    "leilao", "chamamento", "dispensa", "inexigibilidade", "credenciamento",
    "concorrencia", "divida ativa", "covid", "coronavirus", "tomada de preco",
    "carta convite", "alienacao", "aldir blanc", "lei aldir", "projeto cultural",
    "selecao de projetos", "seleção de projetos", "cultura", "turismo",
    "concurso artistico", "concurso artístico", "artistico", "artístico",
    "antidengue", "audiencia publica", "audiência pública", "conselho tutelar",
)

PREF_PAGE_NOISE = (
    "aldir blanc", "lei aldir", "selecao de projetos", "seleção de projetos",
    "licitacao", "licitação", "pregao", "pregão", "dispensa de licitacao",
    "inexigibilidade", "chamamento publico", "concurso artistico",
    "concurso artístico", "antidengue", "audiencia publica", "audiência pública",
    "conselho tutelar",
)

PREF_SECTION_PATH_MARKERS = (
    "concursos-publicos", "concursos_publicos", "/concursos", "/concurso",
    "processos-seletivos", "processo-seletivo", "/seletivos", "/selecao",
    "/selecoes", "/pss", "/editais", "/edital",
)

# Patrones vistos en municipios RS reales. Se prueban como rutas candidatas del
# propio dominio municipal; no son resultados pegados a mano.
PREF_ROUTE_PROBES = (
    "site/concursos",
    "concursos",
    "concurso",
    "concursos-publicos",
    "processos-seletivos",
    "processo-seletivo",
    "publicacoes/concursos-publicos/",
    "portal-da-transparencia/concursos-publicos",
    "portal/editais/3",
    "pt_BR/concursos",
    "concurso/index?slug=lista-de-concursos-publicos",
    "mural/concursos-1",
    "transparencia/concursos",
)

# Bancas cuyo indice oficial sabemos cosechar (extiende el conocimiento del
# pipeline base con Objetiva, que generaba la mayor parte de los huecos).
BANCA_INDEX_EXTRA = {
    "objetiva": ["https://concursos.objetivas.com.br/"],
}
BANCA_GENERIC_EXTRA = {
    "objetiva": "https://concursos.objetivas.com.br/",
}

EXTRA_OFFICIAL_DOMAINS = {
    "ufrgs.br",
    "ufsm.br",
    "unipampa.edu.br",
    "ufcspa.edu.br",
    "processoseletivo.ufcspa.edu.br",
    "prefeitura.poa.br",
    "dopaonlineupload.procempa.com.br",
    "fhgv.com.br",
}

INSTITUTION_INDEXES = {
    "ufrgs": [
        "https://www.ufrgs.br/progesp/pagina-inicial/concursos-e-processos-seletivos/1939-2/",
        "https://www.ufrgs.br/progesp/",
    ],
    "ufsm": [
        "https://www.ufsm.br/pro-reitorias/progep/editais",
        "https://www.ufsm.br/trabalhe-na-ufsm/",
    ],
    "unipampa": [
        "https://unipampa.edu.br/portal/concursos",
        "https://unipampa.edu.br/portal/editais",
        "https://unipampa.edu.br/portal/t_concurso-docente",
    ],
    "ufcspa": [
        "https://ufcspa.edu.br/trabalhe-na-ufcspa/docentes",
        "https://processoseletivo.ufcspa.edu.br/",
    ],
    "fhgv": [
        "https://www.fhgv.com.br/home/portfolio_category/concursos-e-processos/",
        "https://www.fhgv.com.br/",
    ],
    "dmae_poa": [
        "https://prefeitura.poa.br/dmae/concursos-e-estagios",
        "https://prefeitura.poa.br/dmae/noticias",
    ],
    "faurgs": [
        "https://portalfaurgs.com.br/concursosfaurgs",
        "https://www.portalfaurgs.com.br/",
    ],
}

INSTITUTION_DIRECT_PATTERNS = {
    "ufrgs": [
        "https://www.ufrgs.br/progesp/edital-{num2}-{year}-concurso-publico-docente/",
    ],
    "ufsm": [
        "https://www.ufsm.br/pro-reitorias/progep/editais/{num3}-{year}",
    ],
}


def teach_engine() -> None:
    """Inyecta en el pipeline base las fuentes que le faltaban (no destructivo)."""
    for banca, indexes in BANCA_INDEX_EXTRA.items():
        ache.RESOLVER_INDEXES.setdefault(banca, [])
        ache.add_unique(ache.RESOLVER_INDEXES[banca], indexes)
    for banca, url in BANCA_GENERIC_EXTRA.items():
        ache.GENERIC_OFFICIAL_BY_BANCA.setdefault(banca, url)
    ache.OFFICIAL_PLATFORM_DOMAINS.update(EXTRA_OFFICIAL_DOMAINS)


def ascii_slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def city_from_row(orgao: str, detalle_ache: str, edital: str) -> str:
    city = ache.detect_city(" ".join([orgao or "", edital or ""]))
    if city:
        return city
    # Fallback: derivar del slug de la ficha de Ache.
    path = urlparse(detalle_ache or "").path
    slug = path.rstrip("/").split("/")[-1]
    if "-rs" not in slug:
        return ""
    before_rs = re.split(r"-rs(?:-|$)", slug, maxsplit=1)[0]
    parts = [p for p in before_rs.split("-") if p]
    stop = {
        "concurso", "processo", "seletivo", "edital", "prefeitura", "camara",
        "câmara", "abre", "abrem", "vagas", "vaga", "nivel", "niveis",
        "gaúcha", "gaucha", "publico", "publica",
    }
    while parts and parts[0] in stop:
        parts.pop(0)
    if not parts or parts[0] in {"gaucha", "gaúcha"}:
        return ""
    city = " ".join(parts).strip()
    if any(token.isdigit() for token in parts) or len(city) < 3:
        return ""
    return city


def prefeitura_hosts(city: str) -> List[str]:
    base = ascii_slug(city)
    if len(base) < 3:
        return []
    return [
        f"https://www.{base}.rs.gov.br/",
        f"https://{base}.rs.gov.br/",
    ]


def verify_prefeitura(res: "f1.FetchResult", city: str) -> bool:
    if not res or not res.body or res.status not in {200, 301, 302}:
        return False
    text = f1.visible_text(res.body) or ""
    norm = ache.normalize_text(text[:30000])
    if len(norm) < 200:
        return False
    if not any(k in norm for k in ("prefeitura", "municipio", "municipal", "gov br")):
        return False
    return True


def edital_parts(edital_num: str) -> Tuple[str, str]:
    match = re.match(r"\s*(\d{1,4})\s*/\s*(20\d{2})\s*", edital_num or "")
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def detect_edital_num_any(*values: str) -> str:
    for value in values:
        found = ache.detect_edital_num(value or "")
        if found:
            return found
        match = re.search(r"(?:edital\s*)?(?:n[º°o\.\s]*)?(\d{1,4})\s*[-/]\s*(20\d{2})", value or "", re.I)
        if match:
            return f"{int(match.group(1)):02d}/{match.group(2)}"
    return ""


def target_years(*values: str) -> List[str]:
    years: List[str] = []
    for value in values:
        for year in re.findall(r"20\d{2}", value or ""):
            if year not in years:
                years.append(year)
    return years


def has_noise(blob_norm: str) -> bool:
    return any(noise in blob_norm for noise in PREF_NOISE)


def has_page_noise(blob_norm: str) -> bool:
    return any(noise in blob_norm for noise in PREF_PAGE_NOISE)


def has_year_conflict(blob_norm: str, years: Sequence[str]) -> bool:
    found = set(re.findall(r"20\d{2}", blob_norm or ""))
    if not found or not years:
        return False
    return found.isdisjoint(set(years))


def has_url_year_conflict(url: str, years: Sequence[str]) -> bool:
    parsed = urlparse(url)
    return has_year_conflict(ache.normalize_text(parsed.path + " " + parsed.query), years)


def is_section_url(url: str) -> bool:
    parsed = urlparse(url)
    path_q = ache.normalize_text(parsed.path + " " + parsed.query)
    return any(ache.normalize_text(marker.strip("/")) in path_q for marker in PREF_SECTION_PATH_MARKERS)


def is_generic_section_url(url: str) -> bool:
    parsed = urlparse(url)
    path = ache.normalize_text(parsed.path)
    query = ache.normalize_text(parsed.query)
    segments = [seg for seg in path.split("/") if seg]
    last = segments[-1] if segments else ""
    generic_last = {
        "concursos", "concurso", "concursos publicos", "concurso publico",
        "processos seletivos", "processo seletivo",
    }
    if last in generic_last:
        return True
    if "lista de concursos publicos" in query:
        return True
    if "lista/" in path and "concursos publicos" in path:
        return True
    if "mural/concursos" in path or "site/concursos" in path or "pt br/concursos" in path:
        return True
    return False


def prefeitura_probe_urls(base_url: str) -> List[str]:
    out: List[str] = []
    for probe in PREF_ROUTE_PROBES:
        url = urljoin(base_url.rstrip("/") + "/", probe)
        if url not in out:
            out.append(url)
    return out


def visible_norm(raw_html: str) -> str:
    return ache.normalize_text(f1.visible_text(raw_html or "") or "")


def page_has_concurso_signal(url: str, text_norm: str) -> bool:
    blob = ache.normalize_text(url + " " + text_norm[:5000])
    return any(hint in blob for hint in CERTAME_HINT)


def document_url_signal(url: str) -> bool:
    blob = ache.normalize_text(url)
    return any(
        signal in blob
        for signal in (
            "edital", "concurso", "processo seletivo", "pss", "resultado",
            "gabarito", "homolog", "classific", "retific", "convoca",
        )
    )


def validate_official_target_page(
    url: str,
    res: "f1.FetchResult",
    query: str,
    edital_num: str,
    years: Sequence[str],
    allow_generic_section: bool = True,
) -> bool:
    url_norm = ache.normalize_text(url)
    if has_noise(url_norm) or has_url_year_conflict(url, years):
        return False
    if url.lower().endswith(".pdf"):
        if edital_num and not edital_num_in(url_norm, edital_num):
            return False
        if not edital_num and not document_url_signal(url):
            return False
        return not has_year_conflict(url_norm, years)
    if not res or not res.body or res.status not in {200, 301, 302}:
        return False
    text_norm = visible_norm(res.body)
    blob_norm = ache.normalize_text(url + " " + text_norm[:12000])
    if len(text_norm) < 80:
        return False
    if allow_generic_section and is_generic_section_url(url) and page_has_concurso_signal(url, text_norm):
        return True
    if has_page_noise(blob_norm):
        return False
    if has_year_conflict(blob_norm, years):
        return False
    if edital_num and edital_num_in(blob_norm, edital_num):
        return True
    if allow_generic_section and is_generic_section_url(url) and page_has_concurso_signal(url, text_norm):
        return True
    return ache.match_score(blob_norm, query, edital_num) >= 10 and ache.has_distinctive_match(blob_norm, query)


def harvest_concurso_links(
    raw_html: str,
    base_url: str,
    query: str,
    edital_num: str,
) -> List[Tuple[str, int, bool]]:
    """Devuelve [(url, score, is_specific)] de enlaces que huelen a concurso."""
    out: List[Tuple[str, int, bool]] = []
    seen: set = set()
    for url, context in ache.candidate_links_from_index(raw_html, base_url):
        if url in seen:
            continue
        seen.add(url)
        blob = (url + " " + context).lower()
        if not any(k in blob for k in CONCURSO_HINT):
            continue
        if ache.is_noise_url(url):
            continue
        score = ache.match_score(context + " " + url, query, edital_num)
        out.append((url, score, ache.is_specific_official_url(url)))
    out.sort(key=lambda t: (t[2], t[1]), reverse=True)
    return out


def resolve_banca(
    banca: str,
    query: str,
    edital_num: str,
    city: str,
    years: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, object]:
    generic = ache.GENERIC_OFFICIAL_BY_BANCA.get(banca)
    if not generic:
        return {}
    resolution = ache.resolve_specific_official_url(generic, query, banca, edital_num, args)
    url = str(resolution.get("url") or "")
    method = str(resolution.get("method") or "")
    specific = ache.is_specific_official_url(url)
    if not url:
        return {}
    docs = [url] if specific else []
    if specific and banca == "objetiva":
        if not city:
            specific = False
            docs = []
        else:
            res = ache.fetch(url, args)
            body_norm = visible_norm(res.body) if res.body else ""
            city_norm = ache.normalize_text(city)
            blob_norm = ache.normalize_text(url + " " + body_norm[:12000])
            if (
                res.status not in {200, 301, 302}
                or city_norm not in blob_norm
                or has_url_year_conflict(url, years)
                or (edital_num and not edital_num_in(blob_norm, edital_num))
            ):
                url = generic
                method = "generic_unresolved"
                specific = False
                docs = []
    return {
        "strategy": "banca_index",
        "method": method,
        "official_url": url,
        "doc_urls": docs,
        "score": int(resolution.get("score") or 0),
        "specific": specific,
    }


def edital_num_in(blob_norm: str, edital_num: str) -> bool:
    """True si el numero de edital (ej '05/2026') aparece en el blob normalizado,
    tolerando separadores '/', '-', espacio entre numero y año."""
    match = re.match(r"(\d{1,4})\s*/\s*(\d{4})", edital_num or "")
    if not match:
        return False
    num = int(match.group(1))
    year = match.group(2)
    return re.search(rf"\b0*{num}\b\D{{0,4}}{year}\b", blob_norm) is not None


def url_concurso_signal(urln: str) -> bool:
    """True si la PROPIA URL (no su contexto) lleva senal de concurso/seletivo.

    En un CMS de prefeitura el texto del menu mete 'concursos publicos' cerca de
    enlaces no relacionados (acessibilidade, secretarias). Confiar en el contexto
    da falsos positivos; la senal tiene que estar en el path de la URL.
    """
    return any(ache.normalize_text(marker.strip("/")) in urln for marker in PREF_SECTION_PATH_MARKERS)


def select_prefeitura_target(
    links: List[Tuple[str, str]],
    query: str,
    edital_num: str,
    years: Sequence[str],
) -> Tuple[str, str, int]:
    """Elige el destino correcto dentro de una prefeitura.

    Devuelve (url, kind, score) donde kind es:
      - 'edital'   : PDF/pagina cuyo NUMERO de edital aparece en la propia URL
      - 'section'  : seccion oficial cuyo PATH lleva senal de concurso/seletivo
      - ''         : nada confiable
    La discriminacion vive en la URL, no en el texto del menu. NUNCA devuelve un
    articulo suelto que solo coincide por municipio o por contexto del nav (eso
    daba falsos positivos como 'soberanas', 'bolsa patrocinio', 'acessibilidade').
    """
    edital_url = ""
    edital_score = -1
    section_url = ""
    section_score = -1
    for url, ctx in links:
        urln = ache.normalize_text(url)
        blobn = ache.normalize_text(url + " " + ctx)
        generic_section = is_generic_section_url(url)
        if has_noise(blobn) or has_url_year_conflict(url, years) or (has_year_conflict(blobn, years) and not generic_section):
            continue
        if ache.is_noise_url(url) or not ache.is_official(url):
            continue
        sig = url_concurso_signal(urln)
        score = ache.match_score(ctx + " " + url, query, edital_num)
        exact = bool(edital_num and edital_num_in(blobn, edital_num))
        if edital_num and not exact and not generic_section:
            continue
        # Edital fuerte: numero exacto en URL/contexto y senal de certamen.
        if exact and (sig or url.lower().endswith(".pdf")):
            if score > edital_score:
                edital_url, edital_score = url, score
            continue
        # Seccion de concursos: senal en la URL + URL especifica.
        if sig and ache.is_specific_official_url(url) and score > section_score:
            section_url, section_score = url, score + (6 if generic_section else 0)
    if edital_url:
        return edital_url, "edital", max(edital_score, 0)
    if section_url:
        return section_url, "section", max(section_score, 0)
    return "", "", 0


def harvest_pdfs(raw_html: str, base_url: str, edital_num: str, years: Sequence[str] = ()) -> List[str]:
    pdfs: List[str] = []
    for url, ctx in ache.candidate_links_from_index(raw_html, base_url):
        if not url.lower().endswith(".pdf"):
            continue
        if not edital_num and not document_url_signal(url):
            continue
        blobn = ache.normalize_text(url + " " + ctx)
        if has_noise(blobn) or has_year_conflict(blobn, years):
            continue
        if edital_num and not edital_num_in(blobn, edital_num):
            # Sin numero coincidente solo aceptamos PDFs que se autodescriban edital.
            if "edital" not in blobn:
                continue
        if url not in pdfs:
            pdfs.append(url)
    return pdfs


def build_prefeitura_result(
    target_url: str,
    kind: str,
    score: int,
    query: str,
    edital_num: str,
    years: Sequence[str],
    args: argparse.Namespace,
    known_body: str = "",
) -> Dict[str, object]:
    docs: List[str] = [target_url]
    if target_url.lower().endswith(".pdf"):
        sub = f1.FetchResult("memory", "easy", "synthetic_pdf", status=200, final_url=target_url)
    elif known_body:
        sub = f1.FetchResult("memory", "easy", "synthetic", status=200, final_url=target_url, body=known_body)
    else:
        time.sleep(random.uniform(args.delay_min, args.delay_max))
        sub = ache.fetch(target_url, args)
    if not validate_official_target_page(target_url, sub, query, edital_num, years):
        return {}
    if sub.body:
        ache.add_unique(docs, harvest_pdfs(sub.body, sub.final_url or target_url, edital_num, years))
    method = "prefeitura_edital" if kind == "edital" else "prefeitura_concursos_section"
    return {
        "strategy": "prefeitura",
        "method": method,
        "official_url": target_url,
        "doc_urls": docs[:30],
        "score": score,
        "specific": True,
    }


def resolve_prefeitura(
    city: str,
    query: str,
    edital_num: str,
    years: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, object]:
    home_fallback: Dict[str, object] = {}
    for host in prefeitura_hosts(city):
        res = ache.fetch(host, args)
        if not verify_prefeitura(res, city):
            continue
        base = res.final_url or host
        if not home_fallback:
            home_fallback = {
                "strategy": "prefeitura",
                "method": "prefeitura_home",
                "official_url": base,
                "doc_urls": [],
                "score": 0,
                "specific": False,
            }

        links = ache.candidate_links_from_index(res.body or "", base)
        target_url, kind, score = select_prefeitura_target(links, query, edital_num, years)
        if target_url:
            specific = build_prefeitura_result(target_url, kind, score, query, edital_num, years, args)
            if specific:
                return specific

        for probe_url in prefeitura_probe_urls(base):
            time.sleep(random.uniform(args.delay_min, args.delay_max))
            probe = ache.fetch(probe_url, args)
            if not probe.body or probe.status not in {200, 301, 302}:
                continue
            page_url = probe.final_url or probe_url
            page_body = probe.body or ""
            probe_text = visible_norm(page_body)
            if has_page_noise(ache.normalize_text(page_url + " " + probe_text[:6000])):
                continue
            page_res = f1.FetchResult("memory", "easy", "synthetic", status=200, final_url=page_url, body=page_body)
            if is_section_url(page_url) and validate_official_target_page(
                page_url, page_res, query, edital_num, years, allow_generic_section=True
            ):
                score = ache.match_score(page_url + " " + visible_norm(page_body)[:4000], query, edital_num)
                specific = build_prefeitura_result(page_url, "section", score, query, edital_num, years, args, page_body)
                if specific:
                    return specific

            links = ache.candidate_links_from_index(page_body or "", page_url)
            target_url, kind, score = select_prefeitura_target(links, query, edital_num, years)
            if target_url:
                specific = build_prefeitura_result(target_url, kind, score, query, edital_num, years, args)
                if specific:
                    return specific
    return home_fallback


def detect_institution(row: Dict[str, str]) -> str:
    blob = ache.normalize_text(" ".join([
        row.get("orgao", ""),
        row.get("detalle_ache", ""),
        row.get("banca_guess", ""),
    ]))
    if "universidade federal de ciencias da saude" in blob or "ufcspa" in blob:
        return "ufcspa"
    if "universidade federal do rio grande do sul" in blob or "ufrgs" in blob:
        return "ufrgs"
    if "universidade federal de santa maria" in blob or "ufsm" in blob:
        return "ufsm"
    if "universidade federal do pampa" in blob or "unipampa" in blob:
        return "unipampa"
    if "fundacao hospitalar getulio vargas" in blob or "fhgv" in blob:
        return "fhgv"
    if "departamento municipal de agua" in blob or "dmae porto alegre" in blob:
        return "dmae_poa"
    if "faurgs" in blob or "fundacao de apoio da universidade federal" in blob:
        return "faurgs"
    return ""


def direct_institution_urls(kind: str, edital_num: str) -> List[str]:
    num, year = edital_parts(edital_num)
    if not num or not year:
        return []
    values = {
        "num": str(int(num)),
        "num2": f"{int(num):02d}",
        "num3": f"{int(num):03d}",
        "year": year,
    }
    out: List[str] = []
    for pattern in INSTITUTION_DIRECT_PATTERNS.get(kind, []):
        url = pattern.format(**values)
        if url not in out:
            out.append(url)
    return out


def select_institution_target(
    links: List[Tuple[str, str]],
    query: str,
    edital_num: str,
    years: Sequence[str],
    args: argparse.Namespace,
) -> Tuple[str, int]:
    best_url = ""
    best_score = -1
    for url, ctx in links:
        if ache.is_noise_url(url) or not ache.is_official(url):
            continue
        blobn = ache.normalize_text(url + " " + ctx)
        if has_noise(blobn) or has_year_conflict(blobn, years):
            continue
        exact = bool(edital_num and edital_num_in(blobn, edital_num))
        if edital_num and not exact:
            continue
        has_certame_signal = any(hint in blobn for hint in CONCURSO_HINT)
        if not (exact or has_certame_signal or url.lower().endswith(".pdf")):
            continue
        if url.lower().endswith(".pdf") and not edital_num and not document_url_signal(url):
            continue
        score = ache.match_score(ctx + " " + url, query, edital_num)
        if exact:
            score += 20
        if not exact and (score < args.resolve_min_score or not ache.has_distinctive_match(blobn, query)):
            continue
        if score > best_score:
            best_url, best_score = url, score
    return best_url, max(best_score, 0)


def resolve_institution(
    row: Dict[str, str],
    query: str,
    edital_num: str,
    years: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, object]:
    kind = detect_institution(row)
    if not kind:
        return {}

    direct_urls = direct_institution_urls(kind, edital_num)
    for url in direct_urls:
        res = ache.fetch(url, args)
        if validate_official_target_page(url, res, query, edital_num, years):
            docs = [url]
            if res.body:
                ache.add_unique(docs, harvest_pdfs(res.body, res.final_url or url, edital_num, years))
            return {
                "strategy": "institution",
                "method": f"{kind}_direct_pattern",
                "official_url": res.final_url or url,
                "doc_urls": docs[:30],
                "score": 99,
                "specific": True,
            }

    home_fallback: Dict[str, object] = {}
    for index_url in INSTITUTION_INDEXES.get(kind, []):
        res = ache.fetch(index_url, args)
        if not res.body or res.status not in {200, 301, 302}:
            continue
        final = res.final_url or index_url
        if not home_fallback:
            home_fallback = {
                "strategy": "institution",
                "method": f"{kind}_home",
                "official_url": final,
                "doc_urls": [],
                "score": 0,
                "specific": False,
            }
        links = ache.candidate_links_from_index(res.body, final)
        target_url, score = select_institution_target(links, query, edital_num, years, args)
        if not target_url:
            time.sleep(random.uniform(args.delay_min, args.delay_max))
            continue
        if target_url.lower().endswith(".pdf"):
            sub = f1.FetchResult("memory", "easy", "synthetic_pdf", status=200, final_url=target_url)
        elif target_url == final:
            sub = f1.FetchResult("memory", "easy", "synthetic", status=200, final_url=final, body=res.body)
        else:
            time.sleep(random.uniform(args.delay_min, args.delay_max))
            sub = ache.fetch(target_url, args)
        if not validate_official_target_page(target_url, sub, query, edital_num, years):
            continue
        docs = [target_url]
        if sub.body:
            ache.add_unique(docs, harvest_pdfs(sub.body, sub.final_url or target_url, edital_num, years))
        return {
            "strategy": "institution",
            "method": f"{kind}_official_index",
            "official_url": sub.final_url or target_url,
            "doc_urls": docs[:30],
            "score": score,
            "specific": True,
        }
    return home_fallback


def resolve_gap(row: Dict[str, str], args: argparse.Namespace) -> Dict[str, object]:
    orgao = row.get("orgao", "")
    edital = row.get("edital", "")
    nivel = row.get("nivel", "")
    banca = (row.get("banca_guess", "") or "").strip().lower()
    detalle = row.get("detalle_ache", "")
    city = city_from_row(orgao, detalle, edital)
    edital_num = detect_edital_num_any(edital, orgao, detalle)
    query = " ".join([orgao, edital, nivel, city, detalle]).strip()
    years = target_years(query, edital_num)

    # 1) Si Ache detecto banca y la sabemos resolver, intentar el indice oficial.
    if banca and (banca in ache.GENERIC_OFFICIAL_BY_BANCA):
        result = resolve_banca(banca, query, edital_num, city, years, args)
        if result and result.get("specific"):
            return result
        banca_home = result  # puede ser generic_unresolved; lo guardamos de respaldo
    else:
        banca_home = {}

    # 2) Instituciones publicas no municipales (universidades, autarquias, fundaciones).
    inst = resolve_institution(row, query, edital_num, years, args)
    if inst and inst.get("specific"):
        return inst
    if inst and inst.get("official_url") and not banca_home:
        banca_home = inst

    # 3) Prefeitura directa / sin banca: construct-and-verify del dominio oficial.
    if city:
        pref = resolve_prefeitura(city, query, edital_num, years, args)
        if pref and pref.get("specific"):
            return pref
        if pref and pref.get("method") == "prefeitura_home" and not banca_home:
            banca_home = pref

    # 4) Mejor esfuerzo: dominio oficial generico (banca/prefeitura/institucion)
    #    sin pagina especifica. Se guarda en V2 como home, pero no debe convertirse
    #    en pagina oficial verificada del concurso.
    if banca_home and banca_home.get("official_url"):
        return banca_home
    return {}


def split_docs(value: object) -> List[str]:
    out: List[str] = []
    for item in str(value or "").split(" | "):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def apply_resolution(row: Dict[str, str], result: Dict[str, object]) -> None:
    official_url = str(result.get("official_url") or "")
    specific = bool(result.get("specific"))
    docs = list(result.get("doc_urls") or [])

    row["tiene_pagina_oficial"] = "SI"
    row["official_base_url"] = official_url
    row["official_base_specific"] = "SI" if specific else "NO"
    row["resolution_method"] = str(result.get("method") or "")
    row["resolution_score"] = str(result.get("score") or "")

    sources = split_docs(row.get("official_source_urls"))
    ache.add_unique(sources, [official_url])
    row["official_source_urls"] = " | ".join(sources)
    row["n_official_source_urls"] = str(len(sources))

    if docs:
        existing = split_docs(row.get("official_doc_urls"))
        ache.add_unique(existing, docs)
        row["official_doc_urls"] = " | ".join(existing)
        row["n_official_doc_urls"] = str(len(existing))
        row["tiene_base_documentos"] = "SI"
        if not (row.get("edital_pdf") or "").strip():
            pdfs = [u for u in existing if u.lower().endswith(".pdf")]
            if pdfs:
                row["edital_pdf"] = pdfs[0]
        if not (row.get("edital_pagina") or "").strip():
            row["edital_pagina"] = docs[0]


def is_gap(row: Dict[str, str]) -> bool:
    return (row.get("tiene_pagina_oficial", "") or "").strip().upper() != "SI"


def recompute_doc_columns(rows: List[Dict[str, str]], base_fields: List[str]) -> List[str]:
    max_docs = 0
    for row in rows:
        docs = split_docs(row.get("official_doc_urls"))
        max_docs = max(max_docs, len(docs))
        for key in list(row.keys()):
            if key.startswith("official_doc_") and key[len("official_doc_"):].isdigit():
                row.pop(key, None)
        for idx, url in enumerate(docs, start=1):
            row[f"official_doc_{idx}"] = url
    fields = list(base_fields)
    for idx in range(1, max_docs + 1):
        fields.append(f"official_doc_{idx}")
    for row in rows:
        for idx in range(1, max_docs + 1):
            row.setdefault(f"official_doc_{idx}", "")
    return fields


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fase 2 v2 - resolver de huecos del CSV semilla.")
    p.add_argument("--input", default=str(DEFAULT_INPUT))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    p.add_argument("--report", default=str(DEFAULT_MD))
    p.add_argument("--only-status", choices=["aberto", "andamento", "all"], default="all")
    p.add_argument("--limit", type=int, default=0, help="Procesar solo los primeros N huecos.")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--delay-min", type=float, default=0.35)
    p.add_argument("--delay-max", type=float, default=0.9)
    p.add_argument("--resolve-min-score", type=int, default=8)
    p.add_argument("--no-cache", dest="cache", action="store_false")
    p.add_argument("--checkpoint-every", type=int, default=15)
    p.set_defaults(cache=True)
    return p.parse_args()


def write_report(rows: List[Dict[str, str]], path: Path, stats: Dict[str, int]) -> None:
    lines = [
        "# Fase 2 v2 - resolucion de huecos del CSV semilla",
        "",
        "## Resumen",
        "",
        f"- Filas totales: {stats['total']}",
        f"- Filas con hueco al inicio (sin pagina oficial): {stats['gaps']}",
        f"- Huecos intentados: {stats['attempted']}",
        f"- Resueltos a pagina especifica: {stats['resolved_specific']}",
        f"-   por indice de banca: {stats['by_banca']}",
        f"-   por prefeitura .rs.gov.br: {stats['by_prefeitura']}",
        f"-   por institucion oficial: {stats['by_institution']}",
        f"- Solo dominio oficial generico (home, sin pagina especifica): {stats['home_only']}",
        f"- Sin resolver (quedan honestos en null): {stats['unresolved']}",
        f"- Cobertura final con pagina oficial: {stats['final_official']}/{stats['total']}",
        "",
        "## Huecos resueltos en esta corrida",
        "",
        "| # | Status | Orgao | Estrategia | Metodo | URL oficial | Docs |",
        "|---:|---|---|---|---|---|---:|",
    ]
    for row in rows:
        if (row.get("v2_status") or "") not in {"resolved", "home"}:
            continue
        lines.append(
            "| {n} | {status} | {orgao} | {strat} | {method} | {url} | {docs} |".format(
                n=row.get("n", ""),
                status=ache.md_escape(row.get("status", ""), 12),
                orgao=ache.md_escape(row.get("orgao", ""), 60),
                strat=ache.md_escape(row.get("v2_strategy", ""), 16),
                method=ache.md_escape(row.get("v2_method", ""), 22),
                url=ache.md_escape(row.get("v2_official_url", ""), 80),
                docs=len(split_docs(row.get("v2_doc_urls"))),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_args()
    teach_engine()

    in_path = Path(args.input).expanduser().resolve()
    if not in_path.exists():
        print(f"No encuentro el CSV de entrada: {in_path}")
        return 1
    rows = read_csv_dicts(in_path)
    if not rows:
        print("CSV de entrada vacio.")
        return 1

    base_fields = [f for f in rows[0].keys() if not (
        f.startswith("official_doc_") and f[len("official_doc_"):].isdigit()
    )]
    for extra in V2_FIELDS:
        if extra not in base_fields:
            base_fields.append(extra)
    for row in rows:
        for extra in V2_FIELDS:
            row.setdefault(extra, "")
        row["v2_attempted"] = "NO"
        row["v2_status"] = "kept"

    gaps = [r for r in rows if is_gap(r)]
    if args.only_status != "all":
        gaps = [r for r in gaps if (r.get("status", "") or "").strip().lower() == args.only_status]
    if args.limit:
        gaps = gaps[: args.limit]

    print(f"Fase 2 v2 - resolver de huecos")
    print(f"  entrada : {in_path.name} ({len(rows)} filas)")
    print(f"  huecos  : {len([r for r in rows if is_gap(r)])} (procesando {len(gaps)})")
    print(f"  requests: {'OK' if f1.rq is not None else 'FALTA'}  curl_cffi: {'OK' if f1.creq is not None else 'FALTA'}\n")

    stats = {
        "total": len(rows),
        "gaps": len([r for r in rows if is_gap(r)]),
        "attempted": 0,
        "resolved_specific": 0,
        "by_banca": 0,
        "by_prefeitura": 0,
        "by_institution": 0,
        "home_only": 0,
        "unresolved": 0,
        "final_official": 0,
    }

    out_path = Path(args.out).expanduser().resolve()
    for i, row in enumerate(gaps, start=1):
        stats["attempted"] += 1
        row["v2_attempted"] = "SI"
        try:
            result = resolve_gap(row, args)
        except Exception as exc:  # noqa: BLE001 - una fila no debe tumbar la corrida
            result = {}
            row["v2_status"] = f"error:{type(exc).__name__}"
        if result and result.get("official_url"):
            row["v2_strategy"] = str(result.get("strategy") or "")
            row["v2_method"] = str(result.get("method") or "")
            row["v2_official_url"] = str(result.get("official_url") or "")
            row["v2_doc_urls"] = " | ".join(result.get("doc_urls") or [])
            row["v2_score"] = str(result.get("score") or "")
            if result.get("specific"):
                apply_resolution(row, result)
                row["v2_status"] = "resolved"
                stats["resolved_specific"] += 1
                if result.get("strategy") == "banca_index":
                    stats["by_banca"] += 1
                elif result.get("strategy") == "prefeitura":
                    stats["by_prefeitura"] += 1
                elif result.get("strategy") == "institution":
                    stats["by_institution"] += 1
            else:
                row["v2_status"] = "home"
                stats["home_only"] += 1
            mark = "OK" if result.get("specific") else "~~"
        else:
            if not row["v2_status"].startswith("error"):
                row["v2_status"] = "unresolved"
            stats["unresolved"] += 1
            mark = "--"
        print(f"  [{mark}] {i:03d}/{len(gaps):03d} {str(row.get('status','')):<10s} "
              f"{str(row.get('orgao',''))[:48]:48s} -> {row.get('v2_method','') or row.get('v2_status','')}")
        if args.checkpoint_every and i % args.checkpoint_every == 0:
            fields = recompute_doc_columns(list(rows), base_fields)
            write_table(rows, fields, out_path, sheet_name="Fase2 v2")
            print(f"      checkpoint {i}/{len(gaps)} -> {out_path.name}")
        if i < len(gaps):
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    stats["final_official"] = sum(1 for r in rows if (r.get("tiene_pagina_oficial", "") or "").upper() == "SI")

    fields = recompute_doc_columns(rows, base_fields)
    write_table(rows, fields, out_path, sheet_name="Fase2 v2")
    xlsx_path = csv_to_xlsx(out_path, Path(args.xlsx).expanduser().resolve())
    write_report(rows, Path(args.report).expanduser().resolve(), stats)

    print("\n=============== FASE 2 v2 - RESOLUCION DE HUECOS ===============")
    print(f"  Huecos intentados              : {stats['attempted']}")
    print(f"  Resueltos a pagina especifica  : {stats['resolved_specific']} "
          f"(banca={stats['by_banca']}, prefeitura={stats['by_prefeitura']}, "
          f"institucion={stats['by_institution']})")
    print(f"  Solo dominio oficial (home)    : {stats['home_only']}")
    print(f"  Sin resolver (honestos null)   : {stats['unresolved']}")
    print(f"  Cobertura final pagina oficial : {stats['final_official']}/{stats['total']}")
    print(f"\n  CSV   : {out_path}")
    print(f"  Excel : {xlsx_path}")
    print(f"  Report: {Path(args.report).expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
