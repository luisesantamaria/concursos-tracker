#!/usr/bin/env python3
"""Medidor de precision/cobertura del pipeline contra el golden set.

Compara dos CSV y reporta que tan bien el pipeline acierta las URLs:
  --golden    la hoja de respuestas verificada a mano (golden_set_v1.csv)
  --pipeline  la salida del pipeline (municipios_resources_*.csv)

NO importa el pipeline ni usa IA: es una vara de medir independiente.
Solo stdlib (csv, argparse, re, unicodedata, urllib).

Por cada bucket (concursos / processos) clasifica cada municipio:
  HIT    la URL del pipeline coincide (normalizada) con la del golden
  HOST   mismo portal/host que el golden, pero pagina distinta (casi)
  WRNG   el pipeline emitio una URL, pero es incorrecta
  MISS   el golden tiene URL y el pipeline la dejo vacia
  T-NEG  el golden dice no_existe y el pipeline (correctamente) la dejo vacia
  F-POS  el golden dice no_existe pero el pipeline invento una URL

Metricas:
  precision = aciertos / (todo lo que el pipeline emitio)         -> que tan confiable
  cobertura = aciertos / (todos los buckets que existen)          -> cuanto encontro
Cada una en version estricta (solo HIT) y tolerante (HIT+HOST).

Separa las filas requiere_revision_humana=si para no contaminar la metrica de
lo automatizable, y reporta aparte como se porto el pipeline en esos casos.
Desglosa todo por tipo de portal para ver QUE construir primero.

Uso:
  python medir_golden_set.py --golden golden_set_v1.csv --pipeline salida.csv
  python medir_golden_set.py --golden golden_set_v1.csv --pipeline salida.csv --detalle
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Normalizacion
# --------------------------------------------------------------------------- #
def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def muni_key(name: str) -> str:
    """Clave de join robusta a acentos/espacios/mayusculas."""
    return re.sub(r"[^a-z0-9]+", "", strip_accents(name).lower())


def norm_url(url: str) -> str:
    """Forma canonica para comparar URLs.

    - ignora esquema (http==https) y www
    - quita slash final y fragmento (#...)
    - decodifica y ORDENA los query params (su orden no importa, pero su
      contenido SI: en Arambare 'categoria=2' vs 'categoria=3' distingue PSS
      de concurso, asi que no se eliminan)
    """
    url = (url or "").strip()
    if not url or url.lower() == "no_existe":
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = "&".join(f"{k.lower()}={v}" for k, v in sorted(pairs))
    base = f"{host}{path}"
    return f"{base}?{query}" if query else base


def host_of(normalized: str) -> str:
    if not normalized:
        return ""
    return normalized.split("?", 1)[0].split("/", 1)[0]


# --------------------------------------------------------------------------- #
# Juicio por bucket
# --------------------------------------------------------------------------- #
VERDICTS = ["HIT", "HOST", "WRNG", "MISS", "T-NEG", "F-POS", "SKIP"]


def judge_bucket(golden_main: str, golden_extra: str, pipeline_url: str) -> str:
    golden_norms: set[str] = set()
    no_existe = False
    for raw in (golden_main, golden_extra):
        value = (raw or "").strip()
        if not value:
            continue
        if value.lower() == "no_existe":
            no_existe = True
            continue
        normalized = norm_url(value)
        if normalized:
            golden_norms.add(normalized)

    exists = bool(golden_norms)
    pipeline_norm = norm_url(pipeline_url)

    if exists:
        if not pipeline_norm:
            return "MISS"
        if pipeline_norm in golden_norms:
            return "HIT"
        if host_of(pipeline_norm) in {host_of(g) for g in golden_norms}:
            return "HOST"
        return "WRNG"
    if no_existe:
        return "T-NEG" if not pipeline_norm else "F-POS"
    return "SKIP"  # golden sin dato: no se puede juzgar


# --------------------------------------------------------------------------- #
# Carga de CSV
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"No existe: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def get(row: dict, *names: str) -> str:
    """Lee la primera columna que exista (tolera nombres distintos)."""
    for name in names:
        if name in row and row[name] is not None:
            return str(row[name]).strip()
        # tolerar header vacio (primera columna del CSV de Luis)
    return ""


# --------------------------------------------------------------------------- #
# Metricas
# --------------------------------------------------------------------------- #
def pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "  n/a"
    return f"{100.0 * numerator / denominator:5.1f}%"


def precision_recall(counts: dict[str, int]) -> dict[str, float | int]:
    hit = counts.get("HIT", 0)
    host = counts.get("HOST", 0)
    wrng = counts.get("WRNG", 0)
    miss = counts.get("MISS", 0)
    fpos = counts.get("F-POS", 0)

    emitted = hit + host + wrng + fpos          # todo lo que el pipeline escribio
    exists = hit + host + wrng + miss           # todos los buckets que existen
    return {
        "hit": hit,
        "host": host,
        "wrng": wrng,
        "miss": miss,
        "fpos": fpos,
        "tneg": counts.get("T-NEG", 0),
        "emitted": emitted,
        "exists": exists,
        "prec_strict": (hit / emitted) if emitted else -1,
        "prec_lenient": ((hit + host) / emitted) if emitted else -1,
        "rec_strict": (hit / exists) if exists else -1,
        "rec_lenient": ((hit + host) / exists) if exists else -1,
    }


def show_block(title: str, counts: dict[str, int]) -> None:
    m = precision_recall(counts)
    print(f"  {title}")
    print(
        f"    HIT={m['hit']:>2}  HOST={m['host']:>2}  WRNG={m['wrng']:>2}  "
        f"MISS={m['miss']:>2}  T-NEG={m['tneg']:>2}  F-POS={m['fpos']:>2}"
    )
    print(
        f"    precision  estricta={pct(m['hit'], m['emitted'])}   "
        f"tolerante(host)={pct(m['hit'] + m['host'], m['emitted'])}   "
        f"(sobre {m['emitted']} emitidas)"
    )
    print(
        f"    cobertura  estricta={pct(m['hit'], m['exists'])}   "
        f"tolerante(host)={pct(m['hit'] + m['host'], m['exists'])}   "
        f"(sobre {m['exists']} existentes)"
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Mide el pipeline contra el golden set.")
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--detalle", action="store_true", help="Tabla por municipio.")
    args = parser.parse_args()

    golden_rows = read_csv(args.golden)
    pipeline_rows = read_csv(args.pipeline)

    pipeline_by_key: dict[str, dict] = {}
    for row in pipeline_rows:
        name = get(row, "municipio", "Municipio", "MUNICIPIO")
        if name:
            pipeline_by_key[muni_key(name)] = row

    # acumuladores
    overall_c = defaultdict(int)
    overall_p = defaultdict(int)
    auto_c = defaultdict(int)      # solo automatizables (revision=no), concursos
    auto_p = defaultdict(int)
    by_tipo_c: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_tipo_p: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    human_rows: list[tuple] = []
    site_hit = site_total = 0

    detail: list[tuple] = []
    unmatched: list[str] = []

    for g in golden_rows:
        muni = get(g, "municipio") or next(iter(g.values()), "")
        if not muni:
            continue
        tipo = get(g, "tipo") or "?"
        revision = get(g, "requiere_revision_humana").lower() in {"si", "sí", "yes", "true", "1"}

        gc_main = get(g, "url_concursos")
        gc_extra = get(g, "urls_concursos_extra")
        gp_main = get(g, "url_processos_seletivos")
        gp_extra = get(g, "urls_processos_extra")
        g_site = get(g, "site_base")

        p = pipeline_by_key.get(muni_key(muni))
        if p is None:
            unmatched.append(muni)
            continue

        pc = get(p, "url_concursos")
        pp = get(p, "url_processos_seletivos")
        p_site = get(p, "site_base")

        vc = judge_bucket(gc_main, gc_extra, pc)
        vp = judge_bucket(gp_main, gp_extra, pp)

        # site_base: acierto si mismo host
        if norm_url(g_site):
            site_total += 1
            if host_of(norm_url(p_site)) == host_of(norm_url(g_site)) and host_of(norm_url(g_site)):
                site_hit += 1

        overall_c[vc] += 1
        overall_p[vp] += 1
        by_tipo_c[tipo][vc] += 1
        by_tipo_p[tipo][vp] += 1
        if revision:
            human_rows.append((muni, tipo, vc, vp))
        else:
            auto_c[vc] += 1
            auto_p[vp] += 1

        detail.append((muni, tipo, "H" if revision else " ", p_site, vc, pc, vp, pp))

    # ---------------- salida ----------------
    print("=" * 78)
    print("MEDICION DEL PIPELINE CONTRA EL GOLDEN SET")
    print("=" * 78)
    print(f"golden: {len(golden_rows)} municipios   pipeline: {len(pipeline_rows)} filas")
    matched = len(golden_rows) - len(unmatched)
    print(f"cruzados: {matched}   sin match en pipeline: {len(unmatched)}")
    if unmatched:
        print("  (faltan en la salida del pipeline: " + ", ".join(unmatched) + ")")
        print("  -> corre el pipeline sobre TODOS los municipios del golden, no solo letra A.")
    print(f"site_base correcto (mismo host): {site_hit}/{site_total}  {pct(site_hit, site_total)}")

    print("\n" + "-" * 78)
    print("AUTOMATIZABLES (excluye revisar_humano)  <-- esta es la metrica a optimizar")
    print("-" * 78)
    show_block("CONCURSOS", auto_c)
    print()
    show_block("PROCESSOS SELETIVOS", auto_p)
    combined_auto = defaultdict(int)
    for d in (auto_c, auto_p):
        for k, v in d.items():
            combined_auto[k] += v
    print()
    show_block("AMBOS BUCKETS (combinado)", combined_auto)

    print("\n" + "-" * 78)
    print("GENERAL (incluye todo, tambien revisar_humano)")
    print("-" * 78)
    show_block("CONCURSOS", overall_c)
    print()
    show_block("PROCESSOS SELETIVOS", overall_p)

    print("\n" + "-" * 78)
    print("DESGLOSE POR TIPO DE PORTAL  (acierto = HIT; ~ = HOST; x = WRNG/F-POS; - = MISS)")
    print("-" * 78)
    tipos = sorted(set(list(by_tipo_c) + list(by_tipo_p)))
    print(f"  {'tipo':<28} {'concursos':<22} {'processos':<22}")
    for tipo in tipos:
        cc = by_tipo_c.get(tipo, {})
        pp = by_tipo_p.get(tipo, {})

        def fmt(counts: dict[str, int]) -> str:
            parts = []
            for label, key in [("HIT", "HIT"), ("~", "HOST"), ("x", "WRNG"), ("x+", "F-POS"), ("-", "MISS"), ("ok0", "T-NEG")]:
                n = counts.get(key, 0)
                if n:
                    parts.append(f"{label}:{n}")
            return " ".join(parts) if parts else "-"

        print(f"  {tipo:<28} {fmt(cc):<22} {fmt(pp):<22}")

    print("\n" + "-" * 78)
    print("CASOS DE REVISION HUMANA  (aqui un MISS o flag es aceptable; WRNG/F-POS es el fallo real)")
    print("-" * 78)
    if human_rows:
        print(f"  {'municipio':<22} {'tipo':<26} {'concursos':<8} {'processos':<8}")
        for muni, tipo, vc, vp in human_rows:
            print(f"  {muni:<22} {tipo:<26} {vc:<8} {vp:<8}")
    else:
        print("  (ninguno marcado en el golden)")

    if args.detalle:
        print("\n" + "-" * 78)
        print("DETALLE POR MUNICIPIO  (H = revision humana)")
        print("-" * 78)
        for muni, tipo, h, p_site, vc, pc, vp, pp in detail:
            print(f"\n  [{h}] {muni}  ({tipo})")
            print(f"      site:      {p_site or '-'}")
            print(f"      concursos: {vc:<6} {pc or '(vacio)'}")
            print(f"      processos: {vp:<6} {pp or '(vacio)'}")

    print("\n" + "=" * 78)
    print("LECTURA: precision baja = el pipeline INVENTA (peligroso para el dataset).")
    print("         cobertura baja = el pipeline NO ENCUENTRA (huecos a llenar).")
    print("         mira el desglose por tipo para decidir QUE construir primero.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())