#!/usr/bin/env python3
"""
Fase 2 v1 — Discovery de URLs candidatas.

Entra a cada fuente accesible del catalogo, cosecha todos los <a href> de la
pagina inicial, resuelve a URL absoluta, los puntua por "olor a edital" y los
guarda en data/candidate_urls.xlsx.

NO descarga PDFs (eso es Fase 3) ni sigue links de profundidad 2 (Fase 7).
Solo cosecha la primera capa de links y los filtra por score.

Reutiliza los motores de fetch ya validados en fase1_v1.py
(requests -> curl_cffi -> playwright).

Las fuentes radar (portal_radar / source_type radar) se marcan con is_radar=1
para poder compararlas despues contra las fuentes oficiales (Fase 9-10).

Uso tipico (todas las fuentes accesibles por requests/curl):
  python fase2_v1.py

Solo el piloto Sur:
  python fase2_v1.py --pilot

Incluir tambien las que necesitan navegador (Cloudflare/JS) con browser visible:
  python fase2_v1.py --include-challenge --headful
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

# Reutilizar motores de fetch de Fase 1
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fase1_v1 as f1  # noqa: E402
from excel_utils import write_table  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG = PROJECT_ROOT / "data" / "catalog" / "sources_catalog_phase1.csv"
DEFAULT_OUT = PROJECT_ROOT / "data" / "candidate_urls.xlsx"

# Piloto Sur: por donde arrancar segun el roadmap.
PILOT_IDS = {
    "dou", "doe_sp", "doe_pr", "doe_sc", "doe_rs",
    "cebraspe", "fcc", "fundatec", "cesgranrio",
}

RADAR_TYPES = {"portal_radar", "radar"}

# Scoring: positivos (huele a edital) vs negativos (ruido comercial).
POSITIVE = {
    "edital": 3,
    ".pdf": 3,
    "concurso": 2,
    "inscri": 2,            # inscricao / inscricoes
    "processo seletivo": 2,
    "processo-seletivo": 2,
    "processoseletivo": 2,
    "certame": 2,
    "retifica": 2,          # retificacao
    "selecao": 1,
    "abertura": 1,
    "provimento": 1,
    "homologa": 1,
    "concurso publico": 2,
    "concurso-publico": 2,
}

NEGATIVE = {
    "apostila": -3,
    "simulado": -3,
    "assinatura": -3,
    "comprar": -3,
    "/loja": -3,
    "checkout": -3,
    "material": -2,
    "videoaula": -2,
    "curso": -2,
    "facebook": -2,
    "instagram": -2,
    "twitter": -2,
    "youtube": -2,
    "whatsapp": -2,
    "linkedin": -2,
    "telegram": -2,
    "privacidade": -2,
    "login": -1,
    "cadastro": -1,
}

GOOD = {"easy", "js"}


@dataclass
class CatSource:
    source_id: str
    source_name: str
    source_type: str
    url: str
    local_method: str
    enabled: bool

    @property
    def is_radar(self) -> bool:
        return self.source_type in RADAR_TYPES


def load_catalog() -> List[CatSource]:
    out: List[CatSource] = []
    with CATALOG.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            enabled = (row.get("enabled") or "").strip().lower() == "true"
            out.append(
                CatSource(
                    source_id=(row.get("source_id") or "").strip(),
                    source_name=(row.get("source_name") or "").strip(),
                    source_type=(row.get("source_type") or "").strip(),
                    url=(row.get("url") or "").strip(),
                    local_method=(row.get("local_method") or "requests").strip(),
                    enabled=enabled,
                )
            )
    return out


def select_sources(catalog: List[CatSource], args: argparse.Namespace) -> List[CatSource]:
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        return [s for s in catalog if s.source_id in wanted and s.url]
    if args.pilot:
        return [s for s in catalog if s.source_id in PILOT_IDS and s.url]

    sel: List[CatSource] = []
    for s in catalog:
        if not s.enabled or not s.url:
            continue
        # Por defecto solo fuentes alcanzables sin navegador (requests/curl_cffi).
        # Las challenge/playwright requieren --include-challenge.
        needs_browser = s.local_method == "playwright"
        if needs_browser and not args.include_challenge:
            continue
        sel.append(s)
    return sel


def fetch_source(src: CatSource, browser: "f1.PlaywrightFetcher", args: argparse.Namespace) -> "f1.FetchResult":
    fs = f1.Source(src.source_id, src.source_name, src.url, src.source_type)
    order = []
    if src.local_method == "curl_cffi":
        order = ["curl", "requests"]
    elif src.local_method == "playwright":
        order = ["playwright"]
    else:
        order = ["requests", "curl"]

    last: Optional["f1.FetchResult"] = None
    for engine in order:
        if engine == "requests":
            res = f1.fetch_with_requests(fs, args.timeout, args.verify_ssl, args.retries)
        elif engine == "curl":
            if f1.creq is None:
                continue
            res = f1.fetch_with_curl_cffi(fs, args.timeout, args.verify_ssl, args.retries)
        elif engine == "playwright":
            if not browser.available():
                continue
            res = browser.fetch(fs)
        else:
            continue
        last = res
        if res.result in GOOD and res.body:
            return res
    # Si ninguno cargo bien, intentar playwright como ultimo recurso
    if args.include_challenge and browser.available() and (last is None or last.result not in GOOD):
        res = browser.fetch(fs)
        if res.body:
            return res
    return last or f1.FetchResult("none", "error", "no_engine")


def extract_links(html: str, base_url: str) -> List[Tuple[str, str]]:
    """Devuelve lista de (url_absoluta, anchor_text)."""
    import re

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
        scheme = urlparse(absu).scheme
        if scheme not in ("http", "https"):
            continue
        pairs.append((absu, anchor[:200]))
    return pairs


def score_link(url: str, anchor: str) -> int:
    blob = (url + " " + anchor).lower()
    score = 0
    for kw, pts in POSITIVE.items():
        if kw in blob:
            score += pts
    for kw, pts in NEGATIVE.items():
        if kw in blob:
            score += pts
    return score


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fase 2 v1 — discovery de URLs candidatas.")
    p.add_argument("--pilot", action="store_true", help="Solo el piloto Sur.")
    p.add_argument("--only", default="", help="Lista coma-separada de source_id a procesar.")
    p.add_argument("--include-challenge", action="store_true", help="Incluir fuentes que necesitan navegador (Cloudflare/JS).")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="Excel de salida.")
    p.add_argument("--min-score", type=int, default=None, help="Si se da, filtra la salida a score >= este valor.")
    p.add_argument("--timeout", type=int, default=35)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--delay-min", type=float, default=2.0)
    p.add_argument("--delay-max", type=float, default=3.5)
    p.add_argument("--verify-ssl", action="store_true")
    p.add_argument("--headful", action="store_true")
    p.add_argument("--browser-channel", default="chrome")
    p.add_argument("--settle-ms", type=int, default=2500)
    p.add_argument("--manual-wait", type=int, default=0)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not CATALOG.exists():
        print(f"No encuentro el catalogo: {CATALOG}")
        return 1

    catalog = load_catalog()
    sources = select_sources(catalog, args)
    if not sources:
        print("No hay fuentes seleccionadas.")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = PROJECT_ROOT / "logs" / "fase2_v1"
    log_dir.mkdir(parents=True, exist_ok=True)

    browser = f1.PlaywrightFetcher(
        timeout=args.timeout,
        verify_ssl=args.verify_ssl,
        headful=args.headful,
        browser_channel=args.browser_channel,
        manual_wait=args.manual_wait,
        settle_ms=args.settle_ms,
        screenshots=False,
        outdir=log_dir,
    )

    print(f"Fase 2 — discovery sobre {len(sources)} fuentes.")
    print(f"  requests : {'OK' if f1.rq is not None else 'FALTA'}")
    print(f"  curl_cffi: {'OK' if f1.creq is not None else 'FALTA'}")
    print(f"  playwright:{'OK' if f1.sync_playwright is not None else 'FALTA'}\n")

    rows: List[Dict[str, object]] = []
    fetch_log: List[Dict[str, object]] = []
    seen = set()  # (source_id, url)

    try:
        for idx, src in enumerate(sources, start=1):
            res = fetch_source(src, browser, args)
            links = extract_links(res.body, res.final_url or src.url) if res.body else []

            kept = 0
            for url, anchor in links:
                key = (src.source_id, url)
                if key in seen:
                    continue
                seen.add(key)
                score = score_link(url, anchor)
                rows.append({
                    "source_id": src.source_id,
                    "source_name": src.source_name,
                    "source_type": src.source_type,
                    "is_radar": 1 if src.is_radar else 0,
                    "url": url,
                    "anchor_text": anchor,
                    "score": score,
                    "base_url": res.final_url or src.url,
                    "found_at": ts,
                })
                kept += 1

            pos = sum(1 for r in rows if r["source_id"] == src.source_id and r["score"] > 0)
            mark = "[OK]" if res.result in GOOD else "[!!]"
            print(
                f"  {mark} {idx:02d}/{len(sources):02d} {src.source_id:16s} "
                f"{res.result:8s} via {res.engine[:16]:16s} "
                f"links={kept:4d} score>0={pos:3d}"
            )
            fetch_log.append({
                "source_id": src.source_id,
                "result": res.result,
                "engine": res.engine,
                "status": res.status if res.status is not None else "",
                "links": kept,
                "score_pos": pos,
                "note": res.note,
            })

            if idx < len(sources):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
    finally:
        browser.close()

    if args.min_score is not None:
        rows = [r for r in rows if int(r["score"]) >= args.min_score]

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["source_id", "source_name", "source_type", "is_radar",
              "url", "anchor_text", "score", "base_url", "found_at"]
    out_path = write_table(rows, fields, out_path, sheet_name="Candidatas")

    log_path = log_dir / f"fetch_log_{ts}.xlsx"
    log_path = write_table(
        fetch_log,
        ["source_id", "result", "engine", "status", "links", "score_pos", "note"],
        log_path,
        sheet_name="Fetch log",
    )

    total = len(rows)
    pos_rows = [r for r in rows if int(r["score"]) > 0]
    pos = len(pos_rows)
    distinct = len({r["source_id"] for r in rows})
    distinct_pos = len({r["source_id"] for r in pos_rows})
    pos_official = sum(1 for r in pos_rows if int(r["is_radar"]) == 0)
    pos_radar = pos - pos_official
    opened = sum(1 for x in fetch_log if x["result"] in GOOD)

    print("\n=================== FASE 2 — DISCOVERY ===================")
    print(f"  Fuentes abiertas       : {opened}/{len(sources)}")
    print(f"  Links extraidos (bruto): {total}")
    print(f"  Candidatas (score > 0) : {pos}  (oficial={pos_official}, radar={pos_radar})")
    print(f"  Fuentes con candidatas : {distinct_pos}")
    # El entregable de Fase 2 es el set score>0; el bruto incluye navegacion.
    crit = pos >= 200 and distinct_pos >= 5
    print(f"  Mission accomplished   : {'SI' if crit else 'NO'} "
          f"(>=200 candidatas score>0 en >=5 fuentes)")
    print(f"\n  Excel     : {out_path}")
    print(f"  Fetch log : {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
