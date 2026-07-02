#!/usr/bin/env python3
"""Capture a frozen render fixture for verdict_extract regression tests."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import audit_fase2_rs as A  # noqa: E402
import cascade_municipios as C  # noqa: E402
import cierre_dataset as Z  # noqa: E402
import verdict_extract as V  # noqa: E402


def _anchors(raw) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in raw or []:
        if isinstance(a, dict):
            out.append({"href": a.get("href", ""), "text": a.get("text", "")})
        elif isinstance(a, (list, tuple)) and len(a) >= 2:
            out.append({"href": str(a[0] or ""), "text": str(a[1] or "")})
    return out


def capture(args: argparse.Namespace) -> dict:
    session = C.make_session()
    title = ""
    text = ""
    anchors: list[dict[str, str]] = []

    pg = C.fetch_page(session, args.url, args.timeout)
    if pg and pg.ok:
        title = pg.title or ""
        text = pg.text or ""
        anchors = [{"href": h, "text": t} for h, t in (pg.links or [])]

    rendered = A.render_page(args.url, args.timeout)
    if rendered and (
        Z._has_real_listing_item(rendered[1])
        or len((rendered[1] or "").strip()) >= 500
    ):
        title, text, raw_anchors = rendered
        anchors = _anchors(raw_anchors)

    items = [] if args.no_llm else V.extract_items(
        text, session, C.gemini_post, args.model, args.timeout)
    decision, ev = ("revisar", {"motivo": "texto sin estructura de lineas"})
    if (text or "").count("\n") >= 3:
        decision, ev = V.adjudicate(
            text, args.bucket, args.municipio, items, anchors=anchors, title=title)

    expected = args.expected or decision
    return {
        "url": args.url,
        "municipio": args.municipio,
        "bucket": args.bucket,
        "title": title,
        "text": text,
        "anchors": anchors,
        "items_llm": items,
        "label": args.label,
        "expected": expected,
        "captured_decision": decision,
        "captured_evidence": ev,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--municipio", required=True)
    ap.add_argument("--bucket", choices=["concursos", "processos"], required=True)
    ap.add_argument("--label", choices=["tp", "fp", "render_gap"], required=True)
    ap.add_argument("--expected", choices=["confirmar", "revisar"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    fixture = capture(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{args.out}: {fixture['captured_decision']} "
          f"cert={fixture['captured_evidence'].get('n_certames', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
