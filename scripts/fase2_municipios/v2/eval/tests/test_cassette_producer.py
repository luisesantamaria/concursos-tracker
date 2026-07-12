"""Offline contract tests for the fail-closed golden cassette producer."""

from __future__ import annotations

import csv
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios.v2.eval.cassette_producer import (
    ABCLayer,
    CandidateLayer,
    CassetteProducer,
    CitationLayer,
    DiagnosticCode,
    EvidenceLayer,
    ExternalAccessBlocked,
    ProposalLayer,
    SourceLayer,
    V1Layer,
)
from scripts.fase2_municipios.v2.eval.golden_runner import (
    GoldenDifferentialRunner,
    JsonReplayFetchAdapter,
    ReplayEvidenceError,
)


pytestmark = pytest.mark.offline
MUNICIPIO = "Fixture Producer"
TARGETS = (
    (MUNICIPIO, "concurso_publico"),
    (MUNICIPIO, "processo_seletivo"),
)


def _url(municipio: str, bucket: str) -> str:
    slug = golden_evaluator.muni_key(municipio).replace(" ", "-")
    return f"https://fixture.invalid/{slug}/{bucket}"


def _v1(municipio: str, bucket: str) -> V1Layer:
    return V1Layer(
        decision="indice_oficial",
        url=_url(municipio, bucket),
        evidence=EvidenceLayer(
            snapshot_ref=f"v1-sha256:{bucket}",
            authority="confirmada",
            identity="confirmada",
            reason=f"fixture-v1-{bucket}",
        ),
    )


def _abc(municipio: str, bucket: str) -> ABCLayer:
    url = _url(municipio, bucket)
    content = f"Official index for {municipio} {bucket}"
    citation = CitationLayer("main", 0, len(content), content)
    candidate_id = f"candidate-{golden_evaluator.muni_key(municipio)}-{bucket}"
    proposal = ProposalLayer(
        decision="indice_oficial",
        bucket=bucket,
        candidate_id=candidate_id,
        resource_url=url,
        citations=(citation,),
        reason=f"fixture-proposal-{bucket}",
    )
    return ABCLayer(
        evidence=EvidenceLayer(
            snapshot_ref=f"v2-sha256:{bucket}",
            authority="confirmada",
            identity="confirmada",
            reason=f"fixture-v2-{bucket}",
        ),
        sources=(SourceLayer(
            source_id="main",
            url=url,
            retrieved_at="2026-07-11T12:00:00+00:00",
            content=content,
        ),),
        citations=(citation,),
        candidate=CandidateLayer(
            candidate_id=candidate_id,
            url=url,
            decision="indice_oficial",
            bucket=bucket,
            authority="confirmada",
            identity="confirmada",
            evidence_state="completa",
            source_kind="fixture_official",
        ),
        proposal_a=proposal,
        proposal_b=proposal,
        judge_response={"decision": "aceptar_A", "reason": "fixture-c"},
    )


class FakeV1Source:
    def __init__(self, layers=None) -> None:
        self.layers = layers or {}

    def get(self, municipio: str, bucket: str):
        return self.layers.get((municipio, bucket))


class FakeABCProvider:
    def __init__(self, layers=None) -> None:
        self.layers = layers or {}

    def get(self, municipio: str, bucket: str):
        return self.layers.get((municipio, bucket))


def _producer(*, v1_layers=None, abc_layers=None) -> CassetteProducer:
    v1_layers = v1_layers or {target: _v1(*target) for target in TARGETS}
    abc_layers = abc_layers or {target: _abc(*target) for target in TARGETS}
    return CassetteProducer(
        v1_source=FakeV1Source(v1_layers),
        abc_provider=FakeABCProvider(abc_layers),
    )


def _write_golden(path: Path, municipio: str = MUNICIPIO) -> None:
    fieldnames = [
        "municipio", "tipo", "site_base", "url_concursos",
        "url_processos_seletivos", "urls_concursos_extra",
        "urls_processos_extra", "requiere_revision_humana", "notas",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerow({
            "municipio": municipio,
            "tipo": "synthetic",
            "site_base": "https://fixture.invalid",
            "url_concursos": _url(municipio, "concurso_publico"),
            "url_processos_seletivos": _url(municipio, "processo_seletivo"),
            "requiere_revision_humana": "no",
            "notas": "offline fixture",
        })


@pytest.fixture
def precise_network_guard(monkeypatch):
    calls = []

    def blocked(*args, **kwargs):
        calls.append((args, kwargs))
        raise ExternalAccessBlocked("PRECISE_NETWORK_GUARD")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    return calls


def _codes(result) -> set[DiagnosticCode]:
    return {item.code for item in result.diagnostics}


def test_fake_round_trip_uses_real_offline_replay_and_is_deterministic(
    tmp_path: Path, precise_network_guard,
) -> None:
    golden = tmp_path / "golden.csv"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_golden(golden)
    producer = _producer()

    first_result = producer.produce_and_publish(
        TARGETS, destination=first, golden_path=golden
    )
    second_result = producer.produce_and_publish(
        reversed(TARGETS), destination=second, golden_path=golden
    )

    assert first_result.complete and second_result.complete
    assert first.read_bytes() == second.read_bytes()
    assert precise_network_guard == []
    assert len(JsonReplayFetchAdapter(first).cases()) == 2
    artifact = GoldenDifferentialRunner().run_replay(
        golden_path=golden,
        corpus_path=first,
    )
    assert len(artifact["rows"]) == 2


def test_malicious_provider_network_attempt_propagates_precise_guard(
    precise_network_guard,
) -> None:
    class MaliciousABCProvider:
        def get(self, municipio: str, bucket: str):
            socket.create_connection(("malicious.invalid", 443))

    producer = CassetteProducer(
        v1_source=FakeV1Source({TARGETS[0]: _v1(*TARGETS[0])}),
        abc_provider=MaliciousABCProvider(),
    )
    with pytest.raises(ExternalAccessBlocked, match="PRECISE_NETWORK_GUARD"):
        producer.produce((TARGETS[0],))
    assert len(precise_network_guard) == 1


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("proposal_a", DiagnosticCode.MISSING_A),
        ("proposal_b", DiagnosticCode.MISSING_B),
        ("judge_response", DiagnosticCode.MISSING_C),
    ],
)
def test_missing_abc_components_fail_closed_without_file(
    tmp_path: Path, field: str, code: DiagnosticCode,
) -> None:
    target = TARGETS[0]
    incomplete = replace(_abc(*target), **{field: None})
    producer = _producer(
        v1_layers={target: _v1(*target)},
        abc_layers={target: incomplete},
    )
    destination = tmp_path / f"{field}.json"
    result = producer.produce_and_publish(
        (target,), destination=destination, golden_path=tmp_path / "unused.csv"
    )
    assert not result.complete
    assert code in _codes(result)
    assert not destination.exists()


def test_golden_without_run497_is_missing_v1_and_requires_live_in_memory(
    tmp_path: Path,
) -> None:
    target = ("Araricá", "concurso_publico")
    destination = tmp_path / "must-not-exist.json"
    producer = CassetteProducer(
        v1_source=FakeV1Source(),
        abc_provider=FakeABCProvider({target: _abc(*target)}),
    )
    result = producer.produce_and_publish(
        (target,), destination=destination, golden_path=tmp_path / "unused.csv"
    )
    assert not result.complete
    assert _codes(result) == {DiagnosticCode.MISSING_V1}
    assert not destination.exists()


def test_unjustified_v1_has_stable_diagnostic() -> None:
    target = TARGETS[0]
    producer = _producer(
        v1_layers={target: replace(_v1(*target), justified=False)},
        abc_layers={target: _abc(*target)},
    )
    result = producer.produce((target,))
    assert not result.complete
    assert _codes(result) == {DiagnosticCode.V1_UNJUSTIFIED}


def test_duplicate_normalized_unit_fails_closed() -> None:
    first = ("São Fixture", "concurso_publico")
    duplicate = ("Sao Fixture", "concurso_publico")
    producer = _producer(
        v1_layers={first: _v1(*first)},
        abc_layers={first: _abc(*first)},
    )
    result = producer.produce((first, duplicate))
    assert not result.complete
    assert DiagnosticCode.DUPLICATE_UNIT in _codes(result)
    assert result.corpus is None


def test_invalid_citation_fails_closed() -> None:
    target = TARGETS[0]
    abc = _abc(*target)
    bad = replace(abc.citations[0], quote="not in snapshot")
    producer = _producer(
        v1_layers={target: _v1(*target)},
        abc_layers={target: replace(abc, citations=(bad,))},
    )
    result = producer.produce((target,))
    assert not result.complete
    assert DiagnosticCode.INVALID_CITATION in _codes(result)


def test_secret_in_contractual_evidence_aborts_without_redaction(tmp_path: Path) -> None:
    target = TARGETS[0]
    abc = _abc(*target)
    secret = "Authorization: Bearer abcdefghijklmnop"
    citation = CitationLayer("main", 0, len(secret), secret)
    secret_abc = replace(
        abc,
        sources=(replace(abc.sources[0], content=secret),),
        citations=(citation,),
        proposal_a=replace(abc.proposal_a, citations=(citation,)),
        proposal_b=replace(abc.proposal_b, citations=(citation,)),
    )
    destination = tmp_path / "secret.json"
    producer = _producer(
        v1_layers={target: _v1(*target)},
        abc_layers={target: secret_abc},
    )
    result = producer.produce_and_publish(
        (target,), destination=destination, golden_path=tmp_path / "unused.csv"
    )
    assert not result.complete
    assert DiagnosticCode.SECRET_DETECTED in _codes(result)
    assert not destination.exists()


def test_atomic_replay_failure_preserves_previous_destination_and_cleans_temp(
    tmp_path: Path,
) -> None:
    golden = tmp_path / "golden.csv"
    destination = tmp_path / "cassette.json"
    previous = b"previous-cassette\n"
    _write_golden(golden)
    destination.write_bytes(previous)
    target = TARGETS[0]
    producer = _producer(
        v1_layers={target: _v1(*target)},
        abc_layers={target: _abc(*target)},
    )

    with pytest.raises(ReplayEvidenceError, match="processo_seletivo"):
        producer.produce_and_publish(
            (target,), destination=destination, golden_path=golden
        )

    assert destination.read_bytes() == previous
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []
