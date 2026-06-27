"""Deterministic auditor for the fase 2 output (municipality index pages).

Re-fetches every CONFIRMED url in a cascade output CSV and checks objective
signals that it is really a *living index page of the right type* — not a dead
link, a PDF, a single detail page, a licitacao, or a page of the opposite
bucket. It calls NO AI and makes NO selection decisions: it only flags rows a
human should look at, so hidden false positives surface as a finite, reviewable
list instead of an open-ended worry. Re-run it any time to catch link rot too.

Run it from an environment that can actually reach the sites (i.e. from Brazil,
same as the cascade), so blocks are not mistaken for dead links.

    python scripts/eval/audit_fase2_rs.py \
        --input data/fase2/municipios_rs_local.csv --detalle

What it CANNOT catch: semantic ambiguity (a real listing, right keywords, but
wrong legal type — e.g. "Processo Seletivo Publico" that is legally a concurso).
That residue needs a human spot-check; this tool quantifies everything else.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

# Reuse the cascade's fetch (with curl_cffi/browser fallback) and signals, so
# the auditor sees pages exactly as the pipeline did.
_FASE2 = Path(__file__).resolve().parents[1] / "fase2_municipios"
sys.path.insert(0, str(_FASE2))
import json  # noqa: E402

from cascade_municipios_rs import (  # noqa: E402
    make_session, fetch_page, is_pdf_or_file, norm,
    LISTING_RE, BUCKET_KEYWORDS,
    _get_browser, gemini_post, gemini_api_key,
)

REJECT_KEYWORDS = [
    "licitacao", "licitacoes", "pregao", "chamamento publico", "tomada de preco",
    "concorrencia publica", "dispensa de licitacao",
    "soberana", "rainha", "garota", "majestade",  # cultural contests
]

DETAIL_PATH_HINTS = ["/detalhe/", "/noticia/", "/noticias/", "/visualizar/", "/view/"]

BUCKET_LABEL = {
    "concursos": "concursos públicos",
    "processos": "processos seletivos (PSS / seleções)",
}


def distinct_listing_items(text: str) -> int:
    """How many distinct edital-like items the page text exposes."""
    return len({m.group(0).lower() for m in LISTING_RE.finditer(text or "")})


def render_page(url: str, timeout: int = 25) -> tuple[str, str] | None:
    """Open the URL in a real browser and return (title, visible_text).

    For JS-rendered municipal portals (atende.net, oxy.elotech, etc.) whose menu
    and listing only exist after client-side rendering. Returns None if the
    browser is unavailable or the load fails.
    """
    try:
        browser = _get_browser()
    except Exception as e:
        print(f"      render unavailable: {e}", flush=True)
        return None
    ctx = None
    try:
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2500)
        title = page.title() or ""
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        return title, text or ""
    except Exception as e:
        print(f"      render error: {str(e)[:120]}", flush=True)
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _parse_json_object(text: str) -> dict | None:
    """Best-effort parse of a single JSON object from a model response."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    try:
        obj = json.loads(s)
        return obj[0] if isinstance(obj, list) and obj else (
            obj if isinstance(obj, dict) else None)
    except Exception:
        pass
    # Fall back to the first {...} block.
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            return None
    return None


def ai_verdict(session, model: str, municipio: str, bucket: str,
               title: str, text: str, timeout: int = 30) -> tuple[str, str]:
    """Discrete Gemini verdict on whether a page is a valid index of the bucket.

    Returns (veredicto, motivo) where veredicto is one of:
      valido_indice | tipo_equivocado | nao_e_indice | licitacao_ou_cultural | erro
    No scores — a single discrete decision, per project rules.
    """
    if not gemini_api_key():
        return "erro", "sin api key"
    prompt = (
        "Você é um auditor de páginas de índice de concursos municipais (RS, Brasil).\n"
        f"Município: {municipio}. BUCKET ALVO: {BUCKET_LABEL.get(bucket, bucket)}.\n\n"
        "Conteúdo renderizado da página:\n"
        f"TÍTULO: {title[:200]}\n"
        f"TEXTO: {text[:3000]}\n\n"
        "A página funciona como ÍNDICE/LISTAGEM do bucket alvo? Responda com UMA "
        "decisão discreta (JSON):\n"
        '{"veredicto": "valido_indice" | "tipo_equivocado" | "nao_e_indice" '
        '| "licitacao_ou_cultural", "motivo": "frase curta"}\n\n'
        "Definições:\n"
        "- valido_indice: lista MÚLTIPLOS editais/processos DO TIPO do bucket alvo.\n"
        "- tipo_equivocado: é um índice, mas do OUTRO tipo (ex.: o bucket pede "
        "processos seletivos e a página lista concursos públicos, ou vice-versa).\n"
        "- nao_e_indice: edital único, notícia, PDF ou página vazia.\n"
        "- licitacao_ou_cultural: licitação/pregão ou concurso cultural (soberana/rainha).\n\n"
        "IMPORTANTE: classifique pelo CONTEÚDO real, não pelo título. "
        "Um 'Processo Seletivo Público' cujos itens são do tipo Concurso Público "
        "conta como concurso, não como PSS."
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            # Generous budget: gemini-2.5-flash spends tokens on "thinking" and
            # truncates the JSON if the cap is too low.
            "temperature": 0.0, "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }
    try:
        data = gemini_post(session, model, payload, timeout=timeout)
        out = "\n".join(
            p.get("text", "") for p in data["candidates"][0]["content"]["parts"]
        )
        obj = _parse_json_object(out)
        if obj is None:
            return "erro", f"json invalido: {out[:80]}"
        v = str(obj.get("veredicto", "erro")).strip().lower()
        if v not in {"valido_indice", "tipo_equivocado", "nao_e_indice",
                     "licitacao_ou_cultural"}:
            v = "erro"
        return v, str(obj.get("motivo", ""))[:160]
    except Exception as e:
        return "erro", f"gemini: {str(e)[:120]}"


def _deterministic(bucket: str, url: str, title: str, text: str,
                   js_like: bool) -> tuple[str, list[str]]:
    """Structural checks on already-fetched (static or rendered) content."""
    blob = norm(title + " " + text[:4000])
    kws = [norm(k) for k in BUCKET_KEYWORDS[bucket]]
    has_kw = any(k in blob for k in kws)

    hard: list[str] = []
    soft: list[str] = []

    low_url = url.lower()
    if any(h in low_url for h in DETAIL_PATH_HINTS):
        hard.append("ruta_de_detalle")

    if not has_kw:
        (soft if js_like else hard).append(
            "sin_keyword_posible_js" if js_like else "sin_keyword_del_bucket")

    if not has_kw and any(rk in blob for rk in REJECT_KEYWORDS):
        soft.append("posible_licitacao_o_cultural")

    n_items = distinct_listing_items(text)
    if js_like:
        soft.append("render_js_verificar_manual")
    elif n_items < 2:
        soft.append(f"pocos_items_listado({n_items})_posible_detalle_o_vacio")

    flags = hard + soft
    if not flags:
        return "ok", []
    return ("hard" if hard else "soft"), flags


def audit_one(session, bucket: str, url: str, timeout: int,
              municipio: str = "", do_render: bool = False,
              model: str = "", ai_mode: str = "off") -> tuple[str, list[str]]:
    """Audit one confirmed bucket URL.

    do_render: render JS/blocked pages in a browser before the structural check.
    ai_mode:  "off" | "flagged" (ask Gemini only when structural check is not ok)
              | "all" (ask Gemini on every page — catches semantic-type FPs too).
    Returns (severity, flags). "ok" | "soft" | "hard".
    """
    if is_pdf_or_file(url):
        return "hard", ["es_pdf_o_archivo"]

    page = fetch_page(session, url, timeout)
    title, text = page.title, page.text
    reachable = page.ok and not getattr(page, "is_antibot", False)
    js_like = (getattr(page, "is_spa", False) or getattr(page, "is_antibot", False)
               or len(page.text) < 800 or not page.ok)
    rendered = False

    # Render when the static fetch is weak/blocked and rendering is enabled.
    if do_render and (js_like or not reachable):
        r = render_page(url, timeout)
        if r and (r[1] or "").strip():
            title, text = r
            rendered = True
            js_like = False  # we now have the real content

    # If still unreachable and no render, fall back to status-based verdict.
    if not reachable and not rendered:
        status = page.status or 0
        if 500 <= status <= 599:
            return "soft", [f"servidor_5xx_reintentar({status})"]
        if getattr(page, "is_antibot", False):
            return "soft", ["bloqueo_antibot_no_verificable"]
        return "hard", [f"inalcanzable_status_{status or 'err'}"]

    det_sev, det_flags = _deterministic(bucket, url, title, text, js_like)

    # AI adjudication (discrete decision) — the only automatable way to catch a
    # valid-looking listing of the WRONG legal type (semantic ambiguity).
    want_ai = ai_mode == "all" or (ai_mode == "flagged" and det_sev != "ok")
    if want_ai and model:
        v, motivo = ai_verdict(session, model, municipio, bucket, title, text, timeout)
        tag = "rendered" if rendered else "static"
        if v == "valido_indice":
            return "ok", [f"ai_ok({tag})"]
        if v == "erro":
            # AI failed — keep the structural result.
            return det_sev, det_flags + [f"ai_erro: {motivo}"]
        return "hard", [f"ai_{v}({tag}): {motivo}"]

    if rendered and det_sev != "ok":
        det_flags = [f"{f}(rendered)" for f in det_flags]
    return det_sev, det_flags


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic fase 2 index auditor")
    ap.add_argument("--input", type=Path, required=True,
                    help="Cascade output CSV to audit")
    ap.add_argument("--output", type=Path, default=None,
                    help="Where to write the suspects CSV (default: <input>_auditoria.csv)")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--include-revisar", action="store_true",
                    help="Also audit 'revisar' rows (default: only 'confirmado')")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--detalle", action="store_true",
                    help="Print every suspect as it is found")
    ap.add_argument("--render", action="store_true",
                    help="Render JS/blocked pages in a real browser before checking "
                         "(needs Playwright; verifies SPA portals like atende.net)")
    ap.add_argument("--ai", action="store_true",
                    help="Ask Gemini for a discrete verdict on pages the structural "
                         "check flags (cheap; cleans the SOFT/HARD pile)")
    ap.add_argument("--ai-all", action="store_true",
                    help="Ask Gemini on EVERY confirmed URL — catches semantic "
                         "wrong-type FPs hidden among the OK ones (more Gemini cost)")
    ap.add_argument("--model", type=str,
                    default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    args = ap.parse_args()

    ai_mode = "all" if args.ai_all else ("flagged" if args.ai else "off")
    if ai_mode != "off" and not gemini_api_key():
        print("WARNING: --ai pedido pero falta GEMINI_API_KEY; sigo sin IA.", flush=True)
        ai_mode = "off"

    rows = list(csv.DictReader(args.input.open(encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]
    levels = {"confirmado"} | ({"revisar"} if args.include_revisar else set())

    session = make_session()
    suspects: list[dict] = []
    n_audited = 0
    sev_count = {"ok": 0, "soft": 0, "hard": 0}

    buckets = [
        ("concursos", "url_concursos", "confianza_concursos"),
        ("processos", "url_processos_seletivos", "confianza_processos"),
    ]

    for i, r in enumerate(rows, 1):
        muni = r.get("municipio", "")
        for bucket, url_key, conf_key in buckets:
            url = (r.get(url_key) or "").strip()
            conf = (r.get(conf_key) or "").strip()
            if not url or conf not in levels:
                continue
            n_audited += 1
            severity, flags = audit_one(
                session, bucket, url, args.timeout,
                municipio=muni, do_render=args.render,
                model=args.model, ai_mode=ai_mode,
            )
            sev_count[severity] += 1
            if severity != "ok":
                rec = {
                    "municipio": muni, "bucket": bucket, "confianza": conf,
                    "severidad": severity, "url": url, "flags": "; ".join(flags),
                }
                suspects.append(rec)
                if args.detalle:
                    print(f"[{severity.upper():4}] {muni} / {bucket}: "
                          f"{'; '.join(flags)}\n        {url}", flush=True)
        if i % 25 == 0:
            print(f"  ... {i}/{len(rows)} municipios", flush=True)

    out = args.output or args.input.with_name(args.input.stem + "_auditoria.csv")
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "municipio", "bucket", "confianza", "severidad", "url", "flags"])
        w.writeheader()
        w.writerows(suspects)

    mode = []
    if args.render:
        mode.append("render")
    if ai_mode != "off":
        mode.append(f"ai={ai_mode}")
    mode_str = "+".join(mode) if mode else "determinístico"

    print("\n" + "=" * 60, flush=True)
    print(f"Modo: {mode_str}", flush=True)
    print(f"Auditados (URLs confirmadas): {n_audited}", flush=True)
    print(f"  OK:    {sev_count['ok']}", flush=True)
    print(f"  SOFT (verificar manual): {sev_count['soft']}", flush=True)
    print(f"  HARD (probable problema): {sev_count['hard']}", flush=True)
    ok_rate = sev_count["ok"] / n_audited * 100 if n_audited else 0
    print(f"  Tasa OK: {ok_rate:.1f}%", flush=True)
    print(f"\nSospechosos escritos en: {out} ({len(suspects)} filas)", flush=True)
    if ai_mode != "off":
        print("Con --ai, los HARD 'ai_tipo_equivocado'/'ai_nao_e_indice' son FP "
              "semánticos confirmados por Gemini → bajar a 'revisar' en el CSV.",
              flush=True)
    else:
        print("Sin IA: HARD = problema estructural; SOFT incluye páginas JS no "
              "verificables. Usa --render y/o --ai para resolver los SOFT.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
