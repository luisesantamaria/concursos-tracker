"""Offline tests for deterministic per-bucket golden V1/V2 differential replay."""

from __future__ import annotations

import copy
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2.eval import golden_runner as runner_module


pytestmark = pytest.mark.offline
URL_C = "https://www.example.invalid/concursos/?b=2&a=1#top"
URL_C_EQUIV = "http://example.invalid/concursos?a=1&b=2"
URL_P = "https://example.invalid/processos"


def write_golden(path: Path, *, pss: str = "no_existe") -> None:
    fieldnames = [
        "municipio", "tipo", "site_base", "url_concursos",
        "url_processos_seletivos", "urls_concursos_extra",
        "urls_processos_extra", "requiere_revision_humana", "notas",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerow({
            "municipio": "Fixture Dual",
            "tipo": "synthetic_test_fixture",
            "site_base": "https://example.invalid",
            "url_concursos": URL_C_EQUIV,
            "url_processos_seletivos": pss,
            "requiere_revision_humana": "no",
            "notas": "synthetic only",
        })


def v1_block(decision: str, url: str, suffix: str) -> dict[str, Any]:
    return {
        "decision": decision,
        "url": url,
        "evidence": {
            "snapshot_ref": f"v1-sha256:{suffix}",
            "authority": "confirmada" if decision != "revisar" else "desconocida",
            "identity": "confirmada" if decision != "revisar" else "desconocida",
            "reason": f"v1-{suffix}",
        },
    }


def v2_block(
    decision: str, url: str, bucket: str, suffix: str,
) -> dict[str, Any]:
    content = "Official index"
    citations = ([{
        "source_id": "main",
        "start": 0,
        "end": len(content),
        "quote": content,
    }] if decision != "revisar" else [])
    proposal = {
        "decision": decision,
        "bucket": bucket,
        "candidate_id": f"candidate-{suffix}" if decision != "revisar" else "",
        "resource_url": url,
        "citations": citations,
        "reason": f"proposal-{suffix}",
    }
    return {
        "evidence": {
            "snapshot_ref": f"v2-sha256:{suffix}",
            "authority": "confirmada" if decision != "revisar" else "desconocida",
            "identity": "confirmada" if decision != "revisar" else "desconocida",
            "reason": f"v2-{suffix}",
            "sources": [{
                "source_id": "main",
                "url": url or "https://example.invalid/review",
                "retrieved_at": "2026-07-11T12:00:00+00:00",
                "content": content,
            }],
        },
        "citations": citations,
        "candidate": ({
            "candidate_id": f"candidate-{suffix}",
            "url": url,
            "decision": decision,
            "bucket": bucket,
            "authority": "confirmada",
            "identity": "confirmada",
            "evidence_state": "completa",
            "source_kind": "dominio_oficial_prefeitura",
        } if decision != "revisar" else None),
        "proposal_a": proposal,
        "proposal_b": copy.deepcopy(proposal),
        "judge_response": {"decision": "aceptar_A", "reason": "cassette"},
    }


def corpus() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cases": [
            {
                "municipio": "Fixture Dual",
                "bucket": "concurso_publico",
                "v1": v1_block("indice_oficial", URL_C, "c"),
                "v2": v2_block("indice_oficial", URL_C_EQUIV, "concurso_publico", "c"),
            },
            {
                "municipio": "Fixture Dual",
                "bucket": "processo_seletivo",
                "v1": v1_block("revisar", "", "p"),
                "v2": v2_block("revisar", "", "processo_seletivo", "p"),
            },
        ],
    }


def write_corpus(path: Path, value: dict[str, Any] | None = None) -> None:
    path.write_text(
        json.dumps(value or corpus(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )


def make_runner():
    FixedReplayClock = getattr(runner_module, "FixedReplayClock")
    return runner_module.GoldenDifferentialRunner(
        seed=7,
        clock=FixedReplayClock(datetime(2026, 7, 11, tzinfo=timezone.utc)),
    )


def test_replay_is_byte_deterministic_and_csv_is_derived(tmp_path: Path) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    write_corpus(replay)
    runner = make_runner()

    first = runner.run_replay(golden_path=golden, corpus_path=replay)
    second = runner.run_replay(golden_path=golden, corpus_path=replay)
    first_bytes = runner_module.canonical_json_bytes(first)
    second_bytes = runner_module.canonical_json_bytes(second)

    assert first_bytes == second_bytes
    assert first_bytes.endswith(b"\n")
    assert str(tmp_path).encode() not in first_bytes
    csv_bytes = runner_module.derived_csv_bytes(first)
    assert b"municipio,bucket,flip_v1_v2" in csv_bytes
    assert b"Fixture Dual,concurso_publico" in csv_bytes
    assert b"\r\n" not in csv_bytes


def test_unit_is_municipality_bucket_and_combined_covers_both(tmp_path: Path) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    value = corpus()
    for case in value["cases"]:
        case["v1"]["decision"] = "indice_oficial_combinado"
        case["v2"] = v2_block(
            "indice_oficial_combinado",
            URL_C_EQUIV,
            case["bucket"],
            case["bucket"],
        )
    write_corpus(replay, value)

    artifact = make_runner().run_replay(golden_path=golden, corpus_path=replay)
    assert [(row["municipio"], row["bucket"]) for row in artifact["rows"]] == [
        ("Fixture Dual", "concurso_publico"),
        ("Fixture Dual", "processo_seletivo"),
    ]
    assert runner_module.decision_covers_bucket(
        "indice_oficial_combinado", "concurso_publico"
    )
    assert runner_module.decision_covers_bucket(
        "indice_oficial_combinado", "processo_seletivo"
    )


@pytest.mark.parametrize(
    ("v1_decision", "v1_url", "v2_decision", "v2_url", "expected"),
    [
        ("indice_oficial", URL_C, "portal_externo_oficial", URL_C_EQUIV, "both_confirm_same_resource"),
        ("indice_oficial", URL_C, "indice_oficial", URL_P, "both_confirm_distinct_resource"),
        ("revisar", "", "indice_oficial", URL_C, "v2_confirm_v1_review"),
        ("indice_oficial", URL_C, "revisar", "", "v1_confirm_v2_review"),
        ("revisar", "", "revisar", "", "both_review"),
        ("nao_encontrado", "", "detalle_individual_rechazado", "", "both_negative"),
        ("nao_encontrado", "", "indice_oficial", URL_C, "v2_confirm_v1_negative"),
        ("portal_externo_oficial", URL_C, "licitacao_rechazada", "", "v1_confirm_v2_negative"),
        ("nao_encontrado", "", "revisar", "", "v2_review_v1_negative"),
        ("revisar", "", "concurso_cultural_rechazado", "", "v1_review_v2_negative"),
    ],
)
def test_flip_closed_domain(
    v1_decision: str, v1_url: str, v2_decision: str, v2_url: str, expected: str,
) -> None:
    assert runner_module.classify_flip(
        v1_decision=v1_decision, v1_url=v1_url,
        v2_decision=v2_decision, v2_url=v2_url,
    ) == expected
    assert expected in runner_module.FLIP_VALUES


@pytest.mark.parametrize(
    ("decision", "url", "golden_main", "expected"),
    [
        ("indice_oficial", URL_C, URL_C_EQUIV, "match"),
        ("indice_oficial", URL_P, URL_C, "differ"),
        ("revisar", "", "", "golden_na"),
        ("nao_encontrado", "", "no_existe", "match"),
        ("revisar", "", "no_existe", "differ"),
    ],
)
def test_golden_comparison_closed_domain(
    decision: str, url: str, golden_main: str, expected: str,
) -> None:
    result = runner_module.compare_to_golden(
        decision=decision,
        url=url,
        golden_main=golden_main,
        golden_extra="",
    )
    assert result == expected
    assert result in runner_module.GOLDEN_COMPARISON_VALUES


def test_same_resource_imports_canonical_api_and_real_types(monkeypatch) -> None:
    calls = []
    real = cascade._normalized_candidate_url

    def spy(value: str) -> str:
        calls.append(type(value))
        return real(value)

    monkeypatch.setattr(cascade, "_normalized_candidate_url", spy)
    result = runner_module.classify_flip(
        v1_decision="indice_oficial", v1_url=URL_C,
        v2_decision="portal_externo_oficial", v2_url=URL_C_EQUIV,
    )
    assert result == "both_confirm_same_resource"
    assert calls == [str, str]
    assert isinstance(real(URL_C), str)


def test_missing_replay_unit_or_evidence_fails_explicitly(tmp_path: Path) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    value = corpus()
    value["cases"] = value["cases"][:1]
    write_corpus(replay, value)
    with pytest.raises(runner_module.ReplayEvidenceError, match="processo_seletivo"):
        make_runner().run_replay(golden_path=golden, corpus_path=replay)

    value = corpus()
    del value["cases"][0]["v2"]["evidence"]["sources"]
    write_corpus(replay, value)
    with pytest.raises(runner_module.ReplayEvidenceError, match="sources"):
        make_runner().run_replay(golden_path=golden, corpus_path=replay)


def test_adjudication_keeps_v1_v2_evidence_separate_and_validates_citations(tmp_path: Path) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    value = corpus()
    value["cases"][0]["v1"]["evidence"]["reason"] = "V1_ONLY"
    value["cases"][0]["v2"]["evidence"]["reason"] = "V2_ONLY"
    value["cases"][0]["v2"]["proposal_a"]["resource_url"] = URL_P
    value["cases"][0]["v2"]["proposal_b"]["resource_url"] = URL_P
    write_corpus(replay, value)
    artifact = make_runner().run_replay(golden_path=golden, corpus_path=replay)
    sheet = artifact["adjudication"]
    assert sheet
    rendered_v1 = json.dumps(sheet[0]["v1_evidence"])
    rendered_v2 = json.dumps(sheet[0]["v2_evidence"])
    assert "V1_ONLY" in rendered_v1 and "V2_ONLY" not in rendered_v1
    assert "V2_ONLY" in rendered_v2 and "V1_ONLY" not in rendered_v2

    value = corpus()
    value["cases"][0]["v2"]["citations"][0]["end"] = 3
    write_corpus(replay, value)
    with pytest.raises(runner_module.ReplayEvidenceError, match="citation"):
        make_runner().run_replay(golden_path=golden, corpus_path=replay)


def test_flips_and_golden_differences_are_reports_not_failures(tmp_path: Path) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    value = corpus()
    value["cases"][0]["v1"]["url"] = URL_P
    write_corpus(replay, value)
    artifact = make_runner().run_replay(golden_path=golden, corpus_path=replay)
    assert artifact["rows"]
    assert artifact["adjudication"]


def test_existing_golden_evaluator_is_called_for_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    write_corpus(replay)
    calls = []
    real = golden_evaluator.judge_bucket

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(golden_evaluator, "judge_bucket", spy)
    artifact = make_runner().run_replay(golden_path=golden, corpus_path=replay)
    assert calls
    assert "golden_metrics" in artifact


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "paid"},
        {"judge_model": "wrong-model"},
        {"tools": [{"google_search": {}}]},
        {"environ": {"GEMINI_API_KEY_FREE": "free", "GOOGLE_API_KEY": "unauthorized"}},
        {"environ": {"GEMINI_API_KEY_FREE": "free", "GOOGLE_APPLICATION_CREDENTIALS": "/adc"}},
    ],
)
def test_live_contract_aborts_before_request(overrides: dict[str, Any]) -> None:
    contract = runner_module.LiveContract.valid_for_tests()
    contract = contract.with_overrides(**overrides)
    calls = []

    class Adapter:
        def request(self):
            calls.append("request")

    with pytest.raises(runner_module.LiveContractError):
        runner_module.run_live(contract=contract, request_adapter=Adapter())
    assert calls == []


def test_replay_never_reads_credentials_or_network(tmp_path: Path, monkeypatch) -> None:
    golden = tmp_path / "golden.csv"
    replay = tmp_path / "replay.json"
    write_golden(golden)
    write_corpus(replay)

    class BombEnvironment(dict):
        def keys(self):
            raise AssertionError("replay must not inspect environment")

        def __contains__(self, _key):
            raise AssertionError("replay must not inspect credentials")

    monkeypatch.setattr(os, "environ", BombEnvironment({
        "GEMINI_API_KEY_PAID": "never-read",
        "GOOGLE_APPLICATION_CREDENTIALS": "never-read",
    }))
    artifact = make_runner().run_replay(golden_path=golden, corpus_path=replay)
    assert artifact["rows"]


def test_v2_replay_e2e_has_coherent_final_decision_and_zero_external_attempts(
    network_guard_spy,
) -> None:
    network_guard_spy.reset()
    fixture_dir = Path(__file__).parent / "fixtures"
    artifact = make_runner().run_replay(
        golden_path=fixture_dir / "synthetic_golden.csv",
        corpus_path=fixture_dir / "synthetic_replay_corpus.json",
    )

    assert runner_module.LiveContract.valid_for_tests().tools is None
    assert artifact["rows"][0]["v2"]["decision"] == "indice_oficial"
    assert artifact["rows"][0]["v2"]["url"]
    assert network_guard_spy.blocked_attempts == 0


def test_cli_help_works() -> None:
    with pytest.raises(SystemExit) as raised:
        runner_module.main(["--help"])
    assert raised.value.code == 0
