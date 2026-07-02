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
from urllib.parse import parse_qsl, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import cascade_municipios as C   # noqa: E402
import audit_fase2_rs as A       # noqa: E402
import verdict_extract as V      # noqa: E402

BUCKETS = [
    ("concursos", "url_concursos", "confianza_concursos", "tier_concursos"),
    ("processos", "url_processos_seletivos", "confianza_processos", "tier_processos"),
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


def _url_key(url: str, *, drop_default_query: bool = False) -> tuple[str, str, tuple]:
    """Canonical key for reachability comparison.

    Ignores scheme and leading www, keeps path and query. ``ano=0``/empty query
    variants are treated as same page only when explicitly requested.
    """
    try:
        p = urlparse(C.clean_url(url))
    except Exception:
        return ("", "", ())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "/").rstrip("/") or "/"
    query = tuple(sorted(parse_qsl(p.query, keep_blank_values=True)))
    if drop_default_query:
        query = tuple(
            (k, v) for k, v in query
            if not (k.lower() in {"ano", "year"} and v in {"", "0", "todos", "todas"})
        )
    return host, path, query


def _same_reachable_url(a: str, b: str) -> bool:
    """True when a menu/link URL proves reachability for the target URL."""
    if _url_key(a) == _url_key(b):
        return True
    return _url_key(a, drop_default_query=True) == _url_key(b, drop_default_query=True)


def _tier_proves_menu(tier: str) -> bool:
    """Discovery tiers that already prove current-menu reachability."""
    t = (tier or "").strip().lower()
    return t.startswith("t1") or t.startswith("t4")


def _link_matches_target(links: list[tuple[str, str]], target_url: str) -> tuple[bool, str]:
    for href, text in links:
        if _same_reachable_url(href, target_url):
            label = (text or "").strip()
            return True, f"menu_link:{label[:50] or href[:50]}"
    return False, ""


def _collect_page_links(session, page_url: str, timeout: int) -> tuple[list[tuple[str, str]], str]:
    home = C.fetch_page(session, page_url, timeout)
    if not home.ok:
        return [], f"page_inaccesible:{home.status or home.error or 'err'}"
    links = list(home.links or [])
    if getattr(home, "is_spa", False) or len(links) < 8:
        rendered = C._render_page_links(home.url, timeout)  # same helper used by Tier 1
        existing = {h for h, _ in links}
        links.extend((h, t) for h, t in rendered if h not in existing)
    return links, "page_ok"


def _menu_roots(site_base: str, target_url: str) -> list[str]:
    roots = [site_base]
    try:
        site = urlparse(C.clean_url(site_base))
        target = urlparse(C.clean_url(target_url))
    except Exception:
        return roots
    if site.netloc.lower() != target.netloc.lower() or not target.scheme:
        return roots
    first_segment = (target.path or "").strip("/").split("/", 1)[0]
    if first_segment in {"cidadao", "transparencia"}:
        subroot = f"{target.scheme}://{target.netloc}/{first_segment}"
        if subroot not in roots:
            roots.append(subroot)
    return roots


def menu_reachable(session, site_base: str, target_url: str, timeout: int) -> tuple[bool, str]:
    """Best-effort proof that target is reachable from the current official menu.

    This is a precision guard for non-menu discoveries (grounding, probes, repair):
    direct home/menu link or one obvious container page is enough. The caller
    decides whether missing proof is a hard downgrade or a ``menu_risk`` tag.
    """
    if not site_base or not site_base.startswith("http"):
        return False, "sin_site_base_para_menu"
    statuses: list[str] = []
    root_links: list[tuple[str, str]] = []
    for root in _menu_roots(site_base, target_url):
        links, status = _collect_page_links(session, root, timeout)
        statuses.append(f"{root}:{status}:{len(links)}links")
        root_links.extend(links)
        ok, why = _link_matches_target(links, target_url)
        if ok:
            return True, why

    containers: list[tuple[str, str]] = []
    all_container_terms = C.CONTAINER_KEYWORDS + [
        "concurso", "concursos", "processo seletivo", "processos seletivos",
        "selecao publica", "selecoes publicas", "pss",
    ]
    seen = set()
    for href, text in root_links:
        if href in seen or C.is_pdf_or_file(href) or C.is_broad_landing(href):
            continue
        seen.add(href)
        blob = C.norm(f"{text} {urlparse(href).path}")
        if any(term in blob for term in all_container_terms):
            containers.append((href, text))
    for href, text in containers[:8]:
        page = C.fetch_page(session, href, min(timeout, 12))
        if not page.ok or C.is_soft_404(page):
            continue
        ok, why = _link_matches_target(page.links or [], target_url)
        if ok:
            return True, f"container:{(text or href)[:40]}>{why}"
    return False, "no_link_desde_menu_actual; " + "; ".join(statuses[:3])


def apply_menu_reachability_guard(session, row: dict, bucket: str, url: str,
                                  tier: str, timeout: int) -> tuple[bool, str]:
    """Return (allowed, reason) for sealing `confirmado`."""
    if _tier_proves_menu(tier):
        return True, f"tier_menu:{tier}"
    site_base = (row.get("site_base") or "").strip()
    ok, why = menu_reachable(session, site_base, url, timeout)
    if ok:
        return True, f"menu_reachable:{why}"
    return False, f"sin_menu_reachability({tier or 'sin_tier'}): {why}"


def _fmt_extract_evidence(decision: str, ev: dict) -> str:
    certs = ",".join(f"{a}/{b}" for a, b in ev.get("certames", [])[:5])
    estado = ev.get("estado") or decision
    return (f"extract_{decision}: cert={ev.get('n_certames', 0)}"
            f"[{certs}] verif={ev.get('verif', 0)} piso={ev.get('piso', 0)}"
            f" off={ev.get('off_type', 0)} ciclo={ev.get('ciclo', 0)}"
            f" ajeno={ev.get('ajenos', 0)} shell={int(bool(ev.get('listing_shell')))}"
            f" bp={ev.get('binding_piso', 0)} mf={ev.get('meta_floor', 0)}"
            f" item={ev.get('item_here', 0)}/{ev.get('item_other', 0)}"
            f" pblock={int(bool(ev.get('piso_blocked')))}"
            f" estado={estado}")


def extract_verdict(session, model, municipio, bucket, title, text, anchors, timeout):
    """New falsifiable extractor gate: LLM transcribes, code adjudicates."""
    if not C.gemini_api_key():
        return ("revisar", "extract: sin api key")
    if (text or "").count("\n") < 3:
        return ("revisar", "extract: texto sin estructura de lineas (render fallido)")
    try:
        items = V.extract_items(text, session, C.gemini_post, model, timeout)
        decision, ev = V.adjudicate(
            text, bucket, municipio, items, anchors=anchors, title=title)
        conf = "confirmado" if decision == "confirmar" else "revisar"
        return conf, _fmt_extract_evidence(decision, ev)
    except Exception as e:
        return ("revisar", f"extract-error: {str(e)[:80]}")


def rendered_verdict(session, model, municipio, bucket, url, timeout,
                     extract_mode: str = "off"):
    """Render the page (browser if needed) and ask the discrete AI verdict.
    Returns ('confirmado'|'revisar', motivo). Never returns confirmado without a
    rendered, on-topic, valido_indice verdict.

    extract_mode:
      - off: old ai_verdict is the authority.
      - shadow: run verdict_extract in parallel, append telemetry only.
      - authority: use verdict_extract as the authority after deterministic guards.
    """
    # Hard block: a single-item/detail URL is never a valid index by the phase
    # rules, no matter how index-like its rendered content looks (a single concurso
    # page lists many sub-editais and can fool the verdict). Send it to revisar so
    # investigation/human finds the real index. Keeps `confirmado` airtight.
    if C.is_hard_detail_url(url):
        return ("revisar", "url de detalle inequivoca: no es indice (regla de fase)")
    pg = C.fetch_page(session, url, timeout)
    title = pg.title if (pg and pg.ok) else ""
    text = pg.text if (pg and pg.ok) else ""
    anchors = [{"href": h, "text": t} for h, t in (pg.links if (pg and pg.ok) else [])]
    # Render (browser + scroll) cuando el fetch plano viene fino, es SPA, o NO trae
    # items de listado: muchos CMS sirven el menu server-side pero cargan el listado
    # por JS/scroll, asi que un fetch "largo" puede ser solo el menu (Pinhal Grande
    # fetch=3934 era el menu; el render con scroll trae la verdad). Preferimos el
    # render si surface items O si el fetch no los tenia (aunque sea mas corto).
    force_render_for_extract = extract_mode in {"shadow", "authority"}
    need_render = force_render_for_extract or (not (pg and pg.ok)) \
        or getattr(pg, "is_spa", False) or len((text or "").strip()) < 500 \
        or not _has_real_listing_item(text)
    if need_render:
        r = A.render_page(url, timeout)
        if r and (_has_real_listing_item(r[1]) or len((r[1] or "").strip()) >= 500):
            title, text, anchors = r[0], r[1], r[2]
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

    shadow_note = ""
    if extract_mode in {"shadow", "authority"}:
        ex_conf, ex_motivo = extract_verdict(
            session, model, municipio, bucket, title, text, anchors, timeout)
        if extract_mode == "authority":
            return ex_conf, ex_motivo
        shadow_note = f"shadow:{ex_conf}:{ex_motivo[:220]} | "

    try:
        v, motivo = A.ai_verdict(session, model, municipio, bucket, title, text, timeout)
    except Exception as e:
        return ("revisar", f"{shadow_note}verdict-error: {str(e)[:60]}")
    if v == "valido_indice":
        # Re-fetch anti-intermitente: portales .aspx (sinsoft) dan "Runtime Error" a
        # ratos; si estaban arriba durante el render confirmaban en falso. Una 2a lectura
        # barata antes de sellar; si AHORA es error de servidor -> revisar. Un indice
        # estable pasa 2 veces; solo degradamos si el 2do fetch trae marcadores de error
        # (un fallo de red deja pg2 sin marcadores -> no degrada un indice bueno).
        pg2 = C.fetch_page(session, url, timeout)
        if pg2 and pg2.ok and _is_server_error(pg2.title, pg2.text):
            return ("revisar", f"{shadow_note}error de servidor intermitente (2do fetch)")
        return ("confirmado", f"{shadow_note}valido_indice: {motivo[:80]}")
    return ("revisar", f"{shadow_note}{v}: {motivo[:80]}")


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


def _repair_via_canonical(session, model, muni, bucket, current_url, site_base,
                          timeout, extract_mode: str = "off"):
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
            ver, mot = rendered_verdict(
                session, model, muni, bucket, cand, timeout, extract_mode)
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
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-investigate", action="store_true",
                    help="No re-descubrir buckets vacios; solo verificar URLs existentes")
    ap.add_argument("--no-repair", action="store_true",
                    help="No probar rutas canonicas cuando la URL cae a revisar")
    ap.add_argument("--require-menu-reachability", action="store_true",
                    help="Degradar confirmados no-menu sin prueba de reachability "
                         "desde el menu actual (default: solo etiqueta menu_risk)")
    ap.add_argument("--extract-shadow", action="store_true",
                    help="Ejecutar verdict_extract en sombra; ai_verdict viejo sigue "
                         "siendo la autoridad")
    ap.add_argument("--extract-authority", action="store_true",
                    help="Usar verdict_extract como autoridad del cierre (usar solo "
                         "despues de validar la corrida sombra)")
    args = ap.parse_args()
    extract_mode = "authority" if args.extract_authority else (
        "shadow" if args.extract_shadow else "off")

    rows = list(csv.DictReader(args.input.open(encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]
    cols = list(rows[0].keys()) if rows else []
    session = C.make_session()

    summ = {"confirmado": 0, "revisar": 0, "sin_sitio": 0}
    changed = {"promovido": 0, "degradado": 0, "investig_hallado": 0, "reparado": 0,
               "menu_risk": 0}

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

        for bk, ucol, ccol, tcol in BUCKETS:
            url = (r.get(ucol) or "").strip()
            prev = (r.get(ccol) or "").strip()
            final_url, final_conf, motivo = url, None, ""
            final_tier = (r.get(tcol) or "").strip()

            # Step 1 — verify the existing URL (if any).
            if url.startswith("http"):
                ver, motivo = rendered_verdict(
                    session, args.model, muni, bk, url, args.timeout, extract_mode)
                if ver == "confirmado":
                    final_conf = "confirmado"

            # Step 1.5 — REPAIR: si la URL no confirmo, probar rutas canonicas del host.
            # Barato (unos fetches) y antes del investigate (cascade completo, caro). Corrige
            # el error de URL-debil (el indice real existe en la canonica) sin arriesgar FP.
            if final_conf != "confirmado" and not args.no_repair:
                rurl, rmot = _repair_via_canonical(
                    session, args.model, muni, bk, url, r.get("site_base", ""),
                    args.timeout, extract_mode)
                if rurl:
                    final_url, final_conf, motivo = rurl, "confirmado", f"reparado: {rmot}"
                    final_tier = "repair"
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
                    ver2, motivo2 = rendered_verdict(
                        session, args.model, muni, bk, cand, args.timeout, extract_mode)
                    if ver2 == "confirmado":
                        final_url, final_conf, motivo = cand, "confirmado", motivo2
                        final_tier = (d.tier_concursos if bk == "concursos"
                                      else d.tier_processos) or "investigate"
                        if not url.startswith("http"):
                            changed["investig_hallado"] += 1

            # Step 2.8 — menu-reachability guard: non-menu discoveries (grounding,
            # probes, repairs) can point at a fossil index whose content is perfect
            # but no longer linked from the current official site. Default: tag the
            # risk for batch review; --require-menu-reachability makes it hard.
            if final_conf == "confirmado":
                ok_menu, menu_reason = apply_menu_reachability_guard(
                    session, r, bk, final_url, final_tier, args.timeout)
                if ok_menu:
                    motivo = f"{motivo[:110]} | {menu_reason}" if motivo else menu_reason
                else:
                    if args.require_menu_reachability:
                        final_conf = None
                        motivo = menu_reason
                    else:
                        risk = f"menu_risk:{menu_reason}"
                        motivo = f"{motivo[:95]} | {risk}" if motivo else risk
                        changed["menu_risk"] += 1

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
            r["notes"] = (r.get("notes", "") + f" | cierre[{bk}]: {r[ccol]} ({motivo[:360]})")[:3200]
            print(f"    {bk}: {prev or '-'} -> {r[ccol]}", flush=True)

            st = r[ccol]
            summ["confirmado" if st == "confirmado" else "revisar" if st == "revisar" else "sin_sitio"] += 1

    with args.output.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
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
          f"reparados (URL canonica) {changed['reparado']} | "
          f"menu_risk {changed['menu_risk']}")
    if extract_mode != "off":
        print(f"  verdict_extract: {extract_mode}")
    print(f"  salida: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
