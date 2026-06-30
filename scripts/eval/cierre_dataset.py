#!/usr/bin/env python3
"""Cierre del dataset fase 2: la ÚNICA autoridad del estado final de cada bucket.

Filosofía (definida con Luis): el cascade PROPONE (recall máximo, sondas, patrones);
este cierre DECIDE. Un bucket sólo es `confirmado` si un veredicto RENDERIZADO + IA lo
declara índice válido del tipo correcto. Así `confirmado` es intocable y un humano nunca
tiene que re-revisarlo. Cero falsos positivos: jamás confirma algo que no se verificó.

Por cada bucket:
  - tiene URL (confirmado/probable)  -> render + ai_verdict:
        valido_indice                -> confirmado
        tipo_equivocado/nao_e_indice/licitacao/erro/inaccesible -> revisar
  - vacío (sin URL)                  -> INVESTIGA (re-descubre con el cascade:
        grounded + menús + sondas), y si halla candidato lo verifica igual.
        Si no hay índice pero sí sitio -> revisar ; si no hay sitio -> sin_sitio.

Salida: dataset con confianza final + reporte (confirmado / revisar / sin_sitio).

Uso:
  python scripts/eval/cierre_dataset.py --input <in.csv> --output <out.csv> [--limit N]
        [--no-investigate]  (salta el re-descubrimiento de vacíos; sólo verifica URLs)
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import cascade_municipios as C   # noqa: E402
import audit_fase2_rs as A       # noqa: E402

BUCKETS = [
    ("concursos", "url_concursos", "confianza_concursos"),
    ("processos", "url_processos_seletivos", "confianza_processos"),
]

# Server-side error pages served with HTTP 200 (ASP.NET "Runtime Error", PHP/SQL
# stack traces, 500/503 bodies). Their rendered text has no listing, but the AI
# verdict can occasionally still mis-read it. A page matching these is never a
# valid index -> revisar. Caught the sinsoft .aspx portals (Gramado dos Loureiros)
# that intermittently 500 yet were being confirmed.
SERVER_ERROR_MARKERS = [
    "server error in", "runtime error", "application error",
    "erro de execucao", "internal server error", "http error 500",
    "service unavailable", "error 503", "nao foi possivel conectar-se ao banco",
    "could not connect", "error establishing a database",
    "whoops, something went wrong", "exception details",
]


def _is_server_error(title: str, text: str) -> bool:
    blob = C.norm((title or "") + " " + (text or "")[:600])
    return any(m in blob for m in SERVER_ERROR_MARKERS)


# NARROW, low-collateral deterministic guards for the FP categories that ARE
# cleanly detectable. The fuzzy ones (single-concurso, type-mixed) are left to the
# AI verdict + the human/Chrome final audit — trying to catch them with stricter
# rules demoted real indexes (Água Santa, Pareci Novo), so we do NOT.

def _is_generic_editais(text: str) -> bool:
    """Page whose items are licitação / chamamento / pregão / exumação (generic
    administrative editais), with almost no concurso/PSS content. Catches a
    concursos page that is really the generic 'Editais' repository (Sapiranga)."""
    t = C.norm(text or "")
    generic = sum(t.count(k) for k in
                  ("chamamento", "licitacao", "licitação", "pregao", "dispensa",
                   "exumacao", "tomada de preco", "inexigibilidade"))
    relevant = sum(t.count(k) for k in
                   ("concurso publico", "concurso público", "processo seletivo",
                    "selecao publica", "seleção pública"))
    return generic >= 3 and relevant <= 1


_DEFINITION_PHRASES = [
    "e um processo seletivo", "é um processo seletivo", "e o procedimento",
    "é o procedimento", "tem por objetivo selecionar", "destina-se a selecionar",
    "e uma forma de", "é uma forma de",
]


def _is_definition_page(text: str) -> bool:
    """Short explanatory page ('Concurso Público é um processo...') with no real
    listing. Narrow: only fires on short pages with definition phrasing and no
    edital number (Pinhal Grande)."""
    t = text or ""
    if len(t) > 2600:
        return False
    blob = C.norm(t)
    has_def = any(p in blob for p in _DEFINITION_PHRASES)
    has_edital_num = bool(re.search(r"\b\d{1,4}\s*[/.\-]\s*20[12]\d\b", t))
    return has_def and not has_edital_num


def rendered_verdict(session, model, municipio, bucket, url, timeout):
    """Render the page (browser if needed) and ask the discrete AI verdict.
    Returns ('confirmado'|'revisar', motivo). Never returns confirmado without a
    rendered, on-topic, valido_indice verdict."""
    # Hard block: a single-item/detail URL is never a valid index by the phase
    # rules, no matter how index-like its rendered content looks (a single concurso
    # page lists many sub-editais and can fool the verdict). Send it to revisar so
    # investigation/human finds the real index. Keeps `confirmado` airtight.
    if C.is_detail_url(url):
        return ("revisar", "url de detalle: no es indice (regla de fase)")
    pg = C.fetch_page(session, url, timeout)
    title = pg.title if (pg and pg.ok) else ""
    text = pg.text if (pg and pg.ok) else ""
    need_render = (not (pg and pg.ok)) or getattr(pg, "is_spa", False) \
        or len((text or "").strip()) < 500
    if need_render:
        r = A.render_page(url, timeout)
        if r and len((r[1] or "")) > len(text or ""):
            title, text = r
    if not (text or "").strip():
        return ("revisar", "inaccesible/render-vacio")
    if _is_server_error(title, text):
        return ("revisar", "pagina de error de servidor (no es indice)")
    # Guards deterministas NARROW (bajo colateral). Lo difuso (concurso unico,
    # tipo-mixto) se deja a la IA + auditoria humana: endurecer la IA demotaba
    # indices reales (Agua Santa/Pareci Novo).
    if _is_generic_editais(text):
        return ("revisar", "editais genericos/licitacao dominante (no es indice de concurso/PSS)")
    if _is_definition_page(text):
        return ("revisar", "pagina de definicion sin listado (no es indice)")
    if not C.gemini_api_key():
        return ("revisar", "sin api key")
    try:
        v, motivo = A.ai_verdict(session, model, municipio, bucket, title, text, timeout)
    except Exception as e:
        return ("revisar", f"verdict-error: {str(e)[:60]}")
    if v == "valido_indice":
        return ("confirmado", f"valido_indice: {motivo[:80]}")
    return ("revisar", f"{v}: {motivo[:80]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-investigate", action="store_true",
                    help="No re-descubrir buckets vacios; solo verificar URLs existentes")
    args = ap.parse_args()

    rows = list(csv.DictReader(args.input.open(encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]
    cols = list(rows[0].keys()) if rows else []
    session = C.make_session()

    summ = {"confirmado": 0, "revisar": 0, "sin_sitio": 0}
    changed = {"promovido": 0, "degradado": 0, "investig_hallado": 0}

    for i, r in enumerate(rows, 1):
        muni = r["municipio"]
        print(f"[{i}/{len(rows)}] {muni}", flush=True)
        discovered = None  # lazy: only run the cascade once, if some bucket needs it

        def investigate():
            nonlocal discovered
            if discovered is None and not args.no_investigate:
                try:
                    discovered = C.process_municipio(
                        session, muni, args.model, args.timeout, use_playwright=True)
                except Exception as e:
                    discovered = False
                    print(f"    investig error: {str(e)[:70]}", flush=True)
            return discovered if discovered else None

        for bk, ucol, ccol in BUCKETS:
            url = (r.get(ucol) or "").strip()
            prev = (r.get(ccol) or "").strip()
            final_url, final_conf, motivo = url, None, ""

            # Step 1 — verify the existing URL (if any).
            if url.startswith("http"):
                ver, motivo = rendered_verdict(session, args.model, muni, bk, url, args.timeout)
                if ver == "confirmado":
                    final_conf = "confirmado"

            # Step 2 — if not confirmed, INVESTIGATE: re-discover and verify a fresh
            # candidate (this is the "auto mano-negra" over empty AND revisar buckets).
            if final_conf != "confirmado":
                d = investigate()
                cand = ""
                if d:
                    cand = (d.url_concursos if bk == "concursos"
                            else d.url_processos_seletivos) or ""
                if cand.startswith("http") and cand != url:
                    ver2, motivo2 = rendered_verdict(session, args.model, muni, bk, cand, args.timeout)
                    if ver2 == "confirmado":
                        final_url, final_conf, motivo = cand, "confirmado", motivo2
                        if not url.startswith("http"):
                            changed["investig_hallado"] += 1

            # Step 3 — finalize the bucket state.
            site = (r.get("site_base") or "").strip()
            if discovered and discovered is not False:
                site = site or (discovered.site_base or "")
            if final_conf == "confirmado":
                r[ucol] = final_url
                r[ccol] = "confirmado"
                if prev != "confirmado":
                    changed["promovido"] += 1
            else:
                if prev == "confirmado":
                    changed["degradado"] += 1
                # keep a URL (existing or discovered) for the human to look at
                if not r.get(ucol, "").startswith("http") and final_url.startswith("http"):
                    r[ucol] = final_url
                if r.get(ucol, "").startswith("http") or site.startswith("http"):
                    r[ccol] = "revisar"
                else:
                    r[ccol] = ""   # sin sitio oficial
            r["notes"] = (r.get("notes", "") + f" | cierre[{bk}]: {r[ccol]} ({motivo[:70]})")[:1900]
            print(f"    {bk}: {prev or '-'} -> {r[ccol]}", flush=True)

            st = r[ccol]
            summ["confirmado" if st == "confirmado" else "revisar" if st == "revisar" else "sin_sitio"] += 1

    w = csv.DictWriter(args.output.open("w", encoding="utf-8", newline=""), fieldnames=cols)
    w.writeheader()
    w.writerows(rows)

    n = len(rows) * 2
    print(f"\n{'='*56}")
    print(f"=== CIERRE DEL DATASET ({len(rows)} municipios, {n} buckets) ===")
    print(f"  🟢 confirmado (intocable, verificado): {summ['confirmado']} ({100*summ['confirmado']/n:.0f}%)")
    print(f"  🟠 revisar (humano triajea):           {summ['revisar']} ({100*summ['revisar']/n:.0f}%)")
    print(f"  🔴 sin_sitio:                          {summ['sin_sitio']} ({100*summ['sin_sitio']/n:.0f}%)")
    print(f"  movimientos: promovidos {changed['promovido']} | degradados {changed['degradado']} | "
          f"hallados por investigacion {changed['investig_hallado']}")
    print(f"  salida: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
