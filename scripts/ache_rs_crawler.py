#!/usr/bin/env python3
"""
Crawl enfocado: Ache Concursos -> Rio Grande do Sul.

Lista TODOS los concursos RS (abertos + em andamento) de la pagina de Ache,
entra a cada detalle (profundidad 1) y extrae los links OFICIALES que aparecen
(edital/PDF, pagina de banca/orgao, retificacao, classificados/resultado,
inscricao). Reporta cuantos concursos tienen pagina oficial localizada.

Salida: data/ache_rs_concursos.xlsx

Uso:
  python ache_rs_crawler.py
  python ache_rs_crawler.py --limit 30        # solo los primeros 30 (prueba)
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import random
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import write_table  # noqa: E402
import fase1_v1 as f1  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT = PROJECT_ROOT / "data" / "ache_rs_concursos.xlsx"
RS_LIST = "https://www.acheconcursos.com.br/concursos-rio-grande-do-sul"
ACHE_HOST = "acheconcursos.com.br"
DETAIL_PREFIX = "/concursos-rio-grande-do-sul/"

BANCA_DOMAINS = {
    "fundatec.org.br", "legalleconcursos.com.br", "fundacaolasalle.org.br", "objetivas.com.br",
    "portalfaurgs.com.br", "fgv.br", "cebraspe.org.br", "concursosfcc.com.br",
    "cesgranrio.org.br", "vunesp.com.br", "quadrix.org.br", "ibfc.org.br",
    "institutoconsulplan.org.br", "access.org.br", "idecan.org.br",
    "gestaodeconcursos.com.br", "selecon.org.br", "avancasp.org.br",
    "institutomais.org.br", "nossorumo.org.br", "institutoaocp.org.br",
    "fadergs.org.br", "ibam.org.br", "consesp.com.br", "indebras.org.br",
}


def is_official(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    if host.endswith(".gov.br") or host == "gov.br":
        return True
    if any(host == d or host.endswith("." + d) for d in BANCA_DOMAINS):
        return True
    return False


def get_links(html: str, base: str) -> List[Tuple[str, str]]:
    out = []
    for m in re.finditer(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html or "", re.I | re.S):
        href = (m.group(1) or "").strip()
        anchor = re.sub(r"<[^>]+>", " ", m.group(2) or "")
        anchor = re.sub(r"\s+", " ", anchor).strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absu = urljoin(base, href)
        if urlparse(absu).scheme in ("http", "https"):
            out.append((absu, anchor[:160]))
    return out


def categorize(url: str, anchor: str) -> str:
    blob = (url + " " + anchor).lower()
    if "retific" in blob:
        return "retificacao"
    if any(k in blob for k in ("classificad", "resultado", "gabarito", "homologa", "convoca")):
        return "resultado_classificados"
    if url.lower().endswith(".pdf"):
        return "edital_pdf"
    if "edital" in blob:
        return "edital_pagina"
    if "inscri" in blob:
        return "inscricao"
    return "outro_oficial"


def fetch(url: str, args) -> "f1.FetchResult":
    fs = f1.Source("ache", "ache", url, "radar")
    res = f1.fetch_with_requests(fs, args.timeout, False, 1)
    if (res.result not in {"easy", "js"} or not res.body) and f1.creq is not None:
        res2 = f1.fetch_with_curl_cffi(fs, args.timeout, False, 1)
        if res2.body:
            return res2
    return res


def collect_detail_urls(args) -> List[Tuple[str, str]]:
    """Devuelve [(detail_url, titulo_anchor)] unicos para RS."""
    seen: Set[str] = set()
    details: List[Tuple[str, str]] = []
    page = 1
    base = RS_LIST
    while True:
        url = base if page == 1 else f"{base}?page={page}"
        res = fetch(url, args)
        if not res.body or res.result not in {"easy", "js"}:
            break
        added = 0
        for u, a in get_links(res.body, url):
            pu = urlparse(u)
            if ACHE_HOST not in pu.netloc:
                continue
            path = pu.path
            if not path.startswith(DETAIL_PREFIX):
                continue
            slug = path[len(DETAIL_PREFIX):].strip("/")
            if not slug or "/" in slug:  # solo detalle directo, no sub-rutas
                continue
            if u in seen:
                continue
            seen.add(u)
            details.append((u, a))
            added += 1
        # Heuristica de paginacion: si esta pagina no aporto nada nuevo, parar.
        if added == 0:
            break
        page += 1
        if page > args.max_pages:
            break
        time.sleep(random.uniform(args.delay_min, args.delay_max))
    return details


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crawl Ache Concursos RS -> links oficiales.")
    p.add_argument("--limit", type=int, default=0, help="Procesar solo los primeros N concursos.")
    p.add_argument("--max-pages", type=int, default=10, help="Max paginas de listado a recorrer.")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--delay-min", type=float, default=1.0)
    p.add_argument("--delay-max", type=float, default=2.0)
    p.add_argument("--out", default=str(OUT))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    print("Listando concursos RS en Ache Concursos...")
    details = collect_detail_urls(args)
    print(f"  Concursos RS encontrados en el listado: {len(details)}")
    if args.limit:
        details = details[:args.limit]
        print(f"  (limitado a {len(details)} para esta corrida)")

    rows: List[Dict[str, object]] = []
    located = 0
    for i, (durl, dtitle) in enumerate(details, start=1):
        res = fetch(durl, args)
        cats: Dict[str, List[str]] = {}
        title = f1.page_title(res.body) if res.body else ""
        if res.body:
            for u, a in get_links(res.body, res.final_url or durl):
                if not is_official(u):
                    continue
                cats.setdefault(categorize(u, a), [])
                if u not in cats[categorize(u, a)]:
                    cats[categorize(u, a)].append(u)

        official_urls = [u for lst in cats.values() for u in lst]
        official_urls = list(dict.fromkeys(official_urls))
        has_official = bool(official_urls)
        if has_official:
            located += 1

        def first(cat):
            return cats.get(cat, [""])[0] if cats.get(cat) else ""

        rows.append({
            "n": i,
            "titulo": (title or dtitle)[:120],
            "detalle_ache": durl,
            "tiene_oficial": "SI" if has_official else "NO",
            "n_links_oficiales": len(official_urls),
            "edital_pdf": first("edital_pdf"),
            "edital_pagina": first("edital_pagina"),
            "retificacao": first("retificacao"),
            "resultado_classificados": first("resultado_classificados"),
            "inscricao": first("inscricao"),
            "otros_oficiales": " | ".join(cats.get("outro_oficial", []))[:300],
            "todos_oficiales": " | ".join(official_urls)[:600],
        })
        mark = "[OK]" if has_official else "[--]"
        ncat = ",".join(f"{k}:{len(v)}" for k, v in cats.items())
        print(f"  {mark} {i:03d}/{len(details):03d} {(title or dtitle)[:48]:48s} {ncat}")
        if i < len(details):
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["n", "titulo", "detalle_ache", "tiene_oficial", "n_links_oficiales",
              "edital_pdf", "edital_pagina", "retificacao", "resultado_classificados",
              "inscricao", "otros_oficiales", "todos_oficiales"]
    out_path = write_table(rows, fields, out_path, sheet_name="Ache RS")

    with_pdf = sum(1 for r in rows if r["edital_pdf"])
    with_retif = sum(1 for r in rows if r["retificacao"])
    with_result = sum(1 for r in rows if r["resultado_classificados"])

    print("\n=============== ACHE RS — RESUMEN ===============")
    print(f"  Concursos RS procesados      : {len(rows)}")
    print(f"  Con pagina/PDF oficial        : {located}  ({located*100//max(1,len(rows))}%)")
    print(f"  Con edital en PDF directo     : {with_pdf}")
    print(f"  Con link de retificacao       : {with_retif}")
    print(f"  Con resultado/classificados   : {with_result}")
    print(f"\n  Excel: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
