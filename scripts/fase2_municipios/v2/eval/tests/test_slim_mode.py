from __future__ import annotations

import pytest

from scripts.fase2_municipios.v2.eval.live_abc_adapter import LiveABCAdapter, _sample_prosecutor
from scripts.fase2_municipios.v2.eval.run_golden_live import _parser


pytestmark = pytest.mark.offline


def test_slim_sample_is_deterministic_and_near_ten_percent() -> None:
    selected = [
        index for index in range(10_000)
        if _sample_prosecutor("Municipio", f"bucket-{index}", seed=2026071206)
    ]
    assert 900 <= len(selected) <= 1_100
    assert selected == [
        index for index in range(10_000)
        if _sample_prosecutor("Municipio", f"bucket-{index}", seed=2026071206)
    ]


def test_cli_defaults_to_slim_and_allows_full_audit_mode() -> None:
    parser = _parser()
    assert parser.get_default("abc_mode") == "slim"
    action = next(a for a in parser._actions if a.dest == "abc_mode")
    assert action.choices == ("slim", "full")


def test_unsampled_affirmative_skips_but_still_runs_structural_gate() -> None:
    from scripts.fase2_municipios.v2.eval.tests import test_live_abc_adapter as fx

    seed = next(
        value for value in range(100)
        if not _sample_prosecutor(fx.MUNICIPIO, fx.BUCKET, seed=value)
    )
    prosecutor = fx.FakeProsecutor()
    adapter = LiveABCAdapter(
        fetcher=fx.FakeFetcher(),
        target_urls={(fx.MUNICIPIO, fx.BUCKET): fx.URL},
        certifier=fx.FakeCertifier(),
        prosecutor=prosecutor,
        judge=fx.FakeJudge(),
        abc_mode="slim",
        seed=seed,
    )

    outcome = adapter.request(fx.MUNICIPIO, fx.BUCKET)

    assert prosecutor.calls == []
    assert outcome.decision == "indice_oficial"
