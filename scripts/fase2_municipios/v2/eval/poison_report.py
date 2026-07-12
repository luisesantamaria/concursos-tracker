"""Deterministic F2.P3 gate over a frozen poisoned-fixture checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from scripts.fase2_municipios.v2.eval.live_runtime import canonical_json_bytes, normalize_unit


AFFIRMATIVE = frozenset({
    "indice_oficial", "indice_oficial_combinado", "portal_externo_oficial",
})


def _decision(layer: Mapping[str, Any], name: str) -> str:
    value = layer.get(name)
    return str(value.get("decision", "")) if isinstance(value, Mapping) else ""


def build_poison_report(
    manifest: Mapping[str, Any], checkpoint: Mapping[str, Any], *,
    min_cases: int = 15, max_cases: int = 20,
) -> dict[str, Any]:
    if manifest.get("expectation") != "reject_or_review":
        raise ValueError("manifest_expectation_invalid")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not min_cases <= len(cases) <= max_cases:
        raise ValueError("manifest_case_count_invalid")
    required = set(manifest.get("required_types", ()))
    present = {str(case.get("tipo", "")) for case in cases if isinstance(case, Mapping)}
    if not required or not required.issubset(present):
        raise ValueError("manifest_required_types_missing")
    raw_units = checkpoint.get("units")
    if not isinstance(raw_units, Mapping):
        raise ValueError("checkpoint_units_invalid")
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in raw_units.values():
        if not isinstance(record, Mapping):
            continue
        try:
            key = normalize_unit(str(record["municipio"]), str(record["bucket"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key in indexed:
            raise ValueError("checkpoint_duplicate_unit")
        indexed[key] = record

    rows = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("manifest_case_invalid")
        key = normalize_unit(str(case.get("municipio", "")), str(case.get("bucket", "")))
        record = indexed.get(key)
        if record is None:
            raise ValueError(f"checkpoint_missing_unit:{key[0]}:{key[1]}")
        result = record.get("result")
        outcome = result.get("outcome") if isinstance(result, Mapping) else None
        final = str(outcome.get("decision", "")) if isinstance(outcome, Mapping) else ""
        layer = outcome.get("layer") if isinstance(outcome, Mapping) else None
        if not isinstance(layer, Mapping):
            if final != "revisar":
                raise ValueError(f"checkpoint_incomplete_layer:{key[0]}:{key[1]}")
            layer = {}
        unadjudicated_review = not layer
        a = _decision(layer, "proposal_a")
        b = _decision(layer, "proposal_b")
        judge = layer.get("judge_response")
        c_reason = str(judge.get("reason", "")) if isinstance(judge, Mapping) else ""
        c_invoked = bool(c_reason and c_reason != "not_invoked_consensus")
        fp = final in AFFIRMATIVE
        a_affirmative = a in AFFIRMATIVE
        bc_net = a_affirmative and not fp and (b not in AFFIRMATIVE or c_invoked)
        rows.append({
            "municipio": case["municipio"], "bucket": case["bucket"],
            "tipo": case["tipo"], "A": a, "B": b, "C_reason": c_reason,
            "final": final, "fp": fp, "a_stopped_alone": not a_affirmative,
            "b_objected": a_affirmative and b not in AFFIRMATIVE,
            "c_invoked": c_invoked, "bc_net_capture": bc_net,
            "unadjudicated_review": unadjudicated_review,
        })
    return {
        "schema_version": 1,
        "expectation": "reject_or_review",
        "totals": {
            "units": len(rows),
            "fp": sum(row["fp"] for row in rows),
            "a_stopped_alone": sum(row["a_stopped_alone"] for row in rows),
            "b_objected": sum(row["b_objected"] for row in rows),
            "c_invoked": sum(row["c_invoked"] for row in rows),
            "bc_net_captures": sum(row["bc_net_capture"] for row in rows),
            "unadjudicated_reviews": sum(row["unadjudicated_review"] for row in rows),
        },
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_poison_report(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.checkpoint.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=False)
    args.output.write_bytes(canonical_json_bytes(report))
    print(json.dumps(report["totals"], sort_keys=True))
    return 1 if report["totals"]["fp"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
