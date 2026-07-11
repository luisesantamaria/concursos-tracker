#!/usr/bin/env python3
"""Reproduce Fase 2 validation from frozen run497 JSON without network.

The generated CSV contains only golden municipalities represented by at least
one corpus file. Missing municipalities are printed and intentionally excluded
from the evaluator join. Each emitted URL passed the same in-memory
CandidateRecord -> SelectedResource -> FinalDecision chain used by the cascade.

Corpus flip baseline is the per-file ``captured_decision`` field. It is frozen
with the evidence and has a known JSON format; no Git checkout or network is
needed. The script never writes canonical/cumulative datasets.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import types
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as C  # noqa: E402
import verdict_extract as V  # noqa: E402


def municipality_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return "".join(char for char in value.lower() if char.isalnum())


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def replay_decision(fixture: dict, verdict_module=V) -> tuple[str, str]:
    text = fixture.get("text") or ""
    if text.count("\n") < 3:
        return "revisar", "texto sin estructura de lineas (render fallido)"
    decision, evidence = verdict_module.adjudicate(
        text,
        fixture.get("bucket") or "concursos",
        fixture.get("municipio") or "",
        fixture.get("items_llm") or [],
        anchors=fixture.get("anchors") or [],
        title=fixture.get("title") or "",
    )
    return decision, str(evidence.get("motivo") or evidence.get("motivo_code") or "")


def replay_path(path_text: str) -> tuple[str, str | None, str]:
    """Process-safe replay unit; return name, frozen baseline and current state."""
    path = Path(path_text)
    fixture = load_json(path)
    current, _reason = replay_decision(fixture)
    return path.name, fixture.get("captured_decision"), current


def load_reference_verdict(git_ref: str):
    """Execute the exact reference module in memory; do not alter the worktree."""
    spec = f"{git_ref}:scripts/eval/verdict_extract.py"
    source = subprocess.check_output(
        ["git", "show", spec], cwd=ROOT, text=True,
    )
    module_name = f"_verdict_extract_reference_{git_ref.replace('-', '_')}"
    module = types.ModuleType(module_name)
    module.__file__ = spec
    sys.modules[module_name] = module
    exec(compile(source, spec, "exec"), module.__dict__)
    return module


def record_from_fixture(fixture: dict) -> C.CandidateRecord:
    url = fixture.get("url") or ""
    text = fixture.get("text") or ""
    title = fixture.get("title") or ""
    links = tuple(
        (str(anchor.get("href") or ""), str(anchor.get("text") or ""))
        for anchor in (fixture.get("anchors") or [])
        if isinstance(anchor, dict)
    )
    has_evidence = bool(text.strip())
    snapshot = C.EvidenceSnapshot(
        html=(f"<html><head><title>{title}</title></head><body>{text}</body></html>"
              if has_evidence else ""),
        text=text,
        title=title,
        requested_url=url,
        final_url=url,
        status=200 if has_evidence else None,
        source="offline_corpus",
        evidence_state="renderizada" if has_evidence else "error_fetch",
        links=links,
    )
    return C.build_candidate_record(
        requested_url=url,
        source="offline_corpus",
        tier="offline_run497",
        municipio=fixture.get("municipio") or "",
        bucket_hint=fixture.get("bucket") or "",
        evidence=snapshot,
        provenance=fixture.get("provenance") or (),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate partial golden CSV and replay frozen run497 corpus",
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--baseline-ref", default="")
    args = parser.parse_args()

    paths = sorted(args.corpus.glob("*.json"))
    shard_count = max(1, args.shard_count)
    if not 0 <= args.shard_index < shard_count:
        parser.error("--shard-index must be in [0, --shard-count)")
    replay_paths = paths[args.shard_index::shard_count]
    fixtures = [(path, load_json(path)) for path in paths]
    by_municipality: dict[str, list[dict]] = defaultdict(list)
    for _path, fixture in fixtures:
        by_municipality[municipality_key(fixture.get("municipio") or "")].append(fixture)

    with args.golden.open("r", encoding="utf-8-sig", newline="") as handle:
        golden_rows = list(csv.DictReader(handle))
    covered = [
        row["municipio"] for row in golden_rows
        if by_municipality.get(municipality_key(row.get("municipio") or ""))
    ]
    excluded = [
        row["municipio"] for row in golden_rows
        if not by_municipality.get(municipality_key(row.get("municipio") or ""))
    ]

    flips = Counter()
    no_baseline = 0
    reference = load_reference_verdict(args.baseline_ref) if args.baseline_ref else None
    if reference is not None:
        replayed = []
        for path in replay_paths:
            fixture = load_json(path)
            current, _ = replay_decision(fixture, V)
            baseline, _ = replay_decision(fixture, reference)
            replayed.append((path.name, baseline, current))
    else:
        replayed = [replay_path(str(path)) for path in replay_paths]
    for _name, baseline, current in replayed:
        if not baseline:
            no_baseline += 1
            continue
        if current != baseline:
            flips["all"] += 1
            if baseline == "confirmar" and current != "confirmar":
                flips["negative"] += 1
            elif baseline != "confirmar" and current == "confirmar":
                flips["positive"] += 1

    rows = []
    for municipality in covered:
        row = {field: "" for field in C.OUTPUT_FIELDS}
        row.update({"uf": C.UF_SIGLA, "municipio": municipality})
        reasons = []
        municipality_fixtures = by_municipality[municipality_key(municipality)]
        for fixture in municipality_fixtures:
            record = record_from_fixture(fixture)
            short_bucket = fixture.get("bucket") or ""
            canonical = C._canonical_bucket(short_bucket)
            if canonical not in {"concurso_publico", "processo_seletivo"}:
                reasons.append(f"{short_bucket}: bucket desconocido")
                continue
            final = C.derive_final_decision(C.SelectedResource(
                canonical, record, "seleccion unica de fixture offline",
            ))
            reasons.append(f"{short_bucket}:{record.candidate_id}:{final.reason}")
            if short_bucket == "concursos":
                row["url_concursos"] = final.url
                row["confianza_concursos"] = final.status
                row["tier_concursos"] = record.tier
            elif short_bucket == "processos":
                row["url_processos_seletivos"] = final.url
                row["confianza_processos"] = final.status
                row["tier_processos"] = record.tier
            if not row["site_base"] and record.final_url:
                parsed = urlparse(record.final_url)
                if parsed.scheme and parsed.netloc:
                    row["site_base"] = f"{parsed.scheme}://{parsed.netloc}"
        row["method"] = "offline_run497"
        row["razao"] = " | ".join(reasons)
        row["notes"] = "partial_offline_coverage"
        rows.append(row)

    args.pipeline.parent.mkdir(parents=True, exist_ok=True)
    with args.pipeline.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=C.OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"corpus_files={len(fixtures)} replayed_files={len(replayed)} "
          f"shard={args.shard_index}/{shard_count}")
    print(
        f"baseline=git:{args.baseline_ref}:scripts/eval/verdict_extract.py"
        if args.baseline_ref else
        "baseline=captured_decision (frozen per JSON)"
    )
    print(
        f"flips={flips['all']} negative_flips={flips['negative']} "
        f"positive_flips={flips['positive']} without_baseline={no_baseline}"
    )
    print(f"golden_coverage={len(covered)}/{len(golden_rows)}")
    print("covered=" + ", ".join(covered))
    print("excluded_no_fixture=" + ", ".join(excluded))
    print(f"pipeline={args.pipeline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
