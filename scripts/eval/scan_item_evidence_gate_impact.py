"""Offline non-regression scan for the R-T1 iteracion 2 item-evidence gate.

Recorre las unidades CONFIRMADAS de un run de holdout V2 (progress.csv con
``final`` in {indice_oficial, indice_oficial_combinado}), extrae
``stages.A.raw`` de cada observability JSON (mapeado via ``checkpoint.json``,
sin reconstruir el slug hifenado a mano) y lo pasa por
``certifier._certifier_invariants`` (incluye el gate ITEM-POSITIVO: R-T1
iteracion 2, ver agents/certifier.py). Reporta cuales rechazarian HOY y, para
cada rechazo, si el ``evidence_snapshot`` completo de la pagina contiene algun
marcador de item real en OTRA parte que A no cito (candidato a recuperar via
el reintento de reparacion cableado en agents/base.py) o si la pagina esta
genuinamente vacia (posible falso positivo, misma familia que Canela).

Motivo: run_r2_postpalancas/observability/canela--concurso-publico--*.json
confirmo "indice_oficial" citando unicamente la ETIQUETA del filtro
('Concurso ou Processo Seletivo'), verificado como falso positivo real por
Luis en navegador (el municipio publica en otro portal). Ver
docs/RUNBOOK_corridas_locales.md y PLAN_MAESTRO.md para contexto del holdout.

Puramente offline (lee JSON ya generado en disco, cero llamadas a red/API).
No importa ni modifica cascade_municipios.py / verdict_extract.py /
test_contrato_estructural.py / golden_set_v1.csv.

Uso:
    python scripts/eval/scan_item_evidence_gate_impact.py \
        --run-dir staging/fase2_v2/eval/holdout50_20260712/run_r2_postpalancas
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from scripts.fase2_municipios.v2.agents import certifier
from scripts.fase2_municipios.v2.agents.base import AgentOutputRejected

CONFIRMED_DECISIONS = ("indice_oficial", "indice_oficial_combinado")


def load_confirmed_rows(run_dir: Path) -> list[dict]:
    with (run_dir / "progress.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if r["final"] in CONFIRMED_DECISIONS]


def load_observability_index(run_dir: Path) -> dict[tuple[str, str], str]:
    """(municipio, bucket) -> observability_path, sourced from checkpoint.json
    so we never have to reconstruct the hyphenated municipio slug by hand."""
    with (run_dir / "checkpoint.json").open(encoding="utf-8") as fh:
        checkpoint = json.load(fh)
    index: dict[tuple[str, str], str] = {}
    for unit in checkpoint["units"].values():
        index[(unit["municipio"], unit["bucket"])] = unit["result"]["observability_path"]
    return index


def full_page_text(observability: dict) -> str:
    parts = []
    for source in observability.get("evidence_snapshot", {}).get("sources", []):
        for seg in source.get("content_segments", []):
            parts.append(seg.get("text", ""))
    return "\n".join(parts)


def find_item_marker_excerpts(text: str, *, window: int = 60, limit: int = 3) -> list[str]:
    """Short excerpts around genuine item-marker hits elsewhere on the page,
    reusing certifier's own keyword+marker vocabulary. Best-effort signal
    only -- legal-basis citations ("Lei nº X/Y") and "atualizado em"
    timestamps can still produce a decoy hit; read the excerpt before
    trusting it (see the module docstring / mission writeup)."""
    folded = certifier._fold_accents(text).lower()
    hits: list[str] = []
    for m in certifier._ITEM_INSTANCE_MARKER_PATTERN.finditer(folded):
        start, end = max(0, m.start() - window), min(len(text), m.end() + window)
        if certifier._ITEM_KEYWORD_PATTERN.search(folded[start:end]):
            hits.append(text[start:end].replace("\n", " ").strip())
        if len(hits) >= limit:
            return hits
    if hits:
        return hits
    for m in certifier._ITEM_KEYWORD_YEAR_ADJACENT_PATTERN.finditer(folded):
        start, end = max(0, m.start() - window), min(len(text), m.end() + window)
        hits.append(text[start:end].replace("\n", " ").strip())
        if len(hits) >= limit:
            break
    return hits


def scan(run_dir: Path) -> list[dict]:
    confirmed = load_confirmed_rows(run_dir)
    obs_index = load_observability_index(run_dir)
    results = []
    for row in confirmed:
        key = (row["municipio"], row["bucket"])
        obs_path = obs_index.get(key)
        outcome = {
            "municipio": row["municipio"], "bucket": row["bucket"],
            "final": row["final"], "url": row["url"],
            "observability_path": obs_path,
        }
        if obs_path is None:
            outcome.update(rejects=None, reason="no_observability_mapping", excerpts=[])
            results.append(outcome)
            continue
        with (run_dir / obs_path).open(encoding="utf-8") as fh:
            obs = json.load(fh)
        raw = obs["stages"]["A"]["raw"]
        outcome["a_decision"] = raw.get("decision")
        try:
            certifier._certifier_invariants(raw)
        except AgentOutputRejected as exc:
            outcome["rejects"] = True
            outcome["reason"] = exc.reason
            outcome["excerpts"] = find_item_marker_excerpts(full_page_text(obs))
        else:
            outcome["rejects"] = False
            outcome["reason"] = None
            outcome["excerpts"] = []
        results.append(outcome)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", type=Path,
        default=Path("staging/fase2_v2/eval/holdout50_20260712/run_r2_postpalancas"),
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    results = scan(args.run_dir)
    rejected = [r for r in results if r["rejects"]]
    accepted = [r for r in results if r["rejects"] is False]

    print(f"Total confirmadas (final in {CONFIRMED_DECISIONS}): {len(results)}")
    print(f"Aceptadas por el invariante actual: {len(accepted)}")
    print(f"Rechazadas por el invariante actual: {len(rejected)}")
    by_reason: dict[str, list] = {}
    for r in rejected:
        by_reason.setdefault(r["reason"], []).append(r)
    for reason, items in sorted(by_reason.items()):
        print(f"\n=== reason={reason} ({len(items)}) ===")
        for item in items:
            marker = "RECOVERABLE?" if item["excerpts"] else "NO_ITEMS_FOUND"
            print(f"  [{marker}] {item['municipio']}/{item['bucket']}  final={item['final']}  url={item['url']}")
            for excerpt in item["excerpts"]:
                print(f"      hit: ...{excerpt}...")

    if args.json_out:
        args.json_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON completo -> {args.json_out}")


if __name__ == "__main__":
    main()
