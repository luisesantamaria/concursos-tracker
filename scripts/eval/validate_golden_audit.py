"""Validate the fase 2 auditor against the golden set (ground truth).

Two steps, no inline shell/Python needed:

  # 1) Convert the hand-verified golden into pipeline format (confianza=confirmado):
  python scripts/eval/validate_golden_audit.py convert

  # 2) Run the auditor on it (writes <out>_auditoria.csv):
  python scripts/eval/audit_fase2_rs.py \
      --input /tmp/golden_as_pipeline.csv --render --ai-all --detalle

  # 3) Compare the auditor verdict against ground truth:
  python scripts/eval/validate_golden_audit.py compare

A HARD on a golden URL whose requiere_revision_humana == "no" is an AUDITOR error
(it flagged a hand-verified-correct page). If those are ~0, the auditor's OK
verdicts can be trusted without opening each page.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = PROJECT_ROOT / "data" / "golden_set_v1.csv"
PIPELINE_CSV = Path("/tmp/golden_as_pipeline.csv")
AUDIT_CSV = Path("/tmp/golden_as_pipeline_auditoria.csv")

PIPELINE_COLS = [
    "municipio", "url_concursos", "confianza_concursos",
    "url_processos_seletivos", "confianza_processos", "revision_humana",
]


def _real(url: str) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith("http") else ""


def convert(golden: Path, out: Path) -> None:
    rows = []
    for r in csv.DictReader(golden.open(encoding="utf-8-sig")):
        uc, up = _real(r.get("url_concursos")), _real(r.get("url_processos_seletivos"))
        rows.append({
            "municipio": r.get("municipio", ""),
            "url_concursos": uc,
            "confianza_concursos": "confirmado" if uc else "",
            "url_processos_seletivos": up,
            "confianza_processos": "confirmado" if up else "",
            "revision_humana": (r.get("requiere_revision_humana") or "").strip().lower(),
        })
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PIPELINE_COLS)
        w.writeheader()
        w.writerows(rows)
    n_urls = sum(bool(r["url_concursos"]) + bool(r["url_processos_seletivos"]) for r in rows)
    print(f"Golden convertido: {len(rows)} municipios, {n_urls} URLs -> {out}")


def compare(golden: Path, audit: Path) -> None:
    rev = {}
    for r in csv.DictReader(golden.open(encoding="utf-8-sig")):
        rev[r.get("municipio", "")] = (r.get("requiere_revision_humana") or "").strip().lower()

    if not audit.exists():
        print(f"No existe {audit}. Corre primero el auditor sobre {PIPELINE_CSV}.")
        return
    suspects = list(csv.DictReader(audit.open(encoding="utf-8")))
    hard = [s for s in suspects if s.get("severidad") == "hard"]

    # An auditor error = HARD on a golden URL that is NOT flagged for human review.
    errors = [s for s in hard if rev.get(s.get("municipio", ""), "") != "si"]
    expected = [s for s in hard if rev.get(s.get("municipio", ""), "") == "si"]

    print("=" * 60)
    print("VALIDACIÓN DEL AUDITOR CONTRA EL GOLDEN (verdad de campo)")
    print("=" * 60)
    print(f"HARD totales sobre el golden: {len(hard)}")
    print(f"\n  ERRORES del auditor (HARD sobre golden NO-revisión-humana): {len(errors)}")
    for s in errors:
        print(f"    [ERROR] {s['municipio']}/{s['bucket']}: {s['flags'][:90]}")
    print(f"\n  Esperables (HARD sobre golden marcado revisión-humana=si): {len(expected)}")
    for s in expected:
        print(f"    [ok-esperado] {s['municipio']}/{s['bucket']}: {s['flags'][:80]}")

    print("\n" + "-" * 60)
    if not errors:
        print("VEREDICTO: auditor CONFIABLE — 0 falsos HARD sobre URLs verificadas "
              "correctas. Sus 'OK' se pueden confiar sin abrir cada página.")
    else:
        print(f"VEREDICTO: auditor con {len(errors)} falso(s) HARD sobre URLs "
              "correctas. Tráeme esos casos para afinar antes de la corrida de 768.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the auditor against the golden set")
    ap.add_argument("mode", choices=["convert", "compare"])
    ap.add_argument("--golden", type=Path, default=GOLDEN)
    ap.add_argument("--pipeline", type=Path, default=PIPELINE_CSV)
    ap.add_argument("--audit", type=Path, default=AUDIT_CSV)
    args = ap.parse_args()
    if args.mode == "convert":
        convert(args.golden, args.pipeline)
    else:
        compare(args.golden, args.audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
