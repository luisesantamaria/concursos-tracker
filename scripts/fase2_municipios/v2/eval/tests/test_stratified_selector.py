"""Offline contract tests for the score-free stratified selector."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval import stratified_selector as selector


pytestmark = pytest.mark.offline


def row(
    index: int,
    *,
    host: str = "rs.gov.br",
    concursos: str = "boa",
    processos: str = "boa",
    path: str = "",
    external: str = "",
) -> dict[str, str]:
    base = f"https://m{index:03d}.{host}/"
    return {
        "uf": "RS",
        "municipio": f"Município Fixture {index:03d}",
        "ibge": str(4_300_000 + index),
        "site_base": base,
        "url_concursos": external or f"{base}{path or 'concursos'}",
        "url_processos_seletivos": f"{base}processos",
        "url_editais": "",
        "url_convocacoes": "",
        "url_diario_publicacoes": "",
        "status_concursos": concursos,
        "status_processos_seletivos": processos,
        "status_convocacoes": "nao_encontrada",
        "status_diario_publicacoes": "nao_encontrada",
        "confidence": "0",
        "method": "fixture",
        "gemini_used": "0",
        "notes": "synthetic test fixture",
        "checked_at": "fixed-but-excluded",
    }


def universe(count: int = 80) -> list[dict[str, str]]:
    result = []
    hosts = ("rs.gov.br", "atende.net", "govbr.cloud", "example.invalid")
    states = (("boa", "boa"), ("nao_encontrada", "nao_encontrada"), ("boa", "nao_encontrada"), ("revisar", "boa"))
    for index in range(count):
        host = hosts[index % len(hosts)]
        concursos, processos = states[index % len(states)]
        result.append(row(index, host=host, concursos=concursos, processos=processos))
    return result


def test_determinism_size_uniqueness_and_seed_variation() -> None:
    rows = universe()
    first = selector.select_sample(rows, [], size=50, seed=17, borderline_minimum=10)
    second = selector.select_sample(rows, [], size=50, seed=17, borderline_minimum=10)
    other = selector.select_sample(rows, [], size=50, seed=18, borderline_minimum=10)
    first_bytes = selector.canonical_json_bytes(first)
    assert first_bytes == selector.canonical_json_bytes(second)
    assert first_bytes != selector.canonical_json_bytes(other)
    selected = first["selected"]
    assert len(selected) == 50
    assert len({item["identity"] for item in selected}) == 50
    assert {item["municipio"] for item in selected} <= {item["municipio"] for item in rows}


def test_family_precedence_is_unique_and_signals_are_separate() -> None:
    table = selector.load_family_table()
    candidate = row(
        1,
        host="atende.net",
        path="multi24/transparencia",
        external="http://192.0.2.1/multi24/transparencia",
    )
    labelled = selector.classify_candidate(candidate, table)
    assert labelled["familia_portal"] == "multi24"
    assert isinstance(labelled["familia_portal"], str)
    assert labelled["signals"] == {
        "ip_delegado": True,
        "multiples_hosts": True,
        "usa_transparencia_externa": True,
    }


@pytest.mark.parametrize(
    ("concursos", "processos", "expected"),
    [
        ("boa", "boa", "confirmado"),
        ("nao_encontrada", "nao_encontrada", "nao_encontrado"),
        ("boa", "nao_encontrada", "misto"),
        ("revisar", "boa", "revisar"),
        ("", "", "sem_saida_previa"),
    ],
)
def test_closed_state_mapping_preserves_source(concursos: str, processos: str, expected: str) -> None:
    state, source = selector.map_state({
        "status_concursos": concursos,
        "status_processos_seletivos": processos,
    })
    assert state == expected
    assert state in selector.STATE_VOCABULARY
    assert source == {"concursos": concursos, "processos_seletivos": processos}


def test_borderline_reasons_are_deterministic_and_minimum_is_met() -> None:
    artifact = selector.select_sample(universe(), [], size=50, seed=9, borderline_minimum=10)
    borderlines = [item for item in artifact["selected"] if item["borderline"]]
    assert len(borderlines) >= 10
    for item in artifact["selected"]:
        reasons = item["borderline_reasons"]
        assert reasons == [reason for reason in selector.BORDERLINE_REASON_ORDER if reason in reasons]
        assert item["borderline"] is bool(reasons)


def test_hierarchy_reserves_borderlines_then_family_then_state_and_redistributes() -> None:
    rows = [row(i, host="atende.net", concursos="revisar") for i in range(3)]
    rows += [row(i, host="govbr.cloud", concursos="boa") for i in range(3, 5)]
    rows += [row(i, host="rs.gov.br", concursos="boa" if i % 2 else "nao_encontrada") for i in range(5, 20)]
    artifact = selector.select_sample(rows, [], size=12, seed=2, borderline_minimum=5)
    selected = artifact["selected"]
    assert all(any(item["identity"] == selector.classify_candidate(source, selector.load_family_table())["identity"] for item in selected) for source in rows[:5])
    assert sum(item["selection_phase"] == "borderline_reserve" for item in selected) >= 5
    assert set(artifact["coverage"]["families"]) >= {"atende_net", "govbr_cloud", "rs_gov_br"}
    assert len(selected) == 12


def test_golden_is_excluded_by_canonical_municipality_or_resource_identity() -> None:
    rows = universe(55)
    golden = [{
        "municipio": "municipio fixture 000",
        "site_base": "",
        "url_concursos": "",
        "url_processos_seletivos": "",
        "urls_concursos_extra": "",
        "urls_processos_extra": "",
    }, {
        "municipio": "unrelated",
        "site_base": "HTTP://WWW.M001.ATENDE.NET/#fragment",
        "url_concursos": "",
        "url_processos_seletivos": "",
        "urls_concursos_extra": "",
        "urls_processos_extra": "",
    }]
    artifact = selector.select_sample(rows, golden, size=50, seed=4, borderline_minimum=10)
    names = {item["municipio"] for item in artifact["selected"]}
    assert "Município Fixture 000" not in names
    assert "Município Fixture 001" not in names
    assert artifact["excluded_golden_count"] == 2


def test_no_scores_lexical_strata_and_rng_only_inside_strata(monkeypatch) -> None:
    source = Path(selector.__file__).read_text(encoding="utf-8")
    assert "score" not in source.casefold()
    calls = []

    class SpyRandom:
        def __init__(self, seed):
            calls.append(("seed", seed))
        def shuffle(self, values):
            calls.append(("shuffle", tuple(item["identity"] for item in values)))
            values.reverse()

    monkeypatch.setattr(selector.random, "Random", SpyRandom)
    artifact = selector.select_sample(universe(60), [], size=50, seed=7, borderline_minimum=10)
    assert artifact["strata_order"] == sorted(artifact["strata_order"])
    assert calls[0] == ("seed", 7)
    assert all(call[0] == "shuffle" for call in calls[1:])


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_process_stability_and_offline_boundary(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.csv"
    golden_path = tmp_path / "golden.csv"
    _write_csv(universe_path, universe())
    _write_csv(golden_path, [{
        "municipio": "Golden Fixture",
        "tipo": "fixture", "site_base": "https://golden.invalid/",
        "url_concursos": "no_existe", "url_processos_seletivos": "no_existe",
        "urls_concursos_extra": "", "urls_processos_extra": "",
        "requiere_revision_humana": "no", "notas": "fixture",
    }])
    outputs = []
    for hash_seed in ("1", "987654"):
        output_json = tmp_path / f"out-{hash_seed}.json"
        output_csv = tmp_path / f"out-{hash_seed}.csv"
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        for key in ("GEMINI_API_KEY", "GEMINI_API_KEY_FREE", "GEMINI_API_KEY_PAID", "GOOGLE_APPLICATION_CREDENTIALS"):
            env[key] = "MUST_NOT_BE_READ"
        completed = subprocess.run([
            sys.executable, "-m", "scripts.fase2_municipios.v2.eval.stratified_selector",
            "--universe", str(universe_path), "--golden", str(golden_path),
            "--output-json", str(output_json), "--output-csv", str(output_csv),
            "--size", "50", "--seed", "31", "--borderline-minimum", "10",
        ], cwd=Path(__file__).resolve().parents[5], env=env, capture_output=True, check=False)
        assert completed.returncode == 0, completed.stderr.decode()
        outputs.append(output_json.read_bytes())
    assert outputs[0] == outputs[1]


def test_cli_help_works() -> None:
    with pytest.raises(SystemExit) as raised:
        selector.main(["--help"])
    assert raised.value.code == 0
