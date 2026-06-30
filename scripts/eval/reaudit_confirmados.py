#!/usr/bin/env python3
"""Re-auditor de confirmados: la red de seguridad de la regla de oro.

Filosofía (definida con Luis): `confirmado` debe ser 100% cierto. Un solo paso del
cierre da ~95%; lo que se filtra (errores intermitentes, link-rot, categorías que se
vacían, soft-404 que aparece después) se caza re-verificando PERIÓDICAMENTE. Este
script re-corre SOLO los buckets ya `confirmado` por la MISMA compuerta de precisión
del cierre (`rendered_verdict`). Si uno ya no verifica -> `revisar`.

Propiedades clave:
  - **No puede meter falsos positivos:** nunca promueve ni investiga; solo degrada.
    Por eso es seguro correrlo cuantas veces se quiera (costo cero de recall).
  - **No degrada por un parpadeo de red:** si la página sale "inaccesible/render-vacío"
    reintenta una vez; si sigue inaccesible la DEJA confirmada y la marca como
    "no verificada esta vez" (transitoria) en vez de tirarla. Solo degrada ante un
    veredicto SUSTANTIVO (error de servidor, soft-404, sin items, tipo_equivocado…).

Uso:
  # ver qué caería sin tocar el dataset
  python scripts/eval/reaudit_confirmados.py --input data/fase2/municipios_rs_local.csv \
      --report-only
  # re-auditar y escribir el dataset actualizado
  python scripts/eval/reaudit_confirmados.py --input data/fase2/municipios_rs_local.csv \
      --output data/fase2/municipios_rs_local.csv
"""
from __future__ import annotations
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import cascade_municipios as C   # noqa: E402
import cierre_dataset as Z       # noqa: E402

# Motivos que NO son evidencia de que la página sea mala, sino de que no se pudo
# leer ahora (bloqueo/red/render). Ante esto NO se degrada: se reintenta y, si
# persiste, se conserva el confirmado marcándolo como no-verificado-esta-vez.
_TRANSIENT_HINTS = ("inaccesible", "render-vacio", "verdict-error", "sin api key")


def _is_transient(motivo: str) -> bool:
    m = (motivo or "").lower()
    return any(h in m for h in _TRANSIENT_HINTS)


def reverify(session, model, muni, bucket, url, timeout):
    """Devuelve ('confirmado'|'revisar'|'transitorio', motivo). 'transitorio' = no se
    pudo verificar ahora pero NO hay evidencia de página mala -> conservar confirmado.

    Reintenta UNA vez ante señales que pueden ser un parpadeo (inaccesible/render
    vacío, o 'sin items' por una lista que no cargó esta vez): así no degradamos un
    índice real por un blip. Solo degrada si la 2ª lectura confirma el problema, o
    ante un veredicto sustantivo de entrada (error de servidor, soft-404, tipo
    equivocado) que no es atribuible a la red."""
    estado, motivo = Z.rendered_verdict(session, model, muni, bucket, url, timeout)
    if estado == "confirmado":
        return "confirmado", motivo
    retryable = _is_transient(motivo) or "sin items" in motivo.lower()
    if retryable:
        estado2, motivo2 = Z.rendered_verdict(session, model, muni, bucket, url, timeout)
        if estado2 == "confirmado":
            return "confirmado", motivo2
        if _is_transient(motivo2):
            return "transitorio", motivo2   # red caída: conservar, re-chequear próxima
        return "revisar", motivo2           # problema persistente -> degradar
    return "revisar", motivo                # server-error / 404 / tipo -> degradar


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path,
                    help="CSV de salida (omitir con --report-only)")
    ap.add_argument("--report-only", action="store_true",
                    help="No escribe; solo lista lo que caería a revisar")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not args.report_only and not args.output:
        ap.error("usa --output <csv> o --report-only")

    rows = list(csv.DictReader(args.input.open(encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]
    cols = list(rows[0].keys()) if rows else []
    session = C.make_session()

    n_conf = 0
    demotions = []   # (muni, bucket, url, motivo)
    transient = []   # (muni, bucket, url, motivo)

    for i, r in enumerate(rows, 1):
        muni = r["municipio"]
        for bucket, ucol, ccol in Z.BUCKETS:
            if (r.get(ccol) or "").strip() != "confirmado":
                continue
            url = (r.get(ucol) or "").strip()
            if not url.startswith("http"):
                continue
            n_conf += 1
            estado, motivo = reverify(session, args.model, muni, bucket, url, args.timeout)
            if estado == "confirmado":
                continue
            if estado == "transitorio":
                transient.append((muni, bucket, url, motivo))
                if not args.report_only:
                    r["notes"] = (r.get("notes", "") +
                                  f" | reaudit[{bucket}]: no-verificada-esta-vez ({motivo[:50]})")[:1900]
                print(f"  ~ {muni} [{bucket}]: transitorio, se conserva ({motivo[:50]})", flush=True)
                continue
            # degradar
            demotions.append((muni, bucket, url, motivo))
            print(f"  ↓ {muni} [{bucket}]: confirmado -> revisar ({motivo[:55]})", flush=True)
            if not args.report_only:
                r[ccol] = "revisar"
                r["notes"] = (r.get("notes", "") +
                              f" | reaudit[{bucket}]: revisar ({motivo[:60]})")[:1900]

    if not args.report_only:
        w = csv.DictWriter(args.output.open("w", encoding="utf-8", newline=""), fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'='*60}")
    print(f"=== RE-AUDITORÍA DE CONFIRMADOS ===")
    print(f"  confirmados re-verificados:        {n_conf}")
    print(f"  🔻 degradados a revisar (FP/rot):   {len(demotions)}")
    print(f"  ~ no verificados esta vez (red):   {len(transient)} (se conservan)")
    if demotions:
        print("  --- degradados ---")
        for muni, bk, url, motivo in demotions:
            print(f"    {muni} [{bk}] {motivo[:50]} | {url[:55]}")
    if not args.report_only:
        print(f"  salida: {args.output}")
    else:
        print("  (report-only: no se escribió nada)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
