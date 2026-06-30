#!/usr/bin/env python3
"""Overview del dataset fase 2 — semáforo verde / amarillo / rojo + cerrados parciales.

Modelo (post mano-negra Chrome): cada bucket es CONFIRMADO (confianza=confirmado con
URL), PENDIENTE (tiene URL pero aún en probable/revisar — sin revisar a mano) o VACÍO
(sin URL). Por municipio:
  🟢 VERDE  pleno            : ambos buckets CONFIRMADOS
  🟡 AMARILLO pendiente      : algún bucket PENDIENTE (aún no revisado) -> baja con cada tanda
  🟠 cerrado parcial         : 1 confirmado + 1 vacío (revisado, sin índice oficial propio)
  🔴 ROJO   sin sitio        : ambos buckets VACÍOS

Uso:
  python scripts/eval/rollup_overview.py [--csv data/fase2/municipios_rs_local.csv]
"""
from __future__ import annotations
import argparse
import csv
from pathlib import Path


def has_url(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("http")


def bucket_state(conf: str, url: str) -> str:
    if (conf or "").strip() == "confirmado" and has_url(url):
        return "CONF"
    if has_url(url):
        return "PEND"   # probable/revisar con URL = aún por revisar a mano
    return "VAC"        # sin URL = sin fuente


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/fase2/municipios_rs_local.csv")
    args = ap.parse_args()

    rows = list(csv.DictReader(Path(args.csv).open(encoding="utf-8")))
    total = len(rows)

    verde = amarillo = cerrado = rojo = 0
    bc = {"CONF": 0, "PEND": 0, "VAC": 0}
    bp = {"CONF": 0, "PEND": 0, "VAC": 0}

    for r in rows:
        c = bucket_state(r.get("confianza_concursos", ""), r.get("url_concursos", ""))
        p = bucket_state(r.get("confianza_processos", ""), r.get("url_processos_seletivos", ""))
        bc[c] += 1
        bp[p] += 1
        states = {c, p}
        if c == "CONF" and p == "CONF":
            verde += 1
        elif "PEND" in states:
            amarillo += 1
        elif c == "VAC" and p == "VAC":
            rojo += 1
        else:                       # 1 CONF + 1 VAC, sin pendientes
            cerrado += 1

    def pct(n: int) -> str:
        return f"{n} ({100*n/total:.1f}%)"

    print(f"=== OVERVIEW DATASET ({total} municipios) ===")
    print(f"  VERDE    confirmados (pleno):              {pct(verde)}")
    print(f"  AMARILLO pendientes (sin revisar aun):     {pct(amarillo)}   <- baja con cada tanda")
    print(f"  ROJO     sin sitio:                        {pct(rojo)}")
    print(f"  cerrados parciales (1 confirmado+1 vacio): {pct(cerrado)}")
    print(f"--- por bucket (de {total}) ---")
    print(f"  Concursos: confirmado {bc['CONF']} | pendiente {bc['PEND']} | vacio {bc['VAC']}")
    print(f"  Processos: confirmado {bp['CONF']} | pendiente {bp['PEND']} | vacio {bp['VAC']}")


if __name__ == "__main__":
    main()
