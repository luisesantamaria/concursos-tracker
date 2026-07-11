"""Fail-closed producer for the existing golden replay schema.

This module performs no network or model I/O.  Callers inject the V1 and A/B/C
providers; publication is allowed only after the complete in-memory corpus has
passed the existing JSON adapter and the existing offline replay path.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios.v2.agents import (
    DecisionProposal,
    ProposalValidationError,
)
from scripts.fase2_municipios.v2.eval.golden_runner import (
    SCHEMA_VERSION,
    GoldenDifferentialRunner,
    JsonReplayFetchAdapter,
)
from scripts.fase2_municipios.v2.snapshot import (
    CitationVerificationError,
    EvidenceSource as SnapshotEvidenceSource,
    SnapshotError,
    anchor_citation,
    build_snapshot,
    verify_all,
)


TargetUnit = tuple[str, str]
VALID_BUCKETS = frozenset({"concurso_publico", "processo_seletivo"})
CONFIRMING_DECISIONS = frozenset({
    "indice_oficial",
    "indice_oficial_combinado",
    "portal_externo_oficial",
})


class DiagnosticCode(str, Enum):
    MISSING_V1 = "MISSING_V1"
    V1_UNJUSTIFIED = "V1_UNJUSTIFIED"
    MISSING_A = "MISSING_A"
    MISSING_B = "MISSING_B"
    MISSING_C = "MISSING_C"
    INVALID_CITATION = "INVALID_CITATION"
    SECRET_DETECTED = "SECRET_DETECTED"
    DUPLICATE_UNIT = "DUPLICATE_UNIT"
    INVALID_TARGET = "INVALID_TARGET"
    INVALID_V2_EVIDENCE = "INVALID_V2_EVIDENCE"
    INVALID_CANDIDATE = "INVALID_CANDIDATE"


@dataclass(frozen=True)
class Diagnostic:
    unit: TargetUnit
    code: DiagnosticCode


@dataclass(frozen=True)
class EvidenceLayer:
    snapshot_ref: str
    authority: str
    identity: str
    reason: str


@dataclass(frozen=True)
class V1Layer:
    decision: str
    url: str
    evidence: EvidenceLayer
    justified: bool = True


@dataclass(frozen=True)
class SourceLayer:
    source_id: str
    url: str
    retrieved_at: str
    content: str


@dataclass(frozen=True)
class CitationLayer:
    source_id: str
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class CandidateLayer:
    candidate_id: str
    url: str
    decision: str
    bucket: str
    authority: str
    identity: str
    evidence_state: str
    source_kind: str


@dataclass(frozen=True)
class ProposalLayer:
    decision: str
    bucket: str
    candidate_id: str
    resource_url: str
    citations: tuple[CitationLayer, ...]
    reason: str


@dataclass(frozen=True)
class ABCLayer:
    evidence: EvidenceLayer | None
    sources: tuple[SourceLayer, ...]
    citations: tuple[CitationLayer, ...]
    candidate: CandidateLayer | None
    proposal_a: ProposalLayer | None
    proposal_b: ProposalLayer | None
    judge_response: Mapping[str, Any] | None


class V1Source(Protocol):
    def get(self, municipio: str, bucket: str) -> V1Layer | None: ...


class ABCProvider(Protocol):
    def get(self, municipio: str, bucket: str) -> ABCLayer | None: ...


@dataclass(frozen=True)
class ProducerResult:
    complete: bool
    units_ok: tuple[TargetUnit, ...]
    diagnostics: tuple[Diagnostic, ...]
    corpus: Mapping[str, Any] | None = field(default=None, repr=False)


class ExternalAccessBlocked(RuntimeError):
    """A test/runtime guard blocked an external call; never convert to absence."""


class IncompleteCorpusError(ValueError):
    """Publication was requested for an incomplete producer result."""


class CallableABCProvider:
    """Injection seam for a future live V2 pass; it performs no I/O itself."""

    def __init__(
        self, callback: Callable[[str, str], ABCLayer | None]
    ) -> None:
        self._callback = callback

    def get(self, municipio: str, bucket: str) -> ABCLayer | None:
        return self._callback(municipio, bucket)


class Run497V1Source:
    """Read schema-compatible V1 facts from a configurable run497 directory.

    Historical records that do not already justify every schema-1 V1 field are
    returned with ``justified=False``.  No decision, authority or identity is
    inferred from transformed text.
    """

    _BUCKET_MAP = {
        "concursos": "concurso_publico",
        "processos": "processo_seletivo",
        "concurso_publico": "concurso_publico",
        "processo_seletivo": "processo_seletivo",
    }

    def __init__(self, corpus_dir: Path) -> None:
        self.corpus_dir = Path(corpus_dir)
        self._records: dict[tuple[str, str], Mapping[str, Any]] | None = None

    def _load(self) -> dict[tuple[str, str], Mapping[str, Any]]:
        records: dict[tuple[str, str], Mapping[str, Any]] = {}
        for path in sorted(self.corpus_dir.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                continue
            municipio = raw.get("municipio")
            bucket = self._BUCKET_MAP.get(raw.get("bucket"))
            if not isinstance(municipio, str) or not municipio or bucket is None:
                continue
            key = (golden_evaluator.muni_key(municipio), bucket)
            if key in records:
                raise ValueError("DUPLICATE_UNIT")
            records[key] = dict(raw)
        return records

    def get(self, municipio: str, bucket: str) -> V1Layer | None:
        if self._records is None:
            self._records = self._load()
        raw = self._records.get((golden_evaluator.muni_key(municipio), bucket))
        if raw is None:
            return None
        evidence_raw = raw.get("evidence")
        evidence = EvidenceLayer(
            snapshot_ref=(
                str(evidence_raw.get("snapshot_ref", ""))
                if isinstance(evidence_raw, Mapping) else ""
            ),
            authority=(
                str(evidence_raw.get("authority", ""))
                if isinstance(evidence_raw, Mapping) else ""
            ),
            identity=(
                str(evidence_raw.get("identity", ""))
                if isinstance(evidence_raw, Mapping) else ""
            ),
            reason=(
                str(evidence_raw.get("reason", ""))
                if isinstance(evidence_raw, Mapping) else ""
            ),
        )
        decision = raw.get("decision")
        url = raw.get("url")
        justified = (
            isinstance(decision, str)
            and _valid_discrete_decision(decision)
            and isinstance(url, str)
            and _evidence_complete(evidence)
        )
        return V1Layer(
            decision=decision if isinstance(decision, str) else "",
            url=url if isinstance(url, str) else "",
            evidence=evidence,
            justified=justified,
        )


_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}"
    ),
)


def _contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    if isinstance(value, Mapping):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(_contains_secret(getattr(value, item.name)) for item in fields(value))
    return False


def redact_metadata(value: Any) -> Any:
    """Deterministically redact non-contractual metadata only."""
    if isinstance(value, Mapping):
        redacted = {}
        for key in sorted(value, key=str):
            normalized = str(key).lower().replace("_", "-")
            if normalized in {
                "authorization", "proxy-authorization", "cookie", "set-cookie",
                "api-key", "apikey", "access-token", "auth-token", "token", "secret",
            }:
                redacted[str(key)] = f"<REDACTED:{normalized}>"
            else:
                redacted[str(key)] = redact_metadata(value[key])
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_metadata(item) for item in value]
    return value


def _valid_discrete_decision(value: str) -> bool:
    return (
        value in CONFIRMING_DECISIONS
        or value in {"revisar", "nao_encontrado"}
        or value.endswith(("_rechazado", "_rechazada"))
    )


def _evidence_complete(evidence: EvidenceLayer | None) -> bool:
    return evidence is not None and all(
        isinstance(value, str) and bool(value)
        for value in (
            evidence.snapshot_ref,
            evidence.authority,
            evidence.identity,
            evidence.reason,
        )
    )


def _citation_mapping(citation: CitationLayer) -> dict[str, Any]:
    return {
        "source_id": citation.source_id,
        "start": citation.start,
        "end": citation.end,
        "quote": citation.quote,
    }


def _proposal_mapping(proposal: ProposalLayer) -> dict[str, Any]:
    return {
        "decision": proposal.decision,
        "bucket": proposal.bucket,
        "candidate_id": proposal.candidate_id,
        "resource_url": proposal.resource_url,
        "citations": [_citation_mapping(item) for item in proposal.citations],
        "reason": proposal.reason,
    }


def _v1_mapping(layer: V1Layer) -> dict[str, Any]:
    return {
        "decision": layer.decision,
        "url": layer.url,
        "evidence": {
            "snapshot_ref": layer.evidence.snapshot_ref,
            "authority": layer.evidence.authority,
            "identity": layer.evidence.identity,
            "reason": layer.evidence.reason,
        },
    }


def _abc_mapping(layer: ABCLayer) -> dict[str, Any]:
    assert layer.evidence is not None
    return {
        "evidence": {
            "snapshot_ref": layer.evidence.snapshot_ref,
            "authority": layer.evidence.authority,
            "identity": layer.evidence.identity,
            "reason": layer.evidence.reason,
            "sources": [
                {
                    "source_id": item.source_id,
                    "url": item.url,
                    "retrieved_at": item.retrieved_at,
                    "content": item.content,
                }
                for item in sorted(layer.sources, key=lambda item: item.source_id)
            ],
        },
        "citations": [
            _citation_mapping(item)
            for item in sorted(
                layer.citations,
                key=lambda item: (item.source_id, item.start, item.end, item.quote),
            )
        ],
        "candidate": (
            {
                "candidate_id": layer.candidate.candidate_id,
                "url": layer.candidate.url,
                "decision": layer.candidate.decision,
                "bucket": layer.candidate.bucket,
                "authority": layer.candidate.authority,
                "identity": layer.candidate.identity,
                "evidence_state": layer.candidate.evidence_state,
                "source_kind": layer.candidate.source_kind,
            }
            if layer.candidate is not None else None
        ),
        "proposal_a": _proposal_mapping(layer.proposal_a),  # type: ignore[arg-type]
        "proposal_b": _proposal_mapping(layer.proposal_b),  # type: ignore[arg-type]
        "judge_response": dict(layer.judge_response or {}),
    }


def canonical_corpus_bytes(corpus: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            corpus,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


class CassetteProducer:
    def __init__(self, *, v1_source: V1Source, abc_provider: ABCProvider) -> None:
        self.v1_source = v1_source
        self.abc_provider = abc_provider

    @staticmethod
    def _provider_get(provider, municipio: str, bucket: str):
        try:
            return provider.get(municipio, bucket)
        except ExternalAccessBlocked:
            raise
        except Exception:
            return None

    @staticmethod
    def _v1_diagnostics(unit: TargetUnit, layer: V1Layer | None) -> list[Diagnostic]:
        if layer is None:
            return [Diagnostic(unit, DiagnosticCode.MISSING_V1)]
        if (
            not layer.justified
            or not isinstance(layer.decision, str)
            or not _valid_discrete_decision(layer.decision)
            or not isinstance(layer.url, str)
            or (layer.decision in CONFIRMING_DECISIONS and not layer.url)
            or not _evidence_complete(layer.evidence)
        ):
            return [Diagnostic(unit, DiagnosticCode.V1_UNJUSTIFIED)]
        return []

    @staticmethod
    def _snapshot(layer: ABCLayer):
        sources = []
        for item in layer.sources:
            retrieved_at = datetime.fromisoformat(item.retrieved_at)
            sources.append(SnapshotEvidenceSource(
                source_id=item.source_id,
                url=item.url,
                retrieved_at=retrieved_at,
                content=item.content,
            ))
        return build_snapshot(sources)

    @staticmethod
    def _citations_valid(snapshot, citations: tuple[CitationLayer, ...]) -> bool:
        try:
            anchored = tuple(
                anchor_citation(snapshot, _citation_mapping(item), require_offsets=True)
                for item in citations
            )
            verify_all(snapshot, anchored)
        except (CitationVerificationError, SnapshotError, TypeError, ValueError):
            return False
        return True

    @classmethod
    def _abc_diagnostics(
        cls, unit: TargetUnit, layer: ABCLayer | None
    ) -> list[Diagnostic]:
        if layer is None:
            return [
                Diagnostic(unit, DiagnosticCode.MISSING_A),
                Diagnostic(unit, DiagnosticCode.MISSING_B),
                Diagnostic(unit, DiagnosticCode.MISSING_C),
            ]

        diagnostics: list[Diagnostic] = []
        if not _evidence_complete(layer.evidence) or not layer.sources:
            diagnostics.append(Diagnostic(unit, DiagnosticCode.INVALID_V2_EVIDENCE))
            snapshot = None
        else:
            try:
                snapshot = cls._snapshot(layer)
            except (KeyError, TypeError, ValueError, SnapshotError):
                snapshot = None
                diagnostics.append(Diagnostic(unit, DiagnosticCode.INVALID_V2_EVIDENCE))

        if layer.proposal_a is None:
            diagnostics.append(Diagnostic(unit, DiagnosticCode.MISSING_A))
        else:
            try:
                DecisionProposal.from_mapping(_proposal_mapping(layer.proposal_a))
            except ProposalValidationError:
                diagnostics.append(Diagnostic(unit, DiagnosticCode.MISSING_A))

        if layer.proposal_b is None:
            diagnostics.append(Diagnostic(unit, DiagnosticCode.MISSING_B))
        else:
            try:
                DecisionProposal.from_mapping(_proposal_mapping(layer.proposal_b))
            except ProposalValidationError:
                diagnostics.append(Diagnostic(unit, DiagnosticCode.MISSING_B))

        judge = layer.judge_response
        if (
            not isinstance(judge, Mapping)
            or set(judge) != {"decision", "reason"}
            or judge.get("decision") not in {"aceptar_A", "aceptar_B", "revisar"}
            or not isinstance(judge.get("reason"), str)
            or not judge.get("reason")
        ):
            diagnostics.append(Diagnostic(unit, DiagnosticCode.MISSING_C))

        if snapshot is not None:
            citation_groups = [layer.citations]
            if layer.proposal_a is not None:
                citation_groups.append(layer.proposal_a.citations)
            if layer.proposal_b is not None:
                citation_groups.append(layer.proposal_b.citations)
            if any(not cls._citations_valid(snapshot, group) for group in citation_groups):
                diagnostics.append(Diagnostic(unit, DiagnosticCode.INVALID_CITATION))

        confirming = any(
            proposal is not None and proposal.decision in CONFIRMING_DECISIONS
            for proposal in (layer.proposal_a, layer.proposal_b)
        )
        if confirming and (
            layer.candidate is None
            or not all(
                isinstance(value, str) and bool(value)
                for value in (
                    layer.candidate.candidate_id,
                    layer.candidate.url,
                    layer.candidate.decision,
                    layer.candidate.bucket,
                    layer.candidate.authority,
                    layer.candidate.identity,
                    layer.candidate.evidence_state,
                    layer.candidate.source_kind,
                )
            )
        ):
            diagnostics.append(Diagnostic(unit, DiagnosticCode.INVALID_CANDIDATE))

        contractual_values = {
            "evidence": layer.evidence,
            "sources": layer.sources,
            "citations": layer.citations,
            "candidate": layer.candidate,
            "proposal_a": layer.proposal_a,
            "proposal_b": layer.proposal_b,
            "judge_response": layer.judge_response,
        }
        if _contains_secret(contractual_values):
            diagnostics.append(Diagnostic(unit, DiagnosticCode.SECRET_DETECTED))

        return diagnostics

    def produce(self, targets: Iterable[TargetUnit]) -> ProducerResult:
        materialized = list(targets)
        diagnostics: list[Diagnostic] = []
        unique: dict[tuple[str, str], TargetUnit] = {}

        for raw in materialized:
            if (
                not isinstance(raw, tuple)
                or len(raw) != 2
                or not all(isinstance(value, str) and bool(value) for value in raw)
                or raw[1] not in VALID_BUCKETS
            ):
                unit = raw if isinstance(raw, tuple) and len(raw) == 2 else ("", "")
                diagnostics.append(Diagnostic(unit, DiagnosticCode.INVALID_TARGET))
                continue
            key = (golden_evaluator.muni_key(raw[0]), raw[1])
            if key in unique:
                diagnostics.append(Diagnostic(raw, DiagnosticCode.DUPLICATE_UNIT))
            else:
                unique[key] = raw

        cases: list[dict[str, Any]] = []
        units_ok: list[TargetUnit] = []
        for key in sorted(unique):
            unit = unique[key]
            municipio, bucket = unit
            v1 = self._provider_get(self.v1_source, municipio, bucket)
            abc = self._provider_get(self.abc_provider, municipio, bucket)
            unit_diagnostics = self._v1_diagnostics(unit, v1)
            unit_diagnostics.extend(self._abc_diagnostics(unit, abc))
            if v1 is not None and _contains_secret(_v1_mapping(v1)):
                unit_diagnostics.append(Diagnostic(unit, DiagnosticCode.SECRET_DETECTED))
            diagnostics.extend(unit_diagnostics)
            if unit_diagnostics:
                continue
            assert v1 is not None and abc is not None
            cases.append({
                "municipio": municipio,
                "bucket": bucket,
                "v1": _v1_mapping(v1),
                "v2": _abc_mapping(abc),
            })
            units_ok.append(unit)

        diagnostics.sort(key=lambda item: (
            golden_evaluator.muni_key(item.unit[0]), item.unit[1], item.code.value
        ))
        complete = not diagnostics and len(cases) == len(materialized)
        corpus = {"schema_version": SCHEMA_VERSION, "cases": cases} if complete else None
        return ProducerResult(
            complete=complete,
            units_ok=tuple(units_ok),
            diagnostics=tuple(diagnostics),
            corpus=corpus,
        )

    def publish(
        self,
        result: ProducerResult,
        *,
        destination: Path,
        golden_path: Path,
    ) -> None:
        if not result.complete or result.corpus is None:
            raise IncompleteCorpusError("INCOMPLETE_CORPUS")

        destination = Path(destination)
        golden_path = Path(golden_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            payload = canonical_corpus_bytes(result.corpus)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            JsonReplayFetchAdapter(temporary).cases()
            GoldenDifferentialRunner().run_replay(
                golden_path=golden_path,
                corpus_path=temporary,
            )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def produce_and_publish(
        self,
        targets: Iterable[TargetUnit],
        *,
        destination: Path,
        golden_path: Path,
    ) -> ProducerResult:
        result = self.produce(targets)
        if result.complete:
            self.publish(result, destination=destination, golden_path=golden_path)
        return result


__all__ = [
    "ABCProvider",
    "ABCLayer",
    "CallableABCProvider",
    "CandidateLayer",
    "CassetteProducer",
    "CitationLayer",
    "Diagnostic",
    "DiagnosticCode",
    "EvidenceLayer",
    "ExternalAccessBlocked",
    "IncompleteCorpusError",
    "ProducerResult",
    "ProposalLayer",
    "Run497V1Source",
    "SourceLayer",
    "TargetUnit",
    "V1Layer",
    "V1Source",
    "canonical_corpus_bytes",
    "redact_metadata",
]
