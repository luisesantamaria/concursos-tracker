from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

import cierre_dataset as Z  # noqa: E402


def test_retry_operational_retries_only_op_bucket(monkeypatch, tmp_path):
    input_csv = tmp_path / "in.csv"
    output_csv = tmp_path / "out.csv"
    cols = [
        "municipio", "site_base",
        "url_concursos", "confianza_concursos", "tier_concursos",
        "url_processos_seletivos", "confianza_processos", "tier_processos",
        "notes",
    ]
    with input_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({
            "municipio": "Teste",
            "site_base": "https://example.test",
            "url_concursos": "https://example.test/concursos",
            "confianza_concursos": "confirmado",
            "tier_concursos": "t1",
            "url_processos_seletivos": "",
            "confianza_processos": "",
            "tier_processos": "",
            "notes": "",
        })

    calls: list[str] = []

    def fake_rendered(_session, _model, _municipio, bucket, _url, _timeout, _mode,
                      *_args):
        calls.append(bucket)
        if len(calls) == 1:
            return "revisar", "extract: revisar_op:timeout"
        return "confirmado", "extract_confirmar: cert=2"

    monkeypatch.setattr(Z.C, "make_session", lambda: object())
    monkeypatch.setattr(Z, "rendered_verdict", fake_rendered)
    monkeypatch.setattr(Z, "apply_menu_reachability_guard",
                        lambda *_args: (True, "tier_menu:t1"))
    monkeypatch.setattr(sys, "argv", [
        "cierre_dataset.py",
        "--input", str(input_csv),
        "--output", str(output_csv),
        "--no-investigate",
        "--no-repair",
        "--extract-authority",
        "--retry-operational",
        "--op-retry-wait", "0",
    ])

    assert Z.main() == 0
    out = list(csv.DictReader(output_csv.open(encoding="utf-8")))[0]
    assert calls == ["concursos", "concursos"]
    assert out["confianza_concursos"] == "confirmado"
    assert "op_retry[concursos]: confirmado" in out["notes"]


def test_operational_failure_skips_repair_and_investigate(monkeypatch, tmp_path):
    input_csv = tmp_path / "in.csv"
    output_csv = tmp_path / "out.csv"
    cols = [
        "municipio", "site_base",
        "url_concursos", "confianza_concursos", "tier_concursos",
        "url_processos_seletivos", "confianza_processos", "tier_processos",
        "notes",
    ]
    with input_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({
            "municipio": "Teste",
            "site_base": "https://example.test",
            "url_concursos": "https://example.test/concursos",
            "confianza_concursos": "confirmado",
            "tier_concursos": "t4",
            "url_processos_seletivos": "https://example.test/processos",
            "confianza_processos": "confirmado",
            "tier_processos": "t1",
            "notes": "",
        })

    calls: list[str] = []

    def fake_rendered(_session, _model, _municipio, bucket, _url, _timeout, _mode,
                      *_args):
        calls.append(bucket)
        if bucket == "processos":
            return "confirmado", "extract_confirmar: cert=2"
        return "revisar", "extract: revisar_op:timeout"

    monkeypatch.setattr(Z.C, "make_session", lambda: object())
    monkeypatch.setattr(Z, "rendered_verdict", fake_rendered)
    monkeypatch.setattr(Z, "_repair_via_canonical",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("repair touched")))
    monkeypatch.setattr(Z.C, "process_municipio",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("investigate touched")))
    monkeypatch.setattr(sys, "argv", [
        "cierre_dataset.py",
        "--input", str(input_csv),
        "--output", str(output_csv),
        "--extract-authority",
    ])

    assert Z.main() == 0
    out = list(csv.DictReader(output_csv.open(encoding="utf-8")))[0]
    assert calls == ["concursos", "processos"]
    assert out["confianza_concursos"] == "revisar"
    assert "extract: revisar_op:timeout" in out["notes"]
