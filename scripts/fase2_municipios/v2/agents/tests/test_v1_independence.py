"""Prueba arquitectonica de independencia V1/V2 (directiva 12-jul).

El camino DECISOR de V2 no puede importar ni invocar simbolos semanticos de V1:
- verdict_extract como clasificador semantico;
- cascade.derive_final_decision / cascade.resolve_selector_pick como autoridad;
- el codigo de transicion v2_affirms_v1_disagrees_pending_audit;
- degradacion a revisar porque V1 discrepa.

V2 SI puede reutilizar infraestructura tecnica neutral de cascade (buckets
canonicos, normalizacion de URLs, EvidenceSnapshot, autoridad/identidad basadas
en evidencia, deteccion objetiva de challenge/soft-404). V1 queda congelada y
solo corre como baseline comparativo, fuera del runtime V2.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2.agents.certifier import (
    AFFIRMATIVE_CERTIFIER_DECISIONS,
)
from scripts.fase2_municipios.v2.agents.tests import test_orchestration as fx


pytestmark = pytest.mark.offline

V2_ROOT = pathlib.Path(__file__).resolve().parents[2]
DECISION_MODULES = (
    V2_ROOT / "agents" / "orchestration.py",
    V2_ROOT / "agents" / "certifier.py",
    V2_ROOT / "agents" / "prosecutor.py",
    V2_ROOT / "agents" / "judge.py",
    V2_ROOT / "agents" / "base.py",
    V2_ROOT / "agents" / "schemas.py",
    # El runtime live tambien es camino decisor: construye la evidencia que el
    # gate consume. No puede correr el clasificador semantico V1.
    V2_ROOT / "eval" / "live_abc_adapter.py",
    V2_ROOT / "eval" / "structural_evidence.py",
    # authority.py alimenta provenance al gate estructural (official_referrer).
    V2_ROOT / "authority.py",
    # Cliente y verificacion de citas: construyen/validan la evidencia que el
    # gate consume (defensa en profundidad, revision Opus 12-jul).
    V2_ROOT / "gemini" / "client.py",
    V2_ROOT / "snapshot" / "snapshot.py",
)
FORBIDDEN_IMPORT_SUBSTRINGS = ("verdict_extract",)
FORBIDDEN_CALL_ATTRS = {
    "derive_final_decision",
    "resolve_selector_pick",
    "build_candidate_record",
    "evaluate_candidate_contract",
    "derive_decision",
    "candidate_content_state",
}


def _module_ast(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_decision_modules_never_import_verdict_extract() -> None:
    for path in DECISION_MODULES:
        tree = _module_ast(path)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                # Un import aliasado ("from cascade import derive_final_decision
                # as _x") evadiria tanto el chequeo de modulo como el de
                # ast.Name (revision Opus 12-jul): el simbolo importado tambien
                # se valida por nombre real.
                for alias in node.names:
                    assert alias.name not in FORBIDDEN_CALL_ATTRS, (
                        f"{path.name} importa simbolo semantico V1 prohibido: "
                        f"{alias.name}"
                    )
            for name in names:
                assert not any(
                    forbidden in name for forbidden in FORBIDDEN_IMPORT_SUBSTRINGS
                ), f"{path.name} importa simbolo semantico V1 prohibido: {name}"


def test_decision_modules_never_reference_v1_semantic_authority() -> None:
    for path in DECISION_MODULES:
        tree = _module_ast(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALL_ATTRS:
                raise AssertionError(
                    f"{path.name} referencia autoridad semantica V1 prohibida: "
                    f".{node.attr}"
                )
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_CALL_ATTRS:
                raise AssertionError(
                    f"{path.name} referencia autoridad semantica V1 prohibida: "
                    f"{node.id}"
                )


def test_transitional_pending_audit_code_is_gone() -> None:
    source = (V2_ROOT / "agents" / "orchestration.py").read_text(encoding="utf-8")
    assert "v2_affirms_v1_disagrees_pending_audit" not in source, (
        "el codigo de transicion (cola por discrepancia V1) debe eliminarse: "
        "V1 ya no participa en la decision V2"
    )


def test_resolve_never_calls_v1_semantic_authority_at_runtime(monkeypatch) -> None:
    """Guardia dinamica: una resolucion afirmativa completa (A+B consenso ->
    confirmacion) jamas invoca derive_final_decision ni resolve_selector_pick."""

    def _forbidden(name):
        def _raise(*args, **kwargs):
            raise AssertionError(
                f"cascade.{name} fue invocado durante la decision V2"
            )
        return _raise

    monkeypatch.setattr(
        cascade, "derive_final_decision", _forbidden("derive_final_decision")
    )
    monkeypatch.setattr(
        cascade, "resolve_selector_pick", _forbidden("resolve_selector_pick")
    )

    candidate = fx.record(
        "ok", fx.URL_A,
        decision="no_adjudicado_por_v1",  # la etiqueta V1 no debe leerse
        bucket="processo_seletivo",
    )
    proposal = fx.proposal(
        candidate, decision="indice_oficial", bucket="processo_seletivo",
    )
    service, judge_client = fx.orchestrator(
        AssertionError("judge must not be called")
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(candidate,),
        proposal_a=proposal,
        proposal_b=proposal,
        requested_bucket="processo_seletivo",
        prosecutor_result="sustain",
    )

    assert judge_client.calls == []
    assert result.final_decision.status == "confirmado"
    assert result.final_decision.decision in AFFIRMATIVE_CERTIFIER_DECISIONS
    assert result.final_decision.bucket == "processo_seletivo"


def test_judge_path_never_calls_v1_semantic_authority_at_runtime(monkeypatch) -> None:
    """Guardia dinamica de la rama del juez C (revision Opus 12-jul): una
    resolucion por desacuerdo A/B adjudicada por C tampoco invoca jamas
    derive_final_decision ni resolve_selector_pick."""

    def _forbidden(name):
        def _raise(*args, **kwargs):
            raise AssertionError(
                f"cascade.{name} fue invocado durante la decision V2 (rama C)"
            )
        return _raise

    monkeypatch.setattr(
        cascade, "derive_final_decision", _forbidden("derive_final_decision")
    )
    monkeypatch.setattr(
        cascade, "resolve_selector_pick", _forbidden("resolve_selector_pick")
    )

    a = fx.record("a", fx.URL_A, decision="no_adjudicado_por_v1")
    b = fx.record(
        "b", fx.URL_B,
        decision="no_adjudicado_por_v1",
        bucket="processo_seletivo",
    )
    service, judge_client = fx.orchestrator(
        {"decision": "aceptar_A", "reason": "closed choice"}
    )

    result = service.resolve(
        snapshot=fx.snapshot(),
        candidates=(a, b),
        proposal_a=fx.proposal(a, decision="indice_oficial"),
        proposal_b=fx.proposal(
            b, decision="portal_externo_oficial", bucket="processo_seletivo"
        ),
    )

    assert len(judge_client.calls) == 1
    assert result.final_decision.status == "confirmado"
    assert result.final_decision.candidate_id == "a"
