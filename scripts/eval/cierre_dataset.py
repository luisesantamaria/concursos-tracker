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
from urllib.parse import urlparse

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


# Soft-404: pagina servida con HTTP 200 cuyo titulo/cuerpo dice "nao encontrada".
# Categoria WordPress muerta (Estacao C/P: /publicacoes_legais?categ=concursos ->
# "Pagina nao encontrada") que ademas traia una fecha/timestamp que el regex de
# nº-edital confundia con un item -> pasaba como indice. Nunca es indice -> revisar.
# Solo marcadores INEQUIVOCOS de 404 (en titulo o inicio del cuerpo). Se omiten
# "nenhum resultado/nada encontrado" a proposito: aparecen en buscadores de indices
# reales con filtro vacio y degradarian buenos.
NOT_FOUND_MARKERS = [
    "pagina nao encontrada", "page not found", "404 not found", "erro 404",
    "conteudo nao encontrado", "404 - ", "erro 404",
]


def _is_not_found(title: str, text: str) -> bool:
    blob = C.norm((title or "") + " " + (text or "")[:400])
    return any(m in blob for m in NOT_FOUND_MARKERS)


# NARROW, low-collateral deterministic guards for the FP categories that ARE
# cleanly detectable. The fuzzy ones (single-concurso, type-mixed) are left to the
# AI verdict + the human/Chrome final audit — trying to catch them with stricter
# rules demoted real indexes (Água Santa, Pareci Novo), so we do NOT.

_DEFINITION_PHRASES = [
    "e um processo seletivo", "é um processo seletivo", "e o procedimento",
    "é o procedimento", "tem por objetivo selecionar", "destina-se a selecionar",
    "e uma forma de", "é uma forma de",
]


def _is_definition_page(text: str) -> bool:
    """Explanatory page ('Concurso Público é um processo...') with no real listing.
    LENGTH-INDEPENDENT: the deterministic render (networkidle) can grow such a page
    past any size threshold, so we judge by SIGNAL, not length — definition phrasing
    AND zero real listing items. A genuine index never has the definition sentence
    *and* zero items (Pinhal Grande C escaped the old len<2600 gate after render)."""
    t = text or ""
    blob = C.norm(t)
    has_def = any(p in blob for p in _DEFINITION_PHRASES)
    return has_def and not _has_real_listing_item(t)


# A page that names the bucket ("Concurso Público", "Processo Seletivo") only in its
# heading, intro text, or NAV MENU — with no actual edital — is NOT an index. Two
# real false positives had exactly this shape: a definition page (Pinhal Grande C)
# and an empty category page whose only PSS mention was the side menu (Pinhal Grande
# P). The rule is robust against indexes that list by bare year (which broke the old
# >=2 item count): an item counts if there is an edital number, a cascade listing
# hit, OR the bucket keyword next to a 4-digit year (atende "Concurso Público 2021").
# Numero de edital tipo NN/AAAA o NN-AAAA (01/2024, 071-2026). Lookbehind (?<![\d.])
# para NO morder la cola de un numero de LEI ("Lei 13.019/2014" -> "019/2014"): ese
# falso item hacia que el menu pasara como indice. Sin "." de separador (los editais
# usan / o -, no punto), asi "13.019" no dispara.
_EDITAL_NUM_RE = re.compile(r"(?<![\d.])\d{1,3}\s*[/\-]\s*20[12]\d\b")
# Bucket keyword next to a 4-digit year. BROAD on purpose (bare "Concurso 2012",
# not only "Concurso Público"): Gramado lists "Concurso 2012" with no "Público".
# Over-matching is safe here — the guard only GATES (no item -> revisar), so a broad
# match just defers to the AI; under-matching is what wrongly demotes real indexes.
_KEYWORD_YEAR_RE = re.compile(
    r"(concurso|processo\s+seletivo|sele[çc]\w+\s+p\w+)[^\n]{0,15}?\b20[12]\d\b",
    re.I)


# Frases fuertes de un item de listado (las alternativas TEXTUALES del LISTING_RE del
# cascade, SIN la de numero pelado "\d{1,3}/20\d\d" que muerde "Lei 13.019/2014").
_LISTING_PHRASE_RE = re.compile(
    r"edital\s+n|inscri[cç][oõ]es\s+(aberta|encerrada)|"
    r"resultado\s+(final|parcial|preliminar)|homologa[cç][aã]o|retifica[cç][aã]o",
    re.I)


def _has_real_listing_item(text: str) -> bool:
    """True if the page exposes >=1 concrete listing item (not just the keyword in a
    heading/menu). Deliberately permissive so year-listed indexes still pass."""
    t = text or ""
    if _EDITAL_NUM_RE.search(t):
        return True
    if _LISTING_PHRASE_RE.search(t):
        return True
    if _KEYWORD_YEAR_RE.search(t):
        return True
    return False


def rendered_verdict(session, model, municipio, bucket, url, timeout):
    """Render the page (browser if needed) and ask the discrete AI verdict.
    Returns ('confirmado'|'revisar', motivo). Never returns confirmado without a
    rendered, on-topic, valido_indice verdict."""
    # Hard block: a single-item/detail URL is never a valid index by the phase
    # rules, no matter how index-like its rendered content looks (a single concurso
    # page lists many sub-editais and can fool the verdict). Send it to revisar so
    # investigation/human finds the real index. Keeps `confirmado` airtight.
    if C.is_hard_detail_url(url):
        return ("revisar", "url de detalle inequivoca: no es indice (regla de fase)")
    pg = C.fetch_page(session, url, timeout)
    title = pg.title if (pg and pg.ok) else ""
    text = pg.text if (pg and pg.ok) else ""
    # Render (browser + scroll) cuando el fetch plano viene fino, es SPA, o NO trae
    # items de listado: muchos CMS sirven el menu server-side pero cargan el listado
    # por JS/scroll, asi que un fetch "largo" puede ser solo el menu (Pinhal Grande
    # fetch=3934 era el menu; el render con scroll trae la verdad). Preferimos el
    # render si surface items O si el fetch no los tenia (aunque sea mas corto).
    need_render = (not (pg and pg.ok)) or getattr(pg, "is_spa", False) \
        or len((text or "").strip()) < 500 or not _has_real_listing_item(text)
    if need_render:
        r = A.render_page(url, timeout)
        if r and (_has_real_listing_item(r[1]) or len((r[1] or "").strip()) >= 500):
            title, text = r
    if not (text or "").strip():
        return ("revisar", "inaccesible/render-vacio")
    if _is_server_error(title, text):
        return ("revisar", "pagina de error de servidor (no es indice)")
    if _is_not_found(title, text):
        return ("revisar", "soft-404 / pagina no encontrada (no es indice)")
    # Guard determinista de bajo colateral: solo paginas de DEFINICION (probadas
    # sin colateral en golden). El guard de "editais genericos" se descarto: degradaba
    # indices reales que mencionan chamamento/licitacao (Almirante, Anta Gorda). Lo
    # difuso (generico, single-concurso, tipo-mixto) se deja a la IA + auditoria Chrome:
    # NO es separable deterministamente de los indices reales sin romper buenos.
    if _is_definition_page(text):
        return ("revisar", "pagina de definicion sin listado (no es indice)")
    # Regla de oro: sin >=1 item de listado real (solo la palabra clave en titulo/menu),
    # NO es indice -> revisar. Con el render+scroll de arriba, un indice de verdad ya
    # mostro sus items (Pareci Novo: 50 PSS con fecha); si aun asi no hay item, es una
    # definicion o categoria vacia (Pinhal Grande C/P). Permisivo (anio/numero/LISTING)
    # para no tocar los que listan por anio.
    if not _has_real_listing_item(text):
        return ("revisar", "sin items de listado real (definicion/menu/vacia)")
    if not C.gemini_api_key():
        return ("revisar", "sin api key")
    try:
        v, motivo = A.ai_verdict(session, model, municipio, bucket, title, text, timeout)
    except Exception as e:
        return ("revisar", f"verdict-error: {str(e)[:60]}")
    if v == "valido_indice":
        # Re-fetch anti-intermitente: portales .aspx (sinsoft) dan "Runtime Error" a
        # ratos; si estaban arriba durante el render confirmaban en falso. Una 2a lectura
        # barata antes de sellar; si AHORA es error de servidor -> revisar. Un indice
        # estable pasa 2 veces; solo degradamos si el 2do fetch trae marcadores de error
        # (un fallo de red deja pg2 sin marcadores -> no degrada un indice bueno).
        pg2 = C.fetch_page(session, url, timeout)
        if pg2 and pg2.ok and _is_server_error(pg2.title, pg2.text):
            return ("revisar", "error de servidor intermitente (2do fetch)")
        return ("confirmado", f"valido_indice: {motivo[:80]}")
    return ("revisar", f"{v}: {motivo[:80]}")


# Rutas canonicas por bucket (patrones recurrentes: govbr /site/concursos, IPM /concurso,
# pg.php, portal-da-transparencia, /portal/editais/3). Se prueban cuando la URL del dataset
# cae a revisar: muchas veces el INDICE REAL existe en la ruta canonica y el pipeline eligio
# una URL debil (vista filtrada '/site/editais?tipo=N', menu de transparencia, o el sibling
# del tipo equivocado -> Esperanca do Sul, Mato Leitao P). Reparar = probar estas con la MISMA
# vara estricta (rendered_verdict) y quedarse con la que verifique, corrigiendo la URL. Cero
# FP: solo promueve lo que el gate confirma.
_CANON_COMBINED = ["/site/concursos", "/concurso", "/portal/editais/3"]
CANONICAL_PATHS = {
    "concursos": _CANON_COMBINED + [
        "/concursos-publicos/", "/concursos/", "/pg.php?area=CONCURSOPUBLICO",
        "/portal-da-transparencia/concursos-publicos"],
    "processos": _CANON_COMBINED + [
        "/processos-seletivos/", "/processo-seletivo", "/site/selecoes",
        "/pg.php?area=PROCESSOSELETIVO", "/portal-da-transparencia/processos-seletivos",
        "/portal-da-transparencia/contratacoes-emergenciais"],
}


def _host_base(url: str) -> str:
    try:
        p = urlparse(url or "")
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def _repair_via_canonical(session, model, muni, bucket, current_url, site_base, timeout):
    """Prueba rutas canonicas del host; devuelve (url, motivo) de la primera que verifica
    como indice valido del tipo, o (None, '') si ninguna. Corrige el error de DESCUBRIMIENTO
    (URL debil) sin arriesgar FP (todo pasa por rendered_verdict)."""
    base = _host_base(current_url) or _host_base(site_base)
    if not base:
        return None, ""
    seen = {(current_url or "").rstrip("/")}
    for path in CANONICAL_PATHS.get(bucket, []):
        cand = base + path
        if cand.rstrip("/") in seen:
            continue
        seen.add(cand.rstrip("/"))
        try:
            ver, mot = rendered_verdict(session, model, muni, bucket, cand, timeout)
        except Exception:
            continue
        if ver == "confirmado":
            return cand, mot
    return None, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-investigate", action="store_true",
                    help="No re-descubrir buckets vacios; solo verificar URLs existentes")
    ap.add_argument("--no-repair", action="store_true",
                    help="No probar rutas canonicas cuando la URL cae a revisar")
    args = ap.parse_args()

    rows = list(csv.DictReader(args.input.open(encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]
    cols = list(rows[0].keys()) if rows else []
    session = C.make_session()

    summ = {"confirmado": 0, "revisar": 0, "sin_sitio": 0}
    changed = {"promovido": 0, "degradado": 0, "investig_hallado": 0, "reparado": 0}

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

            # Step 1.5 — REPAIR: si la URL no confirmo, probar rutas canonicas del host.
            # Barato (unos fetches) y antes del investigate (cascade completo, caro). Corrige
            # el error de URL-debil (el indice real existe en la canonica) sin arriesgar FP.
            if final_conf != "confirmado" and not args.no_repair:
                rurl, rmot = _repair_via_canonical(
                    session, args.model, muni, bk, url, r.get("site_base", ""), args.timeout)
                if rurl:
                    final_url, final_conf, motivo = rurl, "confirmado", f"reparado: {rmot}"
                    changed["reparado"] += 1

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
          f"hallados por investigacion {changed['investig_hallado']} | "
          f"reparados (URL canonica) {changed['reparado']}")
    print(f"  salida: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
