from __future__ import annotations

import json

import pytest

from scripts.fase2_municipios.v2.eval.poison_report import build_poison_report


pytestmark = pytest.mark.offline


def _checkpoint(final: str, a: str, b: str, *, judge_reason: str = "not_invoked_consensus"):
    return {"schema_version": 1, "units": {"key": {
        "municipio": "fixture", "bucket": "concurso_publico",
        "result": {"outcome": {"decision": final, "layer": {
            "proposal_a": {"decision": a}, "proposal_b": {"decision": b},
            "judge_response": {"decision": "revisar", "reason": judge_reason},
        }}},
    }}}


def _manifest():
    return {"expectation": "reject_or_review", "required_types": ["licitacoes"],
            "cases": [{"municipio": "Fixture", "bucket": "concurso_publico",
                       "tipo": "licitacoes"}]}


@pytest.mark.parametrize("final", ["revisar", "nao_encontrado", "licitacao_rechazada"])
def test_reject_or_review_is_not_a_false_positive(final: str) -> None:
    report = build_poison_report(_manifest(), _checkpoint(final, final, final),
                                 min_cases=1, max_cases=20)
    assert report["totals"]["fp"] == 0


@pytest.mark.parametrize("final", ["indice_oficial", "indice_oficial_combinado", "portal_externo_oficial"])
def test_every_affirmative_final_is_a_false_positive(final: str) -> None:
    report = build_poison_report(_manifest(), _checkpoint(final, final, final),
                                 min_cases=1, max_cases=20)
    assert report["totals"]["fp"] == 1


def test_bc_net_capture_requires_affirmative_a_and_nonaffirmative_final() -> None:
    captured = build_poison_report(
        _manifest(), _checkpoint("revisar", "indice_oficial", "revisar"),
        min_cases=1, max_cases=20,
    )
    stopped_by_a = build_poison_report(
        _manifest(), _checkpoint("revisar", "revisar", "revisar"),
        min_cases=1, max_cases=20,
    )
    assert captured["totals"]["bc_net_captures"] == 1
    assert stopped_by_a["totals"]["bc_net_captures"] == 0


def test_missing_checkpoint_unit_fails_closed() -> None:
    with pytest.raises(ValueError, match="checkpoint_missing_unit"):
        build_poison_report(_manifest(), {"schema_version": 1, "units": {}},
                            min_cases=1, max_cases=20)


def test_validation_failure_review_without_layer_is_safe_abstention() -> None:
    checkpoint = _checkpoint("revisar", "revisar", "revisar")
    checkpoint["units"]["key"]["result"]["outcome"]["layer"] = None
    report = build_poison_report(_manifest(), checkpoint, min_cases=1, max_cases=20)
    assert report["totals"]["fp"] == 0
    assert report["totals"]["unadjudicated_reviews"] == 1
