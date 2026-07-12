"""Offline tests for the ex-post V1-vs-V2 semantic comparison (Fase 4, comp. 1).

Everything here is synthetic: a minimal checkpoint.json (comparable unit,
access_failure unit, and an absent unit implied by the golden universe) and a
2-row golden CSV, both written under ``tmp_path``. Never reads the real golden
set or a real golden_live run directory -- those are only inspected by hand
(see the module docstring) to validate the design against real shapes.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.eval import verdict_extract
from scripts.fase2_municipios.v2.eval import semantic_comparison as sc


pytestmark = pytest.mark.offline

COMPARABLE_URL = "https://fixture.invalid/concursos"
COMPARABLE_CONTENT = "Concurso Publico 01/2024"
MUNI_A = "Fixture Munia"
MUNI_B = "Fixture Munib"


def _outcome(
    *, decision: str, url: str, cause_kind: str, cause_code: str,
    revisar_por: str = "", layer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "municipio": "display name unused by the module",
        "bucket": "concurso_publico",
        "decision": decision,
        "url": url,
        "cause": {
            "kind": cause_kind, "code": cause_code,
            "comment": "synthetic", "revisar_por": revisar_por,
        },
        "layer": layer,
        "events": [],
    }


def _comparable_layer() -> dict[str, Any]:
    return {
        "evidence": {
            "snapshot_ref": "sha256:fixture", "authority": "confirmada",
            "identity": "confirmada", "reason": "single_orion_http_snapshot",
        },
        "sources": [{
            "source_id": "main", "url": COMPARABLE_URL,
            "retrieved_at": "2026-07-12T00:00:00+00:00",
            "content": COMPARABLE_CONTENT,
        }],
        "citations": [{
            "source_id": "main", "start": 0, "end": len(COMPARABLE_CONTENT),
            "quote": COMPARABLE_CONTENT,
        }],
        "candidate": {
            "candidate_id": "v1:fixture-a", "url": COMPARABLE_URL,
            "decision": "indice_oficial", "bucket": "concurso_publico",
            "authority": "confirmada", "identity": "confirmada",
            "evidence_state": "completa", "source_kind": "dominio_oficial_prefeitura",
        },
        "proposal_a": None, "proposal_b": None, "judge_response": None,
    }


def _unit_key(municipio: str, bucket: str) -> str:
    return json.dumps(
        [golden_evaluator.muni_key(municipio), bucket],
        ensure_ascii=False, separators=(",", ":"),
    )


def write_checkpoint(run_dir: Path) -> None:
    units = {
        _unit_key(MUNI_A, "concurso_publico"): {
            "municipio": golden_evaluator.muni_key(MUNI_A),
            "bucket": "concurso_publico",
            "url": COMPARABLE_URL,
            "result": {
                "outcome": _outcome(
                    decision="indice_oficial", url=COMPARABLE_URL,
                    cause_kind="success", cause_code="consensus",
                    layer=_comparable_layer(),
                ),
            },
        },
        _unit_key(MUNI_B, "concurso_publico"): {
            "municipio": golden_evaluator.muni_key(MUNI_B),
            "bucket": "concurso_publico",
            "url": "",
            "result": {
                "outcome": _outcome(
                    decision="revisar", url="",
                    cause_kind="access_failure", cause_code="OSError",
                    revisar_por="revisar_por_adquisicion", layer=None,
                ),
            },
        },
        # Fixture Munia/processo_seletivo and Fixture Munib/processo_seletivo
        # are deliberately absent -- both golden targets for them never ran.
    }
    payload = {"schema_version": 1, "units": units}
    (run_dir / "checkpoint.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def write_golden(path: Path) -> None:
    fieldnames = [
        "municipio", "tipo", "site_base", "url_concursos",
        "url_processos_seletivos", "urls_concursos_extra",
        "urls_processos_extra", "requiere_revision_humana", "notas",
    ]
    rows = [
        {
            "municipio": MUNI_A, "tipo": "synthetic_test_fixture",
            "site_base": "https://fixture.invalid",
            "url_concursos": COMPARABLE_URL,
            "url_processos_seletivos": "no_existe",
            "urls_concursos_extra": "", "urls_processos_extra": "",
            "requiere_revision_humana": "no", "notas": "synthetic only",
        },
        {
            "municipio": MUNI_B, "tipo": "synthetic_test_fixture",
            "site_base": "https://fixture.invalid",
            "url_concursos": "no_existe",
            "url_processos_seletivos": "no_existe",
            "urls_concursos_extra": "", "urls_processos_extra": "",
            "requiere_revision_humana": "no", "notas": "synthetic only",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir()
    write_checkpoint(directory)
    return directory


@pytest.fixture
def golden_path(tmp_path: Path) -> Path:
    path = tmp_path / "golden.csv"
    write_golden(path)
    return path


# --------------------------------------------------------------------------- #
# load_v2_unit
# --------------------------------------------------------------------------- #
def test_load_v2_unit_none_for_unit_that_never_ran(run_dir: Path) -> None:
    assert sc.load_v2_unit(run_dir, "Nobody Here", "concurso_publico") is None
    # Also true for a golden unit that exists but for the OTHER bucket.
    assert sc.load_v2_unit(run_dir, MUNI_A, "processo_seletivo") is None


def test_load_v2_unit_tolerant_to_access_failure_null_layer(run_dir: Path) -> None:
    unit = sc.load_v2_unit(run_dir, MUNI_B, "concurso_publico")
    assert unit is not None
    assert unit["decision"] == "revisar"
    assert unit["url"] == ""
    assert unit["cause_kind"] == "access_failure"
    assert unit["cause_code"] == "OSError"
    assert unit["revisar_por"] == "revisar_por_adquisicion"
    assert unit["candidate"] is None
    assert unit["content"] is None
    assert unit["citations"] == []


def test_load_v2_unit_extracts_layer_fields(run_dir: Path) -> None:
    unit = sc.load_v2_unit(run_dir, MUNI_A, "concurso_publico")
    assert unit is not None
    assert unit["decision"] == "indice_oficial"
    assert unit["url"] == COMPARABLE_URL
    assert unit["cause_kind"] == "success"
    assert unit["content"] == COMPARABLE_CONTENT
    assert unit["candidate"]["source_kind"] == "dominio_oficial_prefeitura"
    assert unit["candidate"]["authority"] == "confirmada"
    assert unit["candidate"]["identity"] == "confirmada"
    assert unit["candidate"]["evidence_state"] == "completa"
    assert len(unit["citations"]) == 1


def test_load_v2_unit_never_writes_to_run_dir(run_dir: Path) -> None:
    before = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}
    sc.load_v2_unit(run_dir, MUNI_A, "concurso_publico")
    sc.load_v2_unit(run_dir, MUNI_B, "concurso_publico")
    sc.load_v2_unit(run_dir, "Nobody Here", "concurso_publico")
    after = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}
    assert before == after
    assert set(before) == {run_dir / "checkpoint.json"}


def test_load_v2_unit_rejects_malformed_checkpoint(tmp_path: Path) -> None:
    bad = tmp_path / "bad_run"
    bad.mkdir()
    (bad / "checkpoint.json").write_text("not json", encoding="utf-8")
    with pytest.raises(sc.SemanticComparisonError):
        sc.load_v2_unit(bad, MUNI_A, "concurso_publico")


def test_load_v2_unit_missing_checkpoint_file(tmp_path: Path) -> None:
    empty = tmp_path / "no_checkpoint"
    empty.mkdir()
    with pytest.raises(sc.SemanticComparisonError):
        sc.load_v2_unit(empty, MUNI_A, "concurso_publico")


# --------------------------------------------------------------------------- #
# v1_baseline
# --------------------------------------------------------------------------- #
def test_v1_baseline_none_when_v2_is_none() -> None:
    assert sc.v1_baseline(None, "concurso_publico") is None


def test_v1_baseline_none_when_no_candidate() -> None:
    v2 = {"candidate": None, "content": "some text"}
    assert sc.v1_baseline(v2, "concurso_publico") is None


def test_v1_baseline_none_when_no_content() -> None:
    v2 = {"candidate": {"url": COMPARABLE_URL}, "content": None}
    assert sc.v1_baseline(v2, "concurso_publico") is None


def test_v1_baseline_confirmatory_sets_url_from_candidate(run_dir: Path) -> None:
    v2 = sc.load_v2_unit(run_dir, MUNI_A, "concurso_publico")
    baseline = sc.v1_baseline(v2, "concurso_publico")
    assert baseline is not None
    assert baseline["decision"] == "indice_oficial"
    assert baseline["decision"] in verdict_extract.INDEX_STATES
    assert baseline["url"] == COMPARABLE_URL


def test_v1_baseline_non_confirmatory_clears_url() -> None:
    v2 = {
        "candidate": {
            "url": "https://fixture.invalid/somewhere",
            "source_kind": "desconocido", "authority": "desconocida",
            "identity": "desconocida", "evidence_state": "completa",
        },
        "content": "",
    }
    baseline = sc.v1_baseline(v2, "concurso_publico")
    assert baseline is not None
    assert baseline["decision"] not in verdict_extract.INDEX_STATES
    assert baseline["url"] == ""


# --------------------------------------------------------------------------- #
# classify_discrepancy (pure taxonomy, exercised directly)
# --------------------------------------------------------------------------- #
def test_classify_discrepancy_no_computable_when_v2_missing() -> None:
    result = sc.classify_discrepancy(
        v1=None, v2=None, cause_kind="", cause_code="", flip_v1_v2=None,
    )
    assert result == "no_computable"


def test_classify_discrepancy_adquisicion_takes_priority_over_missing_v1() -> None:
    # v1 is None (access_failure never built a candidate) but v2 ran: the
    # cause-based category must win, not the "missing baseline" fallback.
    result = sc.classify_discrepancy(
        v1=None, v2={"decision": "revisar"}, cause_kind="access_failure",
        cause_code="OSError", flip_v1_v2=None,
    )
    assert result == "adquisicion"


@pytest.mark.parametrize("cause_kind", ["model_failure", "configuration_failure", "internal_failure"])
def test_classify_discrepancy_infra_modelo(cause_kind: str) -> None:
    result = sc.classify_discrepancy(
        v1=None, v2={"decision": "revisar"}, cause_kind=cause_kind,
        cause_code="whatever", flip_v1_v2=None,
    )
    assert result == "infra_modelo"


@pytest.mark.parametrize("cause_code", ["consensus_failed_final_gate", "agreement_review"])
def test_classify_discrepancy_citas_gate(cause_code: str) -> None:
    result = sc.classify_discrepancy(
        v1=None, v2={"decision": "revisar"}, cause_kind="evidence_failure",
        cause_code=cause_code, flip_v1_v2=None,
    )
    assert result == "citas_gate"


def test_classify_discrepancy_evidence_failure_outside_gate_falls_through() -> None:
    # A real code observed in production (v2_affirms_v1_disagrees_pending_audit)
    # has no dedicated bucket: it must fall to the residual semantic check.
    result = sc.classify_discrepancy(
        v1={"decision": "revisar", "url": ""},
        v2={"decision": "revisar", "url": ""},
        cause_kind="evidence_failure",
        cause_code="v2_affirms_v1_disagrees_pending_audit",
        flip_v1_v2="both_review",
    )
    assert result == "sin_discrepancia"


def test_classify_discrepancy_desacuerdo_abc() -> None:
    result = sc.classify_discrepancy(
        v1=None, v2={"decision": "revisar"}, cause_kind="disagreement_unresolved",
        cause_code="judge_error", flip_v1_v2=None,
    )
    assert result == "desacuerdo_abc"


def test_classify_discrepancy_ausencia_legitima() -> None:
    result = sc.classify_discrepancy(
        v1=None, v2={"decision": "revisar"}, cause_kind="legitimate_absence",
        cause_code="agreement_review", flip_v1_v2=None,
    )
    assert result == "ausencia_legitima"


def test_classify_discrepancy_sin_discrepancia_on_equivalent_flip() -> None:
    result = sc.classify_discrepancy(
        v1={"decision": "indice_oficial", "url": COMPARABLE_URL},
        v2={"decision": "indice_oficial", "url": COMPARABLE_URL},
        cause_kind="success", cause_code="consensus",
        flip_v1_v2="both_confirm_same_resource",
    )
    assert result == "sin_discrepancia"


def test_classify_discrepancy_semantico_real_on_real_divergence() -> None:
    result = sc.classify_discrepancy(
        v1={"decision": "indice_oficial", "url": COMPARABLE_URL},
        v2={"decision": "revisar", "url": ""},
        cause_kind="success", cause_code="consensus",
        flip_v1_v2="v1_confirm_v2_review",
    )
    assert result == "semantico_real"


def test_all_categories_are_declared_in_discrepancy_categories() -> None:
    assert sc.DISCREPANCY_CATEGORIES == {
        "adquisicion", "infra_modelo", "citas_gate", "desacuerdo_abc",
        "ausencia_legitima", "semantico_real", "sin_discrepancia", "no_computable",
    }


# --------------------------------------------------------------------------- #
# build_matrix (end to end over the synthetic run_dir/golden fixtures)
# --------------------------------------------------------------------------- #
def test_build_matrix_covers_every_golden_target(run_dir: Path, golden_path: Path) -> None:
    matrix = sc.build_matrix(run_dir=run_dir, golden_path=golden_path)
    keys = {(row["municipio"], row["bucket"]) for row in matrix["rows"]}
    assert keys == {
        (MUNI_A, "concurso_publico"), (MUNI_A, "processo_seletivo"),
        (MUNI_B, "concurso_publico"), (MUNI_B, "processo_seletivo"),
    }
    for row in matrix["rows"]:
        assert row["discrepancy_category"] in sc.DISCREPANCY_CATEGORIES


def test_build_matrix_comparable_unit_is_sin_discrepancia(
    run_dir: Path, golden_path: Path,
) -> None:
    matrix = sc.build_matrix(run_dir=run_dir, golden_path=golden_path)
    row = next(
        r for r in matrix["rows"]
        if r["municipio"] == MUNI_A and r["bucket"] == "concurso_publico"
    )
    assert row["v1_baseline"]["decision"] == "indice_oficial"
    assert row["v2"]["decision"] == "indice_oficial"
    assert row["v1_baseline_vs_golden"] == "match"
    assert row["v2_vs_golden"] == "match"
    assert row["flip_v1_v2"] == "both_confirm_same_resource"
    assert row["discrepancy_category"] == "sin_discrepancia"


def test_build_matrix_access_failure_unit_is_adquisicion(
    run_dir: Path, golden_path: Path,
) -> None:
    matrix = sc.build_matrix(run_dir=run_dir, golden_path=golden_path)
    row = next(
        r for r in matrix["rows"]
        if r["municipio"] == MUNI_B and r["bucket"] == "concurso_publico"
    )
    assert row["v1_baseline"] is None
    assert row["v2"]["decision"] == "revisar"
    assert row["cause_kind"] == "access_failure"
    assert row["v1_baseline_vs_golden"] is None
    assert row["discrepancy_category"] == "adquisicion"


def test_build_matrix_absent_unit_is_no_computable(
    run_dir: Path, golden_path: Path,
) -> None:
    matrix = sc.build_matrix(run_dir=run_dir, golden_path=golden_path)
    row = next(
        r for r in matrix["rows"]
        if r["municipio"] == MUNI_A and r["bucket"] == "processo_seletivo"
    )
    assert row["v2"] is None
    assert row["v1_baseline"] is None
    assert row["v2_vs_golden"] is None
    assert row["flip_v1_v2"] is None
    assert row["discrepancy_category"] == "no_computable"


def test_build_matrix_never_writes_to_run_dir(run_dir: Path, golden_path: Path) -> None:
    before = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}
    sc.build_matrix(run_dir=run_dir, golden_path=golden_path)
    after = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}
    assert before == after


# --------------------------------------------------------------------------- #
# CSV/JSON serialization and CLI output routing
# --------------------------------------------------------------------------- #
def test_matrix_csv_bytes_header_and_row_count(run_dir: Path, golden_path: Path) -> None:
    matrix = sc.build_matrix(run_dir=run_dir, golden_path=golden_path)
    payload = sc.matrix_csv_bytes(matrix).decode("utf-8")
    lines = payload.splitlines()
    assert lines[0] == ",".join(sc.CSV_FIELDS)
    assert len(lines) == 1 + len(matrix["rows"])


def test_main_writes_only_inside_output_dir(
    run_dir: Path, golden_path: Path, tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    before = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}

    exit_code = sc.main([
        "--run-dir", str(run_dir),
        "--golden", str(golden_path),
        "--output-dir", str(output_dir),
    ])

    assert exit_code == 0
    after = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}
    assert before == after, "run_dir must never be written to"
    assert set(os.listdir(output_dir)) == {"semantic_matrix.json", "semantic_matrix.csv"}

    written = json.loads((output_dir / "semantic_matrix.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == 1
    assert len(written["rows"]) == 4
