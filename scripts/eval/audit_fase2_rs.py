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
import re
import sys
from pathlib import Path

# Reuse the cascade's fetch (with curl_cffi/browser fallback) and signals, so
# the auditor sees pages exactly as the pipeline did.
_FASE2 = Path(__file__).resolve().parents[1] / "fase2_municipios"
sys.path.insert(0, str(_FASE2))
import json  # noqa: E402

from cascade_municipios import (  # noqa: E402
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


WALL_MARKERS = [
    "usa cookies", "utiliza cookies", "aceitar cookies", "aceite os cookies",
    "faca seu login", "faça seu login", "habilite o javascript",
    "enable javascript", "ative o javascript", "ativar o javascript",
]

# Antibot challenge pages (Cloudflare "Just a moment", DDoS-Guard, etc.). They
# resolve with HTTP 200 and render a placeholder, so the real content is hidden
# and an AI verdict on them is meaningless. Treat as SOFT (not verifiable), never
# HARD — a false HARD here could wrongly discard a valid URL.
ANTIBOT_MARKERS = [
    "um momento", "just a moment", "verificacao de seguranca",
    "verificando seguranca", "checking your browser", "cf-browser-verification",
    "attention required", "enable javascript and cookies to continue",
    "ddos-guard", "needs to review the security",
]


def _is_antibot_challenge(title: str, text: str) -> bool:
    """A Cloudflare/DDoS-Guard interstitial that hides the real page."""
    blob = norm((title or "") + " " + (text or "")[:600])
    return any(norm(m) in blob for m in ANTIBOT_MARKERS)


def distinct_listing_items(text: str) -> int:
    """How many distinct edital-like items the page text exposes."""
    return len({m.group(0).lower() for m in LISTING_RE.finditer(text or "")})


def _looks_like_wall(text: str) -> bool:
    """A short cookie/login/JS stub that hides the real page (not verifiable)."""
    if len(text or "") > 2500 or distinct_listing_items(text) >= 2:
        return False
    blob = norm(text)
    return any(norm(m) in blob for m in WALL_MARKERS)


# Generic "show the whole listing" script. A municipal listing's items for the
# target bucket may sit on later pages/years; the auditor must judge ALL of them,
# not just the first (most recent) page — otherwise a page whose recent entries
# are all one type gets a false `tipo_equivocado`. This expands two generic,
# portal-agnostic controls only: a "show N per page" length selector (DataTables)
# and a "year = all" / "all categories" selector. It does NOT touch type/modalidade
# filters (changing those would hide the very items we need to see).
_EXPAND_LISTING_JS = """
() => {
  try {
    // 0) Clear/show-all controls by TEXT (generic, portal-agnostic — not a CMS
    // hardcode). São Marcos had a "LIMPAR FILTROS" anchor: the default view was a
    // filtered subset (only the most recent certame), and clearing it revealed the
    // full listing (4 concursos instead of 1). Match by visible text only.
    const clearRe = /limpar\\s+filtros?|ver\\s+todos|mostrar\\s+todos|exibir\\s+todos|todos\\s+os\\s+anos|ver\\s+mais|carregar\\s+mais|show\\s+all|clear\\s+filters/i;
    document.querySelectorAll('a,button').forEach(el => {
      const t = (el.innerText || el.textContent || '').trim();
      if (t && t.length < 40 && clearRe.test(t)) {
        try { el.click(); } catch (e) {}
      }
    });
  } catch (e) {}
  try {
    document.querySelectorAll('select').forEach(sel => {
      const opts = Array.from(sel.options || []);
      if (!opts.length) return;
      // 1) An explicit "all" option (year=TODOS, "todas categorias", length=todos).
      let target = opts.find(o => /^\\s*(todos|todas|all)\\b/i.test(o.textContent || ''));
      // 2) Otherwise the largest numeric page-length option (>=100), e.g. DataTables.
      if (!target) {
        const nums = opts.filter(o => /^\\d+$/.test((o.value || '').trim())
                                      && parseInt(o.value, 10) >= 100);
        if (nums.length) {
          target = nums.reduce((a, b) =>
            parseInt(a.value, 10) >= parseInt(b.value, 10) ? a : b);
        }
      }
      if (target && sel.value !== target.value) {
        sel.value = target.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  } catch (e) {}
  try {
    // 3) Acordeones colapsados y <details> cerrados esconden documentos internos de
    // innerText aunque el título de la fila (lo que el conteo de certames necesita)
    // suele quedar visible sin expandir — pero expandir no puede perder informacion,
    // solo agregarla. Generico: aria-expanded="false" y details:not([open]).
    document.querySelectorAll('[aria-expanded="false"]').forEach(el => {
      try { el.click(); } catch (e) {}
    });
    document.querySelectorAll('details:not([open])').forEach(el => {
      try { el.open = true; } catch (e) {}
    });
  } catch (e) {}
}
"""


# Recolecta los anchors (href + texto) de la página ya renderizada. Sirve para la
# TOPOLOGÍA DE LINKS del adjudicador: un índice real enlaza a páginas de certames
# DISTINTOS (/concurso/id/200, /id/187...) o a PDFs en carpetas distintas; el detalle
# de UN solo certame enlaza a documentos del mismo certame (mismo dir/mismo id base).
# Contar targets-de-certame distintos discrimina São Marcos (arquetipo B') de forma
# determinista, sin depender de que el texto lo diga. Cap a 300 para no explotar.
_COLLECT_ANCHORS_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!href || href.startsWith('javascript') || href.startsWith('mailto')) continue;
    const key = href + '|' + (a.innerText || '').trim().slice(0, 60);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({href: href, text: (a.innerText || '').trim().slice(0, 120)});
    if (out.length >= 300) break;
  }
  return out;
}
"""

_YEAR_CONTROLS_JS = """
() => {
  const yearRe = /^(19|20)\\d{2}$/;
  const hasAll = (txt) => /\\b(todos|todas|all)\\b/i.test(txt || '');
  const selects = Array.from(document.querySelectorAll('select'));
  for (let i = 0; i < selects.length; i++) {
    const sel = selects[i];
    const opts = Array.from(sel.options || []);
    if (opts.some(o => hasAll(o.textContent || o.value || ''))) continue;
    const years = opts
      .map(o => ({
        year: (o.textContent || '').trim(),
        value: o.value,
      }))
      .filter(o => yearRe.test(o.year));
    const distinct = Array.from(new Set(years.map(o => o.year)));
    if (distinct.length >= 2) {
      const bestYear = distinct.sort((a, b) => parseInt(b) - parseInt(a))[0];
      const best = years.find(o => o.year === bestYear);
      return {select: {index: i, year: bestYear, value: best.value}};
    }
  }
  const anchors = Array.from(document.querySelectorAll('a[href]'))
    .map(a => ({text: (a.innerText || a.textContent || '').trim(), href: a.href || ''}))
    .filter(a => yearRe.test(a.text));
  const byYear = new Map();
  for (const a of anchors) {
    if (!byYear.has(a.text)) byYear.set(a.text, a.href);
  }
  if (byYear.size >= 2) {
    const years = Array.from(byYear.keys()).sort((a, b) => parseInt(b) - parseInt(a));
    return {anchor: {year: years[0], href: byYear.get(years[0])}};
  }
  return {};
}
"""

_APPLY_YEAR_SELECT_JS = """
(info) => {
  const selects = Array.from(document.querySelectorAll('select'));
  const sel = selects[info.index];
  if (!sel) return false;
  sel.value = info.value;
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  const form = sel.form;
  if (form && (form.method || '').toLowerCase() === 'get') {
    if (form.requestSubmit) form.requestSubmit();
    else form.submit();
  }
  return true;
}
"""


def _settle_and_read(page) -> tuple[str, str]:
    """Read visible text after pending client-side rendering settles."""
    title = page.title() or ""
    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    prev = -1
    for _ in range(5):
        if len(text) > 700 and (len(text) == prev or distinct_listing_items(text) >= 2):
            break
        prev = len(text)
        page.wait_for_timeout(1200)
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        title = page.title() or title
    return title, text


def _apply_year_fallback(page, timeout: int) -> str:
    """If the default view is empty, try the most recent explicit year filter."""
    try:
        controls = page.evaluate(_YEAR_CONTROLS_JS) or {}
    except Exception:
        return ""
    select = controls.get("select") if isinstance(controls, dict) else None
    if select:
        try:
            if page.evaluate(_APPLY_YEAR_SELECT_JS, select):
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout * 1000, 8000))
                except Exception:
                    page.wait_for_timeout(2500)
                return str(select.get("year") or "")
        except Exception:
            return ""
    anchor = controls.get("anchor") if isinstance(controls, dict) else None
    if anchor and anchor.get("href"):
        try:
            page.goto(anchor["href"], wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                page.wait_for_timeout(2500)
            return str(anchor.get("year") or "")
        except Exception:
            return ""
    return ""


def _best_frame_text(page) -> str:
    """Return the non-main frame text with the most listing items, if any."""
    best = ""
    best_items = 0
    main = page.main_frame
    for frame in page.frames:
        if frame == main:
            continue
        try:
            text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            continue
        n_items = distinct_listing_items(text)
        if n_items > best_items:
            best = text
            best_items = n_items
    return best if best_items >= 1 else ""


def render_page(url: str, timeout: int = 25) -> tuple[str, str, list] | None:
    """Open the URL in a real browser and return (title, visible_text, anchors).

    For JS-rendered municipal portals (atende.net, oxy.elotech, etc.) whose menu
    and listing only exist after client-side rendering. Before reading the text it
    expands client-side pagination (see ``_EXPAND_LISTING_JS``) so the auditor
    sees the whole listing, not just the first page. ``anchors`` is a list of
    ``{"href", "text"}`` para la topología de links del adjudicador. Returns None
    if the browser is unavailable or the load fails.
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
        # Esperar a que las llamadas XHR/fetch del portal (atende, Next.js, etc.)
        # terminen: ese es el motivo nº1 de que el texto varíe entre corridas (a
        # veces la lista alcanzó a pintar, a veces no). networkidle estabiliza la
        # ENTRADA -> el veredicto deja de parpadear por input cambiante.
        try:
            page.wait_for_load_state("networkidle", timeout=9000)
        except Exception:
            page.wait_for_timeout(2500)
        # Cloudflare/DDoS-Guard interstitial ("Just a moment"): the JS challenge
        # usually clears itself in a few seconds and navigates to the real page.
        for _ in range(6):
            if not _is_antibot_challenge(page.title() or "", ""):
                break
            page.wait_for_timeout(2000)
        # Expand pagination so later-page/older items become visible.
        try:
            page.evaluate(_EXPAND_LISTING_JS)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        # Lazy-load por scroll: muchos CMS municipais (govbr/IPM "/site/...",
        # "/editais-licitacoes/...") cargan el LISTADO de editais recién al hacer
        # scroll; sin esto el render captura solo el menú y la IA juzga el menú, no
        # los items (Pareci Novo rendía el menú -> intermitente; con scroll aparecen
        # los 50 PSS con fecha). Es la causa nº1 de que el listado falte. Scroll
        # progresivo dispara la carga; luego volvemos arriba para leer todo.
        try:
            for _ in range(4):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        # Poll hasta que el texto visible SE ESTABILICE (mismo largo en 2 lecturas
        # consecutivas) o aparezcan ítems de listado — así capturamos la página ya
        # cargada, no un estado intermedio. Determinismo de la entrada.
        title, text = _settle_and_read(page)
        year_used = ""
        if distinct_listing_items(text) == 0:
            year_used = _apply_year_fallback(page, timeout)
            if year_used:
                title, text = _settle_and_read(page)
        frame_used = False
        if distinct_listing_items(text) == 0:
            frame_text = _best_frame_text(page)
            if frame_text:
                text = (text or "") + "\n" + frame_text
                frame_used = True
        try:
            anchors = page.evaluate(_COLLECT_ANCHORS_JS) or []
        except Exception:
            anchors = []
        if year_used:
            anchors.append({
                "href": "render-meta:year_fallback",
                "text": f"year_fallback={year_used}",
            })
        if frame_used:
            anchors.append({
                "href": "render-meta:frame_fallback",
                "text": "frame_fallback=1",
            })
        return title, text, anchors
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
        # Wider window than the original 6000: expanded pagination produces longer
        # listings, and the target bucket's items may sit past the first rows.
        f"TEXTO: {text[:9000]}\n\n"
        "A página funciona como ÍNDICE/LISTAGEM do bucket alvo? Responda com UMA "
        "decisão discreta (JSON):\n"
        '{"veredicto": "valido_indice" | "tipo_equivocado" | "nao_e_indice" '
        '| "licitacao_ou_cultural", "motivo": "frase curta"}\n\n'
        "Definições:\n"
        "- valido_indice: lista MÚLTIPLOS editais/processos do tipo do bucket alvo. "
        "INCLUI páginas COMBINADAS que listam AMBOS os tipos (concursos E processos "
        "seletivos) — uma página combinada é valido_indice para QUALQUER bucket.\n"
        "- tipo_equivocado: é um índice de UM tipo só, e é o OUTRO (ex.: o bucket pede "
        "processos seletivos e a página lista SOMENTE concursos públicos, ou vice-versa). "
        "NÃO use este veredicto se a página lista os dois tipos.\n"
        "- nao_e_indice: edital único, notícia, PDF ou página vazia/sem itens.\n"
        "- licitacao_ou_cultural: licitação/pregão ou concurso cultural (soberana/rainha).\n\n"
        "IMPORTANTE: classifique pelo CONTEÚDO real, não pelo título. "
        "Um 'Processo Seletivo Público' cujos itens são do tipo Concurso Público "
        "conta como concurso, não como PSS. Na dúvida entre valido e tipo_equivocado "
        "quando há itens do bucket alvo presentes, escolha valido_indice."
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
            # gemini-2.5-flash spends tokens "thinking" and can truncate the JSON
            # mid-string (the long `motivo`), leaving valid output unparseable. The
            # verdict word itself appears early — recover it by regex so a real
            # `valido_indice` is not lost to a parse error (was demoting valid
            # indexes to revisar: Almirante Tamandaré).
            m = re.search(
                r'veredicto"?\s*:\s*"?(valido_indice|tipo_equivocado|nao_e_indice|licitacao_ou_cultural)',
                out, re.I)
            if m:
                return m.group(1).lower(), "veredicto recuperado de json truncado"
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

    # Is the STATIC page already a rich index (keyword + several items)? If so we
    # trust it and skip rendering — rendering some portals returns a degraded
    # "baixe a versão atualizada" view that is worse than the static content.
    kws0 = [norm(k) for k in BUCKET_KEYWORDS[bucket]]

    def _is_rich(t: str, x: str) -> bool:
        blob0 = norm((t or "") + " " + (x or "")[:4000])
        return any(k in blob0 for k in kws0) and distinct_listing_items(x) >= 2

    static_rich = reachable and _is_rich(title, text)
    rendered = False

    # Render when the static page is weak: blocked/unreachable, or not already a
    # rich index (covers JS portals like atende.net that serve a short shell).
    if do_render and (not reachable or not static_rich):
        r = render_page(url, timeout)
        # Keep the richer of the two — never let a degraded render replace good
        # static content.
        if r and len(r[1] or "") > len(text):
            title, text = r[0], r[1]  # r[2] = anchors
            rendered = True

    js_like = (not rendered) and (
        getattr(page, "is_spa", False) or getattr(page, "is_antibot", False)
        or len(page.text) < 800 or not page.ok)

    # If still unreachable and no render, fall back to status-based verdict.
    if not reachable and not rendered:
        status = page.status or 0
        if 500 <= status <= 599:
            return "soft", [f"servidor_5xx_reintentar({status})"]
        if getattr(page, "is_antibot", False):
            return "soft", ["bloqueo_antibot_no_verificable"]
        return "hard", [f"inalcanzable_status_{status or 'err'}"]

    # Antibot challenge that did not clear (Cloudflare/DDoS-Guard): real content
    # is hidden → not verifiable. SOFT, never HARD (a false HARD could discard a
    # valid URL). Comes before the deterministic/AI checks for the same reason.
    if _is_antibot_challenge(title, text):
        return "soft", ["desafio_antibot_no_verificable"]

    # Cookie / login wall: a short stub that hides the real page → not verifiable.
    if _looks_like_wall(text):
        return "soft", ["muro_cookies_login_no_verificable"]

    det_sev, det_flags = _deterministic(bucket, url, title, text, js_like)

    # We only trust an AI "bad" verdict as HARD when we are confident we judged
    # the REAL content: either the page rendered, or the static page was already a
    # rich index. Otherwise (thin nav/menu of a JS portal we could not fully load)
    # a bad verdict is downgraded to SOFT — likely an artefact, not a true FP.
    confident = rendered or static_rich

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
        # Anti-pruning guard: a page with >=2 distinct edital numbers (NN/AAAA) IS
        # a listing. Hard structural evidence overrides the model misreading it as
        # a single detail/empty page (only for nao_e_indice; type errors stand).
        if v == "nao_e_indice" and distinct_listing_items(text) >= 2:
            return "ok", [f"ai_nao_e_indice_anulado_por_listado({tag})"]
        sev = "hard" if confident else "soft"
        return sev, [f"ai_{v}({tag}{'' if confident else ',low_conf'}): {motivo}"]

    if rendered and det_sev != "ok":
        det_flags = [f"{f}(rendered)" for f in det_flags]
    return det_sev, det_flags


PROGRESS_COLS = ["municipio", "bucket", "severidad", "flags"]


def _load_progress(path: Path) -> dict[tuple[str, str], dict]:
    """Load prior progress file into a dict keyed by (municipio, bucket)."""
    done: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return done
    for r in csv.DictReader(path.open(encoding="utf-8")):
        key = (r.get("municipio", ""), r.get("bucket", ""))
        done[key] = r
    return done


def _flush_progress(path: Path, records: list[dict]) -> None:
    """Write all progress records to disk (atomic overwrite)."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PROGRESS_COLS)
        w.writeheader()
        w.writerows(records)
    tmp.replace(path)


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
    ap.add_argument("--fresh", action="store_true",
                    help="Ignore existing progress and start from scratch")
    args = ap.parse_args()

    ai_mode = "all" if args.ai_all else ("flagged" if args.ai else "off")
    if ai_mode != "off" and not gemini_api_key():
        print("WARNING: --ai pedido pero falta GEMINI_API_KEY; sigo sin IA.", flush=True)
        ai_mode = "off"

    rows = list(csv.DictReader(args.input.open(encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]
    levels = {"confirmado"} | ({"revisar"} if args.include_revisar else set())

    progress_path = args.input.with_name(args.input.stem + "_audit_progress.csv")
    if args.fresh and progress_path.exists():
        progress_path.unlink()
        print("--fresh: progreso anterior descartado.", flush=True)

    prior = _load_progress(progress_path)
    if prior:
        print(f"Progreso previo: {len(prior)} URLs ya auditadas — retomando.",
              flush=True)

    progress_records: list[dict] = list(prior.values())

    session = make_session()
    n_audited = 0
    n_skipped = 0
    sev_count = {"ok": 0, "soft": 0, "hard": 0}
    pending_flush = 0

    buckets = [
        ("concursos", "url_concursos", "confianza_concursos"),
        ("processos", "url_processos_seletivos", "confianza_processos"),
    ]

    for sev in sev_count:
        sev_count[sev] = sum(1 for r in prior.values() if r.get("severidad") == sev)
    n_audited = len(prior)

    for i, r in enumerate(rows, 1):
        muni = r.get("municipio", "")
        did_work = False
        for bucket, url_key, conf_key in buckets:
            url = (r.get(url_key) or "").strip()
            conf = (r.get(conf_key) or "").strip()
            if not url or conf not in levels:
                continue
            if (muni, bucket) in prior:
                n_skipped += 1
                continue
            n_audited += 1
            did_work = True
            severity, flags = audit_one(
                session, bucket, url, args.timeout,
                municipio=muni, do_render=args.render,
                model=args.model, ai_mode=ai_mode,
            )
            sev_count[severity] += 1
            rec = {
                "municipio": muni, "bucket": bucket,
                "severidad": severity, "flags": "; ".join(flags),
            }
            progress_records.append(rec)
            prior[(muni, bucket)] = rec
            if severity != "ok" and args.detalle:
                print(f"[{severity.upper():4}] {muni} / {bucket}: "
                      f"{'; '.join(flags)}\n        {url}", flush=True)
        if did_work:
            pending_flush += 1
        if pending_flush >= 10:
            _flush_progress(progress_path, progress_records)
            pending_flush = 0
        if i % 25 == 0:
            print(f"  ... {i}/{len(rows)} municipios  "
                  f"(auditados {n_audited}, ok {sev_count['ok']}, "
                  f"soft {sev_count['soft']}, hard {sev_count['hard']})",
                  flush=True)

    _flush_progress(progress_path, progress_records)

    suspects: list[dict] = []
    url_lookup: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        muni = r.get("municipio", "")
        for bucket, url_key, conf_key in buckets:
            url = (r.get(url_key) or "").strip()
            conf = (r.get(conf_key) or "").strip()
            if url and conf in levels:
                url_lookup[(muni, bucket)] = (url, conf)

    for rec in progress_records:
        if rec.get("severidad") not in ("soft", "hard"):
            continue
        key = (rec["municipio"], rec["bucket"])
        url, conf = url_lookup.get(key, ("", ""))
        suspects.append({
            "municipio": rec["municipio"], "bucket": rec["bucket"],
            "confianza": conf, "severidad": rec["severidad"],
            "url": url, "flags": rec.get("flags", ""),
        })

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
    if n_skipped:
        print(f"  Skipped (ya en progreso): {n_skipped}", flush=True)
    print(f"  OK:    {sev_count['ok']}", flush=True)
    print(f"  SOFT (verificar manual): {sev_count['soft']}", flush=True)
    print(f"  HARD (probable problema): {sev_count['hard']}", flush=True)
    ok_rate = sev_count["ok"] / n_audited * 100 if n_audited else 0
    print(f"  Tasa OK: {ok_rate:.1f}%", flush=True)
    print(f"\nProgreso guardado en: {progress_path} ({len(progress_records)} filas)",
          flush=True)
    print(f"Sospechosos escritos en: {out} ({len(suspects)} filas)", flush=True)
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
