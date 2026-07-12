"""Offline provider-routing contracts for the turnkey live CLI."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.fase2_municipios.v2.eval import live_abc_adapter as adapter_module
from scripts.fase2_municipios.v2.eval import live_model_policy as policy_module
from scripts.fase2_municipios.v2.eval import run_golden_live as live_cli
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    FetchedEvidence,
    LiveABCAdapter,
    LiveABCConfigurationError,
    LiveCauseKind,
)


pytestmark = pytest.mark.offline
ORIGINAL_POLICY_FACTORY = LiveABCAdapter.from_model_policy_environment


def _argv(tmp_path: Path, provider: str) -> list[str]:
    golden = tmp_path / "golden.csv"
    url_map = tmp_path / "url_map.csv"
    corpus = tmp_path / "run497"
    corpus.mkdir(exist_ok=True)
    golden.write_text("municipio,tipo\nFixture,fixture\n", encoding="utf-8")
    url_map.write_text(
        "municipio,bucket,url\n"
        "Fixture,concurso_publico,https://fixture.invalid/concursos\n",
        encoding="utf-8",
    )
    return [
        "--provider", provider,
        "--tools", "none",
        "--grounding", "off",
        "--golden", str(golden),
        "--url-map", str(url_map),
        "--v1-corpus-dir", str(corpus),
        "--output-dir", str(tmp_path / "staging" / "run"),
        "--seed", "0",
    ]


def _artifacts(tmp_path: Path):
    return SimpleNamespace(
        output_dir=tmp_path / "staging" / "run",
        coverage={"total": 1, "covered": 1, "sin_cobertura_v1": 0},
        telemetry={
            "free_calls": 0,
            "paid_calls": 0,
            "paid_fallback_reasons": {},
            "tokens": 0,
        },
        sin_cobertura_v1=(),
    )


def test_free_transport_records_free_telemetry() -> None:
    """G4: la telemetria free debe reflejar requests reales (el canario r1/r2
    marco free_calls=0 con ~10 llamadas reales). El camino free no tiene
    fallback pago; esto es contabilidad auditable, no politica."""
    from scripts.fase2_municipios.v2.gemini.client import RawResponse, TokenUsage

    telemetry = policy_module.ModelPolicyTelemetry()

    class _Inner:
        def generate(self, model, contents, config):
            return RawResponse(
                text="{}", usage=TokenUsage(
                    prompt_tokens=1, candidate_tokens=2, total_tokens=3,
                ),
            )

    transport = adapter_module._FreeTelemetryTransport(_Inner(), telemetry)
    transport.generate("gemini-3.1-flash-lite", [], {})
    summary = telemetry.summary()
    assert summary["free_calls"] == 1
    assert summary["paid_calls"] == 0
    assert summary["tokens"] == 3


def test_from_free_environment_wires_telemetry() -> None:
    adapter = LiveABCAdapter.from_free_environment(
        fetcher=object(),
        target_urls={},
        environ={"GEMINI_API_KEY_FREE": "fixture-free"},
        sdk_client_factory=lambda **kwargs: object(),
    )
    assert adapter.telemetry is not None
    assert adapter.telemetry.summary()["free_calls"] == 0


def test_gemini_free_cli_filters_forbidden_credentials_from_file(
    monkeypatch, tmp_path: Path
) -> None:
    """P0 free-only: la CLI publica debe ser alcanzable con el archivo de
    credenciales real (que exige ambas keys): las prohibidas se descartan en la
    frontera CLI, antes de llegar al adapter free (que rechaza su sola
    presencia). gemini_policy NO se filtra (cubierto por otro test)."""
    creds = tmp_path / "gemini_concursos.env"
    creds.write_text(
        "GEMINI_API_KEY_FREE=fixture-free\nGEMINI_API_KEY=fixture-paid\n",
        encoding="utf-8",
    )
    recorded = {}

    def fake_run_golden_live(**kwargs):
        recorded["environ"] = dict(kwargs["environ"])
        return _artifacts(tmp_path)

    monkeypatch.setattr(live_cli, "run_golden_live", fake_run_golden_live)
    code = live_cli.main(
        _argv(tmp_path, "gemini_free") + ["--credentials-file", str(creds)],
        staging_root=tmp_path / "staging",
    )

    assert code == 0
    assert recorded["environ"].get("GEMINI_API_KEY_FREE") == "fixture-free"
    for forbidden in (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        assert forbidden not in recorded["environ"]
    # La funcion exacta que reventaba (UnauthorizedCredentialError) debe aceptar
    # el environ ya filtrado.
    from scripts.fase2_municipios.v2.gemini.client import resolve_free_api_key

    assert resolve_free_api_key(recorded["environ"]) == "fixture-free"


def test_gemini_free_routes_without_paid_credential(monkeypatch, tmp_path: Path) -> None:
    observed = {}

    def free_factory(**kwargs):
        observed["free_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        LiveABCAdapter, "from_free_environment", staticmethod(free_factory)
    )

    def fake_run_golden_live(**kwargs):
        try:
            kwargs["adapter_factory"](
                fetcher=object(),
                target_urls={},
                environ=kwargs["environ"],
                timeout_seconds=kwargs["http_read_timeout"],
            )
        except LiveABCConfigurationError as exc:
            observed["configuration_error"] = str(exc)
            raise
        return _artifacts(tmp_path)

    monkeypatch.setattr(live_cli, "run_golden_live", fake_run_golden_live)

    code = live_cli.main(
        _argv(tmp_path, "gemini_free"),
        environ={"GEMINI_API_KEY_FREE": "fixture-free"},
        staging_root=tmp_path / "staging",
    )

    assert observed.get("configuration_error") is None, observed
    assert code == 0
    assert observed["free_kwargs"]["environ"] == {
        "GEMINI_API_KEY_FREE": "fixture-free"
    }


def test_gemini_free_never_touches_policy_or_paid_transport(
    monkeypatch, tmp_path: Path
) -> None:
    calls = []

    def bomb(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("paid path touched")

    def free_factory(**kwargs):
        calls.append("free")
        return object()

    monkeypatch.setattr(
        LiveABCAdapter, "from_free_environment", staticmethod(free_factory)
    )
    monkeypatch.setattr(
        LiveABCAdapter, "from_model_policy_environment", staticmethod(bomb)
    )
    monkeypatch.setattr(policy_module, "PolicyTransport", bomb)

    def fake_run_golden_live(**kwargs):
        kwargs["adapter_factory"](
            fetcher=object(), target_urls={}, environ=kwargs["environ"]
        )
        return _artifacts(tmp_path)

    monkeypatch.setattr(live_cli, "run_golden_live", fake_run_golden_live)
    code = live_cli.main(
        _argv(tmp_path, "gemini_free"),
        environ={"GEMINI_API_KEY_FREE": "fixture-free"},
        staging_root=tmp_path / "staging",
    )

    assert code == 0
    assert calls == ["free"]


def test_gemini_policy_selects_factory_with_effective_kwargs_and_requires_paid(
    monkeypatch, tmp_path: Path
) -> None:
    recorded = {}

    def policy_factory(*, fetcher, target_urls, environ, timeout_seconds):
        recorded["factory_kwargs"] = {
            "fetcher": fetcher,
            "target_urls": target_urls,
            "environ": environ,
            "timeout_seconds": timeout_seconds,
        }
        return object()

    monkeypatch.setattr(
        LiveABCAdapter,
        "from_model_policy_environment",
        staticmethod(policy_factory),
    )

    def fake_run_golden_live(**kwargs):
        recorded["selected_factory"] = kwargs["adapter_factory"]
        kwargs["adapter_factory"](
            fetcher="fetcher",
            target_urls={('Fixture', 'concurso_publico'): 'https://fixture.invalid'},
            environ=kwargs["environ"],
            timeout_seconds=kwargs["http_read_timeout"],
        )
        return _artifacts(tmp_path)

    monkeypatch.setattr(live_cli, "run_golden_live", fake_run_golden_live)
    environment = {
        "GEMINI_API_KEY_FREE": "fixture-free",
        "GEMINI_API_KEY": "fixture-paid",
    }
    code = live_cli.main(
        _argv(tmp_path, "gemini_policy"),
        environ=environment,
        staging_root=tmp_path / "staging",
    )

    assert code == 0
    assert recorded["selected_factory"] is policy_factory
    assert recorded["factory_kwargs"] == {
        "fetcher": "fetcher",
        "target_urls": {
            ("Fixture", "concurso_publico"): "https://fixture.invalid"
        },
        "environ": environment,
        "timeout_seconds": 60.0,
    }
    with pytest.raises(
        LiveABCConfigurationError, match="paid_fallback_credential_missing"
    ):
        ORIGINAL_POLICY_FACTORY(
            fetcher=object(),
            target_urls={},
            environ={"GEMINI_API_KEY_FREE": "fixture-free"},
        )


def test_injected_adapter_factory_wins_over_provider(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def bomb(*args, **kwargs):
        raise AssertionError("provider factory must not be selected")

    def injected_factory(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        LiveABCAdapter, "from_free_environment", staticmethod(bomb)
    )

    def fake_run_golden_live(**kwargs):
        assert kwargs["adapter_factory"] is injected_factory
        kwargs["adapter_factory"](environ=kwargs["environ"])
        return _artifacts(tmp_path)

    monkeypatch.setattr(live_cli, "run_golden_live", fake_run_golden_live)
    code = live_cli.main(
        _argv(tmp_path, "gemini_free"),
        environ={"GEMINI_API_KEY_FREE": "fixture-free"},
        staging_root=tmp_path / "staging",
        adapter_factory=injected_factory,
    )

    assert code == 0
    assert calls == [{"environ": {"GEMINI_API_KEY_FREE": "fixture-free"}}]


def test_selected_free_model_failure_is_dedicated_fail_closed() -> None:
    url = "https://fixture.invalid/concursos"

    class LocalFetcher:
        def fetch(self, requested_url, *, timeout_seconds):
            return FetchedEvidence(
                requested_url=requested_url,
                final_url=requested_url,
                retrieved_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
                status=200,
                content="Prefeitura Fixture Concursos Públicos",
                html="<h1>Concursos Públicos</h1>",
                title="Concursos Públicos",
            )

    class RaisingCertifier:
        def certify(self, **kwargs):
            raise RuntimeError("free model failed")

    adapter = LiveABCAdapter(
        fetcher=LocalFetcher(),
        target_urls={("Fixture", "concurso_publico"): url},
        certifier=RaisingCertifier(),
        prosecutor=SimpleNamespace(),
        judge=SimpleNamespace(),
    )
    outcome = adapter.request("Fixture", "concurso_publico")

    assert outcome.decision == "revisar"
    assert outcome.url == ""
    assert outcome.cause.kind is LiveCauseKind.MODEL_FAILURE
    assert outcome.original_exception is not None


def test_cli_help_and_invalid_provider_precede_adapter_construction(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    touched = []

    def bomb(*args, **kwargs):
        touched.append(True)
        raise AssertionError("adapter constructed during parsing")

    monkeypatch.setattr(
        LiveABCAdapter, "from_free_environment", staticmethod(bomb)
    )
    with pytest.raises(SystemExit) as help_exit:
        live_cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert help_exit.value.code == 0
    assert "gemini_free" in help_text
    assert "gemini_policy" in help_text

    with pytest.raises(SystemExit) as invalid_exit:
        live_cli.main(_argv(tmp_path, "invalid-provider"))
    assert invalid_exit.value.code == 2
    assert touched == []


def test_network_isolation_has_separate_zero_counters_and_positive_control(
    network_guard_spy,
) -> None:
    network_guard_spy.reset()
    with pytest.raises(RuntimeError, match="RED BLOQUEADA"):
        socket.getaddrinfo("positive-control.invalid", 443)
    assert network_guard_spy.getaddrinfo_attempts == 1

    network_guard_spy.reset()
    assert network_guard_spy.connect_attempts == 0
    assert network_guard_spy.create_connection_attempts == 0
    assert network_guard_spy.getaddrinfo_attempts == 0
