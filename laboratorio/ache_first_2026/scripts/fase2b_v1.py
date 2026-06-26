#!/usr/bin/env python3
"""
Fase 2B v1 — Discovery RADAR + match con oficiales (scope: Rio Grande do Sul).

Regla dura del proyecto:
  portal_radar puede CREAR candidatos, pero NO puede crear concursos
  verificados por si solo. Un candidato solo sube a "official_found" o
  "pdf_found" si encontramos evidencia en un dominio oficial
  (.gov.br, banca conocida o diario oficial).

Que hace:
  1. Lee los portales radar (ache, pci, concursosnobrasil, gran, direcao,
     folha_dirigida...) accesibles por requests/curl.
  2. Filtra a candidatos que huelen a Rio Grande do Sul (slug /rs,
     "rio grande do sul" o nombre de municipio gaucho).
  3. Sigue link de profundidad 1 al articulo y extrae pistas:
     orgao, numero de edital, banca, ciudad, snippet.
  4. Busca dentro del articulo links salientes a dominios oficiales
     (.gov.br / banca) y PDFs oficiales.
  5. Puntua: +oficial, +pdf, +banca, +edital_num; -portal_only.
  6. Asigna verification_status: pdf_found / official_found / unverified.

Salida: data/candidate_urls_rs.xlsx (schema enriquecido).

NO descarga PDFs (Fase 3). Solo descubre y enlaza con lo oficial.

Uso:
  python fase2b_v1.py
  python fase2b_v1.py --max-per-portal 40
  python fase2b_v1.py --only ache,pci
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import write_table  # noqa: E402
import fase1_v1 as f1  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG = PROJECT_ROOT / "data" / "catalog" / "sources_catalog_phase1.csv"
DEFAULT_OUT = PROJECT_ROOT / "data" / "candidate_urls_rs.xlsx"

UF = "RS"

# Paginas-indice de RS por portal (confirmadas con 200 + contenido gaucho).
# Entrar por aqui en vez de la homepage multiplica el yield de RS.
RS_ENTRY = {
    "ache": "https://www.acheconcursos.com.br/concursos-rio-grande-do-sul",
    "pci": "https://www.pciconcursos.com.br/concursos/rio-grande-do-sul/",
    "concursosnobrasil": "https://concursosnobrasil.com/concursos/rs/",
    "gran": "https://blog.grancursosonline.com.br/concursos-rs/",
}

# Municipios de RS (los mas poblados / con concursos frecuentes). Sirven como
# senal fuerte de que un candidato es gaucho.
RS_CITIES = [
    "porto alegre", "caxias do sul", "pelotas", "canoas", "santa maria",
    "gravatai", "gravataí", "viamao", "viamão", "novo hamburgo", "sao leopoldo",
    "são leopoldo", "rio grande", "alvorada", "passo fundo", "sapucaia do sul",
    "uruguaiana", "santa cruz do sul", "cachoeirinha", "bage", "bagé",
    "bento goncalves", "bento gonçalves", "erechim", "guaiba", "guaíba",
    "cachoeira do sul", "santana do livramento", "ijui", "ijuí", "esteio",
    "alegrete", "lajeado", "carazinho", "venancio aires", "venâncio aires",
    "farroupilha", "santa rosa", "vacaria", "montenegro", "camaqua", "camaquã",
    "sao borja", "são borja", "taquara", "gramado", "canela", "torres",
    "tres coroas", "três coroas", "osorio", "osório", "capao da canoa",
    "capão da canoa", "tramandai", "tramandaí", "parobe", "parobé", "estancia velha",
    "campo bom", "dois irmaos", "dois irmãos", "igrejinha", "rolante",
    "santo angelo", "santo ângelo", "cruz alta", "frederico westphalen",
    "tres passos", "três passos", "santiago", "sao gabriel", "são gabriel",
    "dom pedrito", "rosario do sul", "rosário do sul", "encruzilhada do sul",
]

RS_SLUG_SIGNALS = [
    "rio grande do sul", "rio-grande-do-sul",
    "/rs/", "/rs-", "-rs-", "-rs/", "-rs.", "_rs_", "/concursos-rs",
    "concursos-rio-grande-do-sul", "(rs)", " rs ", "/rs\"",
]

# Bancas tipicamente gauchas: si aparecen, refuerzan que es RS.
RS_BANCAS = ["fundatec", "legalle", "faurgs", "objetiva", "lasalle", "la salle"]

# Catalogo de bancas (keyword -> dominio oficial) para detectar banca y link oficial.
BANCA_DOMAINS = {
    "fundatec": "fundatec.org.br",
    "legalle": "legalleconcursos.com.br",
    "lasalle": "fundacaolasalle.org.br",
    "la salle": "fundacaolasalle.org.br",
    "fundacao la salle": "fundacaolasalle.org.br",
    "faurgs": "portalfaurgs.com.br",
    "objetiva": "objetivas.com.br",
    "fgv": "fgv.br",
    "cebraspe": "cebraspe.org.br",
    "cespe": "cebraspe.org.br",
    "fcc": "concursosfcc.com.br",
    "cesgranrio": "cesgranrio.org.br",
    "vunesp": "vunesp.com.br",
    "quadrix": "quadrix.org.br",
    "ibfc": "ibfc.org.br",
    "consulplan": "institutoconsulplan.org.br",
    "access": "access.org.br",
    "idecan": "idecan.org.br",
    "fundep": "gestaodeconcursos.com.br",
    "selecon": "selecon.org.br",
    "avanca": "avancasp.org.br",
    "instituto mais": "institutomais.org.br",
    "nosso rumo": "nossorumo.org.br",
}
BANCA_DOMAIN_SET = set(BANCA_DOMAINS.values())

# Diarios oficiais utiles para RS.
OFFICIAL_DIARIO_HINTS = ["diariooficial.rs.gov.br", "in.gov.br"]

EDITAL_NUM_RE = re.compile(
    r"edital\s*(?:de\s+abertura\s*)?(?:n[º°o\.]*\s*)?(\d{1,4}\s*/\s*\d{4})",
    re.IGNORECASE,
)
ORGAO_RE = re.compile(
    r"((?:prefeitura(?:\s+municipal)?|c[aâ]mara(?:\s+municipal)?|"
    r"instituto|universidade|conselho|funda[cç][aã]o|secretaria|"
    r"tribunal|minist[eé]rio|autarquia)\s+(?:de\s+|do\s+|da\s+|dos\s+|das\s+)?"
    r"[A-Za-zÀ-ú][A-Za-zÀ-ú'\s]{2,40})",
    re.IGNORECASE,
)


@dataclass
class RadarSource:
    source_id: str
    source_name: str
    url: str
    local_method: str


@dataclass
class Candidate:
    candidate_url: str
    source_id: str
    source_type: str
    title: str = ""
    anchor_text: str = ""
    snippet: str = ""
    score: int = 0
    score_reasons: str = ""
    official_url_guess: str = ""
    verification_status: str = "unverified"
    orgao_guess: str = ""
    edital_num: str = ""
    banca_guess: str = ""
    city_guess: str = ""
    uf: str = UF
    discovered_at: str = ""


def load_radar_sources(only: Set[str]) -> List[RadarSource]:
    out: List[RadarSource] = []
    with CATALOG.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("source_type") or "").strip() != "portal_radar":
                continue
            if (row.get("enabled") or "").strip().lower() != "true":
                continue
            sid = (row.get("source_id") or "").strip()
            if only and sid not in only:
                continue
            out.append(RadarSource(
                source_id=sid,
                source_name=(row.get("source_name") or "").strip(),
                url=(row.get("url") or "").strip(),
                local_method=(row.get("local_method") or "requests").strip(),
            ))
    return out


def fetch(url: str, method: str, args: argparse.Namespace) -> "f1.FetchResult":
    fs = f1.Source("x", "x", url, "radar")
    if method == "curl_cffi" and f1.creq is not None:
        res = f1.fetch_with_curl_cffi(fs, args.timeout, args.verify_ssl, args.retries)
        if res.result in {"easy", "js"} and res.body:
            return res
    res = f1.fetch_with_requests(fs, args.timeout, args.verify_ssl, args.retries)
    if res.result in {"easy", "js"} and res.body:
        return res
    if f1.creq is not None:
        res2 = f1.fetch_with_curl_cffi(fs, args.timeout, args.verify_ssl, args.retries)
        if res2.body:
            return res2
    return res


def extract_links(html: str, base_url: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    pattern = re.compile(
        r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(html or ""):
        href = (m.group(1) or "").strip()
        anchor = re.sub(r"<[^>]+>", " ", m.group(2) or "")
        anchor = re.sub(r"\s+", " ", anchor).strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absu = urljoin(base_url, href)
        if urlparse(absu).scheme not in ("http", "https"):
            continue
        pairs.append((absu, anchor[:200]))
    return pairs


def looks_rs(blob: str) -> Tuple[bool, str]:
    low = blob.lower()
    for sig in RS_SLUG_SIGNALS:
        if sig in low:
            return True, f"rs_slug:{sig.strip('/')}"
    for city in RS_CITIES:
        if city in low:
            return True, f"rs_city:{city}"
    for banca in RS_BANCAS:
        if banca in low:
            return True, f"rs_banca:{banca}"
    return False, ""


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def is_official(url: str) -> bool:
    host = host_of(url)
    if host.endswith(".gov.br") or host == "gov.br":
        return True
    if any(host == d or host.endswith("." + d) for d in BANCA_DOMAIN_SET):
        return True
    if any(h in host for h in OFFICIAL_DIARIO_HINTS):
        return True
    return False


def find_official_links(links: List[Tuple[str, str]]) -> Tuple[List[str], List[str]]:
    official, pdfs = [], []
    for url, _ in links:
        if not is_official(url):
            continue
        if url.lower().endswith(".pdf"):
            pdfs.append(url)
        else:
            official.append(url)
    # dedupe preservando orden
    return list(dict.fromkeys(official)), list(dict.fromkeys(pdfs))


def detect_banca(blob: str) -> str:
    low = blob.lower()
    for kw in BANCA_DOMAINS:
        if kw in low:
            return kw
    return ""


def parse_article(html: str, base_url: str) -> Dict[str, object]:
    text = f1.visible_text(html)
    title = f1.page_title(html)
    edital_num = ""
    m = EDITAL_NUM_RE.search(text)
    if m:
        edital_num = re.sub(r"\s+", "", m.group(1))
    orgao = ""
    mo = ORGAO_RE.search(text)
    if mo:
        orgao = re.sub(r"\s+", " ", mo.group(1)).strip()[:70]
    snippet = ""
    idx = text.lower().find("edital")
    if idx < 0:
        idx = text.lower().find("concurso")
    if idx >= 0:
        start = max(0, idx - 60)
        snippet = text[start:start + 260].strip()
    links = extract_links(html, base_url)
    official, pdfs = find_official_links(links)
    banca = detect_banca(title + " " + text[:1500])
    return {
        "title": title,
        "snippet": snippet,
        "edital_num": edital_num,
        "orgao": orgao,
        "banca": banca,
        "official": official,
        "pdfs": pdfs,
    }


def city_in(blob: str) -> str:
    low = blob.lower()
    for city in RS_CITIES:
        if city in low:
            return city
    return ""


def score_candidate(c: Candidate, parsed: Dict[str, object], rs_reason: str) -> None:
    reasons: List[str] = [rs_reason] if rs_reason else []
    score = 0
    if parsed["pdfs"]:
        score += 4
        reasons.append("official_pdf")
        c.verification_status = "pdf_found"
        c.official_url_guess = parsed["pdfs"][0]
    elif parsed["official"]:
        score += 3
        reasons.append("official_link")
        c.verification_status = "official_found"
        c.official_url_guess = parsed["official"][0]
    else:
        score -= 2
        reasons.append("portal_only")
        c.verification_status = "unverified"

    if parsed["banca"]:
        score += 2
        reasons.append(f"banca:{parsed['banca']}")
    if parsed["edital_num"]:
        score += 2
        reasons.append(f"edital:{parsed['edital_num']}")
    if parsed["orgao"]:
        score += 1
        reasons.append("orgao")

    blob = (c.candidate_url + " " + c.anchor_text).lower()
    if "edital" in blob:
        score += 1
    if "inscri" in blob:
        score += 1
    if "apostila" in blob or "simulado" in blob or "curso" in blob:
        score -= 2
        reasons.append("commercial")

    c.score = score
    c.score_reasons = ";".join(reasons)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fase 2B — discovery radar + match oficial (RS).")
    p.add_argument("--only", default="", help="Lista coma-separada de portales (source_id).")
    p.add_argument("--max-per-portal", type=int, default=30, help="Max articulos a seguir por portal.")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--delay-min", type=float, default=1.2)
    p.add_argument("--delay-max", type=float, default=2.4)
    p.add_argument("--verify-ssl", action="store_true")
    p.add_argument("--no-follow", action="store_true", help="No seguir al articulo; solo listar links RS.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    radars = load_radar_sources(only)
    if not radars:
        print("No hay portales radar seleccionados.")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"Fase 2B — radar -> oficial, scope {UF}. Portales: {len(radars)}")
    print(f"  requests:{'OK' if f1.rq else 'FALTA'}  curl_cffi:{'OK' if f1.creq else 'FALTA'}\n")

    candidates: List[Candidate] = []

    for src in radars:
        # Entrar por el indice RS si existe, mas la homepage como respaldo.
        entry_urls = []
        if src.source_id in RS_ENTRY:
            entry_urls.append(RS_ENTRY[src.source_id])
        entry_urls.append(src.url)

        links: List[Tuple[str, str]] = []
        opened = False
        for entry in entry_urls:
            res = fetch(entry, src.local_method, args)
            if res.body and res.result in {"easy", "js"}:
                opened = True
                links.extend(extract_links(res.body, res.final_url or entry))
            time.sleep(random.uniform(args.delay_min, args.delay_max))
        if not opened:
            print(f"  [!!] {src.source_id:16s} no abrio")
            continue

        # Filtrar a links que huelen a RS y a articulo (no nav/footer).
        rs_links: List[Tuple[str, str, str]] = []
        seen_urls: Set[str] = set()
        for url, anchor in links:
            blob = url + " " + anchor
            ok, reason = looks_rs(blob)
            if not ok:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            rs_links.append((url, anchor, reason))

        rs_links = rs_links[:args.max_per_portal]
        print(f"  [OK] {src.source_id:16s} links_RS={len(rs_links)} (siguiendo profundidad 1...)")

        for i, (url, anchor, reason) in enumerate(rs_links):
            c = Candidate(
                candidate_url=url,
                source_id=src.source_id,
                source_type="portal_radar",
                anchor_text=anchor,
                discovered_at=ts,
            )
            if args.no_follow:
                c.score_reasons = reason + ";no_follow"
                c.city_guess = city_in(url + " " + anchor)
                candidates.append(c)
                continue
            art = fetch(url, src.local_method, args)
            if art.body:
                parsed = parse_article(art.body, art.final_url or url)
                c.title = parsed["title"]
                c.snippet = parsed["snippet"]
                c.orgao_guess = parsed["orgao"]
                c.edital_num = parsed["edital_num"]
                c.banca_guess = parsed["banca"]
                c.city_guess = city_in(c.title + " " + c.snippet + " " + url + " " + anchor)
                score_candidate(c, parsed, reason)
            else:
                c.score = -2
                c.score_reasons = reason + ";article_failed"
                c.city_guess = city_in(url + " " + anchor)
            candidates.append(c)
            if i < len(rs_links) - 1:
                time.sleep(random.uniform(args.delay_min, args.delay_max))
        time.sleep(random.uniform(args.delay_min, args.delay_max))

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["candidate_url", "source_id", "source_type", "title", "anchor_text",
              "snippet", "score", "score_reasons", "official_url_guess",
              "verification_status", "orgao_guess", "edital_num", "banca_guess",
              "city_guess", "uf", "discovered_at"]
    rows = [{k: getattr(c, k) for k in fields} for c in candidates]
    out_path = write_table(rows, fields, out_path, sheet_name="Candidatas RS")

    total = len(candidates)
    pdf_found = sum(1 for c in candidates if c.verification_status == "pdf_found")
    off_found = sum(1 for c in candidates if c.verification_status == "official_found")
    unver = sum(1 for c in candidates if c.verification_status == "unverified")
    with_banca = sum(1 for c in candidates if c.banca_guess)
    with_edital = sum(1 for c in candidates if c.edital_num)

    print("\n=============== FASE 2B — RADAR -> OFICIAL (RS) ===============")
    print(f"  Candidatos RS totales : {total}")
    print(f"  pdf_found             : {pdf_found}")
    print(f"  official_found        : {off_found}")
    print(f"  unverified (solo radar): {unver}")
    print(f"  con banca identificada: {with_banca}")
    print(f"  con n edital          : {with_edital}")
    crit = (pdf_found + off_found) >= 10
    print(f"  Mission accomplished  : {'SI' if crit else 'PARCIAL'} "
          f"(>=10 candidatos con evidencia oficial)")
    print(f"\n  Excel: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
