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
import sys
from pathlib import Path

# Reuse the cascade's fetch (with curl_cffi/browser fallback) and signals, so
# the auditor sees pages exactly as the pipeline did.
_FASE2 = Path(__file__).resolve().parents[1] / "fase2_municipios"
sys.path.insert(0, str(_FASE2))
from cascade_municipios_rs import (  # noqa: E402
    make_session, fetch_page, is_pdf_or_file, norm,
    LISTING_RE, BUCKET_KEYWORDS,
)

REJECT_KEYWORDS = [
    "licitacao", "licitacoes", "pregao", "chamamento publico", "tomada de preco",
    "concorrencia publica", "dispensa de licitacao",
    "soberana", "rainha", "garota", "majestade",  # cultural contests
]

DETAIL_PATH_HINTS = ["/detalhe/", "/noticia/", "/noticias/", "/visualizar/", "/view/"]


def distinct_listing_items(text: str) -> int:
    """How many distinct edital-like items the page text exposes."""
    return len({m.group(0).lower() for m in LISTING_RE.finditer(text or "")})


def audit_url(session, bucket: str, url: str, timeout: int) -> tuple[str, list[str]]:
    """Return (severity, flags) for one confirmed bucket URL.

    severity: "ok" | "soft" (verify manually) | "hard" (likely a real problem).
    """
    if is_pdf_or_file(url):
        return "hard", ["es_pdf_o_archivo"]

    page = fetch_page(session, url, timeout)

    if getattr(page, "is_antibot", False):
        return "soft", ["bloqueo_antibot_no_verificable"]
    if not page.ok:
        status = page.status or 0
        # 5xx is usually a transient server hiccup, not a dead/wrong link.
        if 500 <= status <= 599:
            return "soft", [f"servidor_5xx_reintentar({status})"]
        return "hard", [f"inalcanzable_status_{status or 'err'}"]

    blob = norm(page.title + " " + page.text[:4000])
    kws = [norm(k) for k in BUCKET_KEYWORDS[bucket]]
    has_kw = any(k in blob for k in kws)
    # A JS-rendered page (SPA markers or near-empty static text) carries its real
    # content client-side, so missing keywords/items there is not evidence of a
    # bad page — only a limitation of a no-browser check. Such findings are soft.
    js_like = getattr(page, "is_spa", False) or len(page.text) < 800

    hard: list[str] = []
    soft: list[str] = []

    # Detail / news single-item route — structural, always a real problem.
    low_url = url.lower()
    if any(h in low_url for h in DETAIL_PATH_HINTS):
        hard.append("ruta_de_detalle")

    # Missing bucket keyword: hard only when the static page is substantial and
    # still has no mention; otherwise it is likely JS-rendered → soft.
    if not has_kw:
        if js_like:
            soft.append("sin_keyword_posible_js")
        else:
            hard.append("sin_keyword_del_bucket")

    # Reject content (licitacao / cultural) without a bucket keyword → verify.
    if not has_kw and any(rk in blob for rk in REJECT_KEYWORDS):
        soft.append("posible_licitacao_o_cultural")

    # Index check: a real listing exposes several distinct edital items. Absent
    # on JS pages by design, so soft there.
    n_items = distinct_listing_items(page.text)
    if js_like:
        soft.append("render_js_verificar_manual")
    elif n_items < 2:
        soft.append(f"pocos_items_listado({n_items})_posible_detalle_o_vacio")

    flags = hard + soft
    if not flags:
        return "ok", []
    return ("hard" if hard else "soft"), flags


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
    args = ap.parse_args()

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
            severity, flags = audit_url(session, bucket, url, args.timeout)
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

    print("\n" + "=" * 60, flush=True)
    print(f"Auditados (URLs confirmadas): {n_audited}", flush=True)
    print(f"  OK:    {sev_count['ok']}", flush=True)
    print(f"  SOFT (verificar manual): {sev_count['soft']}", flush=True)
    print(f"  HARD (probable problema): {sev_count['hard']}", flush=True)
    ok_rate = sev_count["ok"] / n_audited * 100 if n_audited else 0
    print(f"  Tasa OK estructural: {ok_rate:.1f}%", flush=True)
    print(f"\nSospechosos escritos en: {out} ({len(suspects)} filas)", flush=True)
    print("Nota: SOFT incluye SPA/antibot (no verificables sin JS) y listados "
          "cortos. HARD = sin keyword del bucket, PDF, ruta de detalle o muerta.",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
