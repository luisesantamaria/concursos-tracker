"""Comparacion semantica controlada V1-vs-V2 sobre la MISMA evidencia congelada.

Fase 4 (comparacion 1) de la directiva 12-jul. Problema que resuelve: hoy V1
(run497 historico) y V2 (fetch live) adjudican sobre evidencia DISTINTA -- la
pagina real puede haber cambiado entre una corrida y otra. Sin controlar por
eso, un "flip" en el diferencial de ``golden_runner`` puede significar
"la pagina cambio" en vez de "los dos sistemas adjudican distinto". Este
modulo reconstruye un baseline V1 DE SOLO LECTURA, EX-POST, sobre el texto
exacto que V2 ya congelo en ``checkpoint.json`` durante una corrida de
``run_golden_live`` (p.ej. ``staging/fase2_v2/eval/golden36_fable_20260712_r2``).
Nunca vuelve a pedir la pagina ni llama a un modelo: solo re-adjudica con el
verificador determinista de V1 sobre el mismo string de texto que V2 vio.

IMPORTANTE -- por que este modulo SI importa ``scripts.eval.verdict_extract``
y por que eso NO viola la independencia V1/V2 (directiva 12-jul):
``verdict_extract.evaluate_candidate_contract`` es exactamente el adjudicador
semantico de V1 que el camino DECISOR de V2 tiene prohibido invocar (ver
``scripts/fase2_municipios/v2/agents/tests/test_v1_independence.py``,
``FORBIDDEN_IMPORT_SUBSTRINGS``). Este modulo no es parte de ese camino: no
construye ningun ``CandidateRecord``, no participa en ``ABCOrchestrator``, no
corre durante ``run_golden_live`` y su salida (``semantic_matrix.json/csv``)
nunca se relee para adjudicar nada -- es reporting comparativo que corre
DESPUES de que V2 ya publico y cerro su cassette/differential. Es el mismo rol
que cumple ``scripts/eval/medir_golden_set.py`` frente al pipeline V1: mide,
no decide. Por eso NO se agrega a ``DECISION_MODULES`` de
``test_v1_independence.py`` ni se importa jamas desde
``scripts/fase2_municipios/v2/agents/*`` o desde
``scripts/fase2_municipios/v2/eval/live_abc_adapter.py`` /
``structural_evidence.py`` (los modulos que SI construyen la decision V2 en
caliente). Si alguna vez alguien intenta importar este modulo desde el camino
decisor, el test arquitectonico AST debe seguir fallando -- no lo debilites
agregando ``semantic_comparison`` a ninguna lista de excepciones ahi.

Contrato de lectura/escritura: ``--run-dir`` se abre EXCLUSIVAMENTE en modo
lectura (solo se lee ``checkpoint.json``); este modulo nunca escribe, borra ni
crea nada dentro de ``--run-dir``. Toda salida va a ``--output-dir``
(``semantic_matrix.json`` y ``semantic_matrix.csv``), nunca fuera de el.

Limitacion conocida (documentada, no parcheada en silencio): el checkpoint
persiste el texto renderizado (``layer.sources[0].content``) pero NO el
``title`` del documento ni la lista de anchors del DOM -- ``v1_baseline()``
por lo tanto siempre invoca ``evaluate_candidate_contract`` con
``title=""``/``anchors=[]``. Heuristicas de V1 que dependen de esas dos
senales (deteccion de "listing shell" por anchors, herencia de tipo por
titulo de pagina) pueden divergir levemente de lo que un V1 real hubiera
resuelto con el HTML crudo original. Verificado a mano contra
``staging/fase2_v2/eval/golden36_fable_20260712_r2/checkpoint.json`` (solo
lectura): sobre las 21 unidades con ``layer`` no nulo de esa corrida,
``evaluate_candidate_contract`` corre sin excepciones y produce las ocho
decisiones discretas esperadas (incluyendo un caso real de divergencia
semantica: Novo Hamburgo/concurso_publico, V2=revisar vs baseline
V1=indice_oficial, sobre el mismo texto).

Nota sobre severidad de comparacion (para no comparar peras con manzanas):
``golden_runner.compare_to_golden`` es ESTRICTO -- unicamente ``HIT`` cuenta
como ``match``; ``HOST`` (mismo dominio, distinto path/query) cuenta como
``differ``. Esto es mas duro que el reporte tolerante de
``scripts/eval/medir_golden_set.py``, que expone HIT/HOST/WRNG/MISS por
separado. Los campos ``*_vs_golden`` de este modulo heredan la semantica
estricta de ``golden_runner``, no la tolerante del evaluador legado.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.eval import verdict_extract
from scripts.fase2_municipios.v2.eval import golden_runner
from scripts.fase2_municipios.v2.eval import run_golden_live


class SemanticComparisonError(ValueError):
    """Entrada o forma de checkpoint invalida para la comparacion ex-post."""


# EVIDENCE_FAILURE cuyo `code` cae aqui llego a `revisar` porque el gate de
# citas/consenso determinista no lo confirmo (no por falla de infraestructura
# ni por ausencia legitima). Ver docstring de modulo para el resto de la
# taxonomia.
_CITAS_GATE_CODES = frozenset({"consensus_failed_final_gate", "agreement_review"})
# MODEL_FAILURE/CONFIGURATION_FAILURE/INTERNAL_FAILURE son todas fallas de
# infraestructura del runtime V2 (API, config, bug local), no desacuerdo
# semantico -- se agrupan bajo una sola categoria de reporting.
_INFRA_MODELO_KINDS = frozenset({
    "model_failure", "configuration_failure", "internal_failure",
})
# Mismo conjunto que `golden_runner.run_replay` usa internamente (variable
# local `equivalent_flips`) para filtrar su tabla de adjudicacion: un flip en
# este conjunto es un desacuerdo de FORMA (confirm/confirm con distinto
# `_decision_kind`, o mismo kind con distinto detalle) que en el fondo es el
# mismo veredicto. No se importa desde ahi porque no esta expuesto como
# simbolo publico del modulo.
_EQUIVALENT_FLIPS = frozenset({
    "both_confirm_same_resource", "both_review", "both_negative",
})
DISCREPANCY_CATEGORIES = frozenset({
    "adquisicion", "infra_modelo", "citas_gate", "desacuerdo_abc",
    "ausencia_legitima", "semantico_real", "sin_discrepancia", "no_computable",
})

CSV_FIELDS = (
    "municipio",
    "bucket",
    "golden_expectation",
    "golden_urls",
    "v1_baseline_decision",
    "v1_baseline_url",
    "v1_baseline_vs_golden",
    "v2_decision",
    "v2_url",
    "v2_vs_golden",
    "flip_v1_v2",
    "cause_kind",
    "cause_code",
    "revisar_por",
    "discrepancy_category",
)


def _read_checkpoint(run_dir: Path) -> Mapping[str, Any]:
    """Read-only load of ``<run_dir>/checkpoint.json``. Never writes."""
    path = Path(run_dir) / "checkpoint.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticComparisonError(f"checkpoint_unreadable:{path}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("units"), Mapping):
        raise SemanticComparisonError(f"checkpoint_shape_invalid:{path}")
    return raw


def _units_by_key(checkpoint: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Re-index checkpoint['units'] (JSON-encoded-tuple string keys) by the
    parsed (muni_key, bucket) tuple, matching live_runtime.unit_storage_key
    without needing to reproduce its exact separator formatting."""
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw_key, record in checkpoint["units"].items():
        try:
            parsed = json.loads(raw_key)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(parsed, list)
            and len(parsed) == 2
            and all(isinstance(value, str) for value in parsed)
            and isinstance(record, Mapping)
        ):
            indexed[(parsed[0], parsed[1])] = record
    return indexed


def load_v2_unit(run_dir: Path, municipio: str, bucket: str) -> dict[str, Any] | None:
    """Read-only lookup of one unit's frozen V2 outcome from checkpoint.json.

    Returns ``None`` when the unit never ran in this run (its key is absent
    from the checkpoint) -- distinct from a unit that ran and failed closed
    (e.g. access_failure), which still returns a mapping with an empty
    candidate/content. Tolerant to a null ``layer``/``evidence`` (units with
    access_failure never build one).
    """
    checkpoint = _read_checkpoint(Path(run_dir))
    key = (golden_evaluator.muni_key(municipio), bucket)
    record = _units_by_key(checkpoint).get(key)
    if record is None:
        return None
    result = record.get("result")
    if not isinstance(result, Mapping):
        raise SemanticComparisonError(f"checkpoint_unit_missing_result:{key}")
    outcome = result.get("outcome")
    if not isinstance(outcome, Mapping):
        raise SemanticComparisonError(f"checkpoint_unit_missing_outcome:{key}")

    cause = outcome.get("cause")
    cause = cause if isinstance(cause, Mapping) else {}
    layer = outcome.get("layer")
    layer = layer if isinstance(layer, Mapping) else None

    candidate: dict[str, Any] | None = None
    content: str | None = None
    citations: list[dict[str, Any]] = []
    if layer is not None:
        candidate_raw = layer.get("candidate")
        if isinstance(candidate_raw, Mapping):
            candidate = dict(candidate_raw)
        sources = layer.get("sources")
        if isinstance(sources, list) and sources and isinstance(sources[0], Mapping):
            content_value = sources[0].get("content")
            content = content_value if isinstance(content_value, str) else None
        citations_raw = layer.get("citations")
        if isinstance(citations_raw, list):
            citations = [
                dict(item) for item in citations_raw if isinstance(item, Mapping)
            ]

    decision = outcome.get("decision")
    url = outcome.get("url")
    return {
        "decision": decision if isinstance(decision, str) else "",
        "url": url if isinstance(url, str) else "",
        "cause_kind": cause.get("kind") if isinstance(cause.get("kind"), str) else "",
        "cause_code": cause.get("code") if isinstance(cause.get("code"), str) else "",
        "revisar_por": (
            cause.get("revisar_por") if isinstance(cause.get("revisar_por"), str) else ""
        ),
        "candidate": candidate,
        "content": content,
        "citations": citations,
    }


def v1_baseline(v2: Mapping[str, Any] | None, bucket: str) -> dict[str, Any] | None:
    """Ex-post V1 baseline over the SAME frozen evidence V2 already saw.

    Never re-fetches, never calls a model: this is
    ``verdict_extract.evaluate_candidate_contract`` (V1's deterministic
    adjudicator) applied to the exact ``content`` string V2 froze. Returns
    ``None`` when ``v2`` is ``None`` (unit never ran) or when V2 itself never
    built a candidate/content (e.g. access_failure) -- there is nothing for
    V1 to adjudicate over.

    See the module docstring for the known title/anchors limitation.
    """
    if v2 is None:
        return None
    candidate = v2.get("candidate")
    content = v2.get("content")
    if candidate is None or content is None:
        return None
    bucket_name = "concursos" if bucket == "concurso_publico" else "processos"
    contract = verdict_extract.evaluate_candidate_contract(
        text=content,
        bucket=bucket_name,
        title="",
        anchors=[],
        source_kind=str(candidate.get("source_kind") or "desconocido"),
        authority=str(candidate.get("authority") or "desconocida"),
        identity=str(candidate.get("identity") or "desconocida"),
        evidence_state=str(candidate.get("evidence_state") or "completa"),
        accessible=candidate.get("evidence_state") != "error_fetch",
    )
    url = (
        str(candidate.get("url") or "")
        if contract.decision in verdict_extract.INDEX_STATES
        else ""
    )
    return {"decision": contract.decision, "url": url, "note": contract.note}


def classify_discrepancy(
    *,
    v1: Mapping[str, Any] | None,
    v2: Mapping[str, Any] | None,
    cause_kind: str,
    cause_code: str,
    flip_v1_v2: str | None,
) -> str:
    """Pure eight-way taxonomy for one row. Precedence: missing V2, then the
    OUTCOME's cause (why V2 didn't confirm), then agreement between V1/V2 only
    for the residual cases (cause == success, or an EVIDENCE_FAILURE code
    outside the citas_gate set such as the legacy
    ``v2_affirms_v1_disagrees_pending_audit``, which has no dedicated bucket
    in this taxonomy and falls through to the semantic comparison instead)."""
    if v2 is None:
        return "no_computable"
    if cause_kind == "access_failure":
        return "adquisicion"
    if cause_kind in _INFRA_MODELO_KINDS:
        return "infra_modelo"
    if cause_kind == "evidence_failure" and cause_code in _CITAS_GATE_CODES:
        return "citas_gate"
    if cause_kind == "disagreement_unresolved":
        return "desacuerdo_abc"
    if cause_kind == "legitimate_absence":
        return "ausencia_legitima"
    if v1 is None:
        return "no_computable"
    if flip_v1_v2 in _EQUIVALENT_FLIPS:
        return "sin_discrepancia"
    return "semantico_real"


def _golden_lookup(golden_path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """municipio/bucket -> (golden_main, golden_extra), read-only over the
    golden CSV (mirrors the per-row/per-bucket walk golden_runner.run_replay
    does, without needing a replay corpus)."""
    lookup: dict[tuple[str, str], tuple[str, str]] = {}
    for row in golden_evaluator.read_csv(Path(golden_path)):
        municipio = golden_evaluator.get(row, "municipio")
        if not municipio:
            continue
        for bucket, main_column, extra_column in golden_runner.BUCKET_COLUMNS:
            key = (golden_evaluator.muni_key(municipio), bucket)
            lookup[key] = (
                golden_evaluator.get(row, main_column),
                golden_evaluator.get(row, extra_column),
            )
    return lookup


def build_matrix(*, run_dir: Path, golden_path: Path) -> dict[str, Any]:
    """Build the full semantic comparison matrix for every golden unit.

    Read-only over ``run_dir`` (only ``checkpoint.json`` is opened) and over
    ``golden_path``. Does not write anything; callers persist the result.
    """
    run_dir = Path(run_dir)
    golden_path = Path(golden_path)
    golden_lookup = _golden_lookup(golden_path)
    targets = run_golden_live.golden_targets(golden_path)

    rows: list[dict[str, Any]] = []
    for municipio, bucket in targets:
        key = (golden_evaluator.muni_key(municipio), bucket)
        golden_main, golden_extra = golden_lookup.get(key, ("", ""))
        expectation, golden_urls = golden_runner._golden_expectation(
            golden_main, golden_extra
        )

        v2 = load_v2_unit(run_dir, municipio, bucket)
        v1 = v1_baseline(v2, bucket)

        v1_vs_golden = (
            golden_runner.compare_to_golden(
                decision=v1["decision"], url=v1["url"],
                golden_main=golden_main, golden_extra=golden_extra,
            )
            if v1 is not None else None
        )
        v2_vs_golden = (
            golden_runner.compare_to_golden(
                decision=v2["decision"], url=v2["url"],
                golden_main=golden_main, golden_extra=golden_extra,
            )
            if v2 is not None else None
        )
        flip_v1_v2 = (
            golden_runner.classify_flip(
                v1_decision=v1["decision"], v1_url=v1["url"],
                v2_decision=v2["decision"], v2_url=v2["url"],
            )
            if (v1 is not None and v2 is not None) else None
        )
        cause_kind = v2["cause_kind"] if v2 is not None else ""
        cause_code = v2["cause_code"] if v2 is not None else ""
        revisar_por = v2["revisar_por"] if v2 is not None else ""
        discrepancy_category = classify_discrepancy(
            v1=v1, v2=v2, cause_kind=cause_kind, cause_code=cause_code,
            flip_v1_v2=flip_v1_v2,
        )

        rows.append({
            "municipio": municipio,
            "bucket": bucket,
            "golden": {"expectation": expectation, "urls": golden_urls},
            "v1_baseline": v1,
            "v2": (
                {
                    "decision": v2["decision"],
                    "url": v2["url"],
                    "citations": v2["citations"],
                }
                if v2 is not None else None
            ),
            "v1_baseline_vs_golden": v1_vs_golden,
            "v2_vs_golden": v2_vs_golden,
            "flip_v1_v2": flip_v1_v2,
            "cause_kind": cause_kind,
            "cause_code": cause_code,
            "revisar_por": revisar_por,
            "discrepancy_category": discrepancy_category,
        })

    rows.sort(key=lambda row: (golden_evaluator.muni_key(row["municipio"]), row["bucket"]))
    return {
        "schema_version": 1,
        "run_dir": str(run_dir),
        "golden_path": str(golden_path),
        "rows": rows,
    }


def matrix_csv_bytes(matrix: Mapping[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in matrix["rows"]:
        v1 = row["v1_baseline"]
        v2 = row["v2"]
        writer.writerow({
            "municipio": row["municipio"],
            "bucket": row["bucket"],
            "golden_expectation": row["golden"]["expectation"],
            "golden_urls": " | ".join(row["golden"]["urls"]),
            "v1_baseline_decision": v1["decision"] if v1 is not None else "",
            "v1_baseline_url": v1["url"] if v1 is not None else "",
            "v1_baseline_vs_golden": row["v1_baseline_vs_golden"] or "",
            "v2_decision": v2["decision"] if v2 is not None else "",
            "v2_url": v2["url"] if v2 is not None else "",
            "v2_vs_golden": row["v2_vs_golden"] or "",
            "flip_v1_v2": row["flip_v1_v2"] or "",
            "cause_kind": row["cause_kind"],
            "cause_code": row["cause_code"],
            "revisar_por": row["revisar_por"],
            "discrepancy_category": row["discrepancy_category"],
        })
    return output.getvalue().encode("utf-8")


def _checked_output_dir(output_dir: Path) -> Path:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Comparacion semantica ex-post V1-vs-V2 sobre evidencia V2 ya "
            "congelada por run_golden_live (Fase 4, comparacion 1). "
            "--run-dir se abre solo lectura; toda salida va a --output-dir."
        )
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    matrix = build_matrix(run_dir=args.run_dir, golden_path=args.golden)
    destination = _checked_output_dir(args.output_dir)
    (destination / "semantic_matrix.json").write_bytes(
        golden_runner.canonical_json_bytes(matrix)
    )
    (destination / "semantic_matrix.csv").write_bytes(matrix_csv_bytes(matrix))
    counts: dict[str, int] = {}
    for row in matrix["rows"]:
        category = row["discrepancy_category"]
        counts[category] = counts.get(category, 0) + 1
    print(
        "semantic_comparison=complete "
        f"rows={len(matrix['rows'])} output_dir={destination} "
        f"by_category={json.dumps(counts, sort_keys=True)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
