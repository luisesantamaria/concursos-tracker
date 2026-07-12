#!/usr/bin/env python3
"""Overview del dataset fase 2 — conteo POR URL (bucket), no por municipio.

Cada municipio tiene 2 buckets/URLs (concursos, processos) → 497×2 = 994 URLs.
Estado de cada URL:
  🟢 CONFIRMADA      : confianza=confirmado con URL (índice oficial válido)
  🟡 PENDIENTE       : tiene URL pero aún en probable/revisar (sin verificar a mano)
  🟠 VACIA SIN REVISAR: sin URL y SIN marca de Chrome (el pipeline no halló nada; falta mano-negra)
  🔴 VACIA VERIFICADA : sin URL pero con nota [SIN_INDICE_OFICIAL_CHROME:<bucket>]
                        (Chrome cavó a fondo y confirmó que no existe índice oficial)

"Trabajo real pendiente" = PENDIENTE + VACIA SIN REVISAR.

Tambien da el resumen por municipio (verde/amarillo/cerrado/rojo) como referencia.

Uso: python scripts/eval/rollup_overview.py [--csv data/fase2/municipios_rs_local.csv]
"""
from __future__ import annotations
import argparse
import csv
from pathlib import Path


def has_url(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("http")


def bucket_state(conf: str, url: str, notes: str, bucket: str) -> str:
    if (conf or "").strip() == "confirmado" and has_url(url):
        return "CONF"
    if has_url(url):
        return "PEND"
    n = (notes or "").lower()
    if f"[sin_indice_oficial_chrome:{bucket}]" in n:
        return "VAC_VERIF"
    return "VAC_RAW"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/fase2/municipios_rs_local.csv")
    args = ap.parse_args()

    rows = list(csv.DictReader(Path(args.csv).open(encoding="utf-8")))
    nmun = len(rows)
    nurl = nmun * 2

    tot = {"CONF": 0, "PEND": 0, "VAC_VERIF": 0, "VAC_RAW": 0}
    bc = {"CONF": 0, "PEND": 0, "VAC_VERIF": 0, "VAC_RAW": 0}
    bp = {"CONF": 0, "PEND": 0, "VAC_VERIF": 0, "VAC_RAW": 0}
    # municipio-level (referencia)
    verde = amarillo = cerrado = rojo = 0

    for r in rows:
        notes = r.get("notes", "")
        c = bucket_state(r.get("confianza_concursos", ""), r.get("url_concursos", ""), notes, "concursos")
        p = bucket_state(r.get("confianza_processos", ""), r.get("url_processos_seletivos", ""), notes, "processos")
        for st in (c, p):
            tot[st] += 1
        bc[c] += 1
        bp[p] += 1
        states = {c, p}
        if c == "CONF" and p == "CONF":
            verde += 1
        elif "PEND" in states:
            amarillo += 1
        elif c == "CONF" or p == "CONF":     # 1 confirmado + 1 vacio
            cerrado += 1
        else:                                # ambos vacios
            rojo += 1

    def pu(n: int) -> str:
        return f"{n} ({100*n/nurl:.1f}%)"

    def pm(n: int) -> str:
        return f"{n} ({100*n/nmun:.1f}%)"

    print(f"=== OVERVIEW POR URL ({nurl} URLs = {nmun} municipios x 2 buckets) ===")
    print(f"  CONFIRMADAS:           {pu(tot['CONF'])}")
    print(f"  PENDIENTES (probable): {pu(tot['PEND'])}")
    print(f"  VACIAS SIN REVISAR:    {pu(tot['VAC_RAW'])}   <- falta mano-negra")
    print(f"  VACIAS VERIFICADAS:    {pu(tot['VAC_VERIF'])}   <- Chrome confirmo no-indice")
    work = tot["PEND"] + tot["VAC_RAW"]
    print(f"  >> TRABAJO REAL PENDIENTE (pend + vacias sin revisar): {work} URLs")
    print(f"--- por bucket ---")
    print(f"  Concursos: conf {bc['CONF']} | pend {bc['PEND']} | vac_sin_rev {bc['VAC_RAW']} | vac_verif {bc['VAC_VERIF']}")
    print(f"  Processos: conf {bp['CONF']} | pend {bp['PEND']} | vac_sin_rev {bp['VAC_RAW']} | vac_verif {bp['VAC_VERIF']}")
    print(f"--- referencia por municipio ({nmun}) ---")
    print(f"  verde/pleno {pm(verde)} | amarillo/pend {pm(amarillo)} | cerrado-parcial {pm(cerrado)} | rojo/sin-sitio {pm(rojo)}")


if __name__ == "__main__":
    main()
