"""Turnkey golden live runner: directed V2 A/B/C, cassette, and replay.

The CLI never derives fetch targets from the golden answers. Orion supplies a
separate CSV mapping and a complete, justified V1 corpus. All outputs are
restricted to ``staging/fase2_v2/eval``; incomplete units leave only an audit
artifact and never publish a partial schema-1 cassette.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import io
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios.v2.eval.cassette_producer import (
    ABCLayer,
    CandidateLayer,
    CassetteProducer,
    CitationLayer,
    EvidenceLayer,
    ProposalLayer,
    Run497V1Source,
    SourceLayer,
)
from scripts.fase2_municipios.v2.eval.coverage_schema import (
    SinCoberturaV1Unit,
    canonical_sin_cobertura_v1,
    coverage_summary,
)
from scripts.fase2_municipios.v2.eval.golden_runner import (
    BUCKET_COLUMNS,
    GoldenDifferentialRunner,
    LiveContract,
    _golden_expectation,
    canonical_json_bytes,
    compare_to_golden,
    derived_csv_bytes,
    run_live,
)
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCAdapter,
    LiveABCOutcome,
    LiveAuditEvent,
    LiveCause,
    LiveCauseKind,
    ModelResponseValidationError,
    OrionHTTPFetcher,
    RenderFallbackFetcher,
)
from scripts.fase2_municipios.v2.agents.orchestration import ProposalValidationError
from scripts.fase2_municipios.v2.eval.live_model_policy import (
    CredentialConfigError,
    ErrorCategory,
    classify_error,
    load_model_credentials,
)
from scripts.fase2_municipios.v2.eval.live_observability import (
    IncompleteRunTracker,
    StageArtifactWriter,
)
from scripts.fase2_municipios.v2.eval.live_runtime import (
    EventLogger,
    LiveRunState,
    RunnerLock,
    RunnerLockError,
    atomic_durable_write,
    normalize_unit,
)
from scripts.fase2_municipios.v2.gemini import (
    GeminiClientError,
    RoleModels,
    gentle_free_only_environment,
    resolve_free_api_key,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
CANONICAL_STAGING_ROOT = REPO_ROOT / "staging" / "fase2_v2" / "eval"
URL_MAP_COLUMNS = ("municipio", "bucket", "url")
VALID_UNIT_BUCKETS = frozenset(bucket for bucket, _main, _extra in BUCKET_COLUMNS)
FINAL_FILENAMES = (
    "golden_cassette.schema1.json",
    "differential.json",
    "differential.csv",
    "flips.json",
)
AUDIT_FILENAME = "live_audit.json"
# --no-v1-differential output: deliberately NOT named/shaped like the schema-1
# cassette (FINAL_FILENAMES) -- this mode never produces a V1-justified cassette.
V2_ONLY_FILENAMES = (
    "v2_only_differential.json",
    "v2_only_differential.csv",
)
V2_ONLY_MODE = "v2_only_no_v1_differential"
V1_DIFFERENTIAL_MODE = "v1_differential"
# In --no-v1-differential the url_map IS the scope fixture: golden units with
# no url_map row are excluded the same way sin_cobertura_v1 excludes them, but
# coverage_schema.py is frozen (motivo can only be "sin_cobertura_v1"), so this
# mode reuses that exact label and documents the reinterpretation in the audit.
FUERA_DE_URL_MAP_NOTE = (
    "en modo v2_only_no_v1_differential, motivo=sin_cobertura_v1 significa "
    "'fuera del fixture url_map' (el golden no tiene fila en --url-map), no "
    "ausencia de V1"
)


class GoldenLiveError(RuntimeError):
    """Secret-free turnkey execution failure."""


class GoldenLiveInputError(GoldenLiveError):
    """An explicit input is missing, ambiguous, or outside its contract."""


class GoldenLiveIncompleteError(GoldenLiveError):
    """At least one unit failed closed; no cassette/differential was published."""


@dataclass(frozen=True)
class GoldenLiveArtifacts:
    output_dir: Path
    # None in --no-v1-differential: FINAL_FILENAMES (schema-1 cassette,
    # V1/V2 differential, flips) are never written without a real V1 corpus.
    cassette: Path | None
    differential_json: Path | None
    differential_csv: Path | None
    flips: Path | None
    audit: Path
    coverage: Mapping[str, int]
    sin_cobertura_v1: tuple[SinCoberturaV1Unit, ...]
    telemetry: Mapping[str, Any]
    # Populated only in --no-v1-differential.
    v2_only_differential_json: Path | None = None
    v2_only_differential_csv: Path | None = None


@dataclass(frozen=True)
class GoldenTargetCoverage:
    total: int
    covered: tuple[tuple[str, str], ...]
    target_urls: Mapping[tuple[str, str], str]
    sin_cobertura_v1: tuple[SinCoberturaV1Unit, ...]

    @property
    def summary(self) -> dict[str, int]:
        return coverage_summary(
            total=self.total,
            covered=len(self.covered),
            sin_cobertura_v1=len(self.sin_cobertura_v1),
        )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _checked_output_dir(
    output_dir: Path,
    staging_root: Path,
    *,
    resume: bool = False,
    final_filenames: tuple[str, ...] = FINAL_FILENAMES,
) -> Path:
    root = Path(staging_root).resolve()
    destination = Path(output_dir).resolve()
    if not _is_relative_to(destination, root):
        raise GoldenLiveInputError("output_dir_must_be_inside_staging_root")
    protected = final_filenames if resume else (*final_filenames, AUDIT_FILENAME)
    for filename in protected:
        if (destination / filename).exists():
            raise GoldenLiveInputError(f"output_artifact_already_exists:{filename}")
    return destination


def _atomic_write(path: Path, payload: bytes) -> None:
    atomic_durable_write(path, payload)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_manifest_sha256(path: Path) -> str:
    root = Path(path)
    entries = [
        [item.relative_to(root).as_posix(), _file_sha256(item)]
        for item in sorted(root.glob("*.json"))
        if item.is_file()
    ]
    encoded = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def golden_targets(golden_path: Path) -> tuple[tuple[str, str], ...]:
    rows = golden_evaluator.read_csv(Path(golden_path))
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    municipality_names: dict[str, str] = {}
    for row in rows:
        municipio = golden_evaluator.get(row, "municipio")
        if not municipio:
            raise GoldenLiveInputError("golden_row_without_municipio")
        normalized_municipio = golden_evaluator.muni_key(municipio)
        previous_name = municipality_names.get(normalized_municipio)
        if previous_name is not None and previous_name != municipio:
            raise GoldenLiveInputError(f"muni_key_collision:{normalized_municipio}")
        municipality_names[normalized_municipio] = municipio
        for bucket, _main, _extra in BUCKET_COLUMNS:
            key = (normalized_municipio, bucket)
            if key in seen:
                # The normalized tuple is the unit.  A repeated source row does
                # not create another unit or progress entry.
                continue
            seen.add(key)
            targets.append((municipio, bucket))
    if not targets:
        raise GoldenLiveInputError("golden_has_no_targets")
    return tuple(targets)


def parse_unit_specs(specs: list[str] | tuple[str, ...] | None) -> tuple[tuple[str, str], ...]:
    """Parse repeatable ``Municipio:bucket`` values without inventing units."""

    if not specs:
        return ()
    parsed: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, spec in enumerate(specs, start=1):
        if not isinstance(spec, str) or ":" not in spec:
            raise GoldenLiveInputError(f"invalid_unit_spec_at:{position}")
        municipio, bucket = (item.strip() for item in spec.rsplit(":", 1))
        if not municipio or bucket not in VALID_UNIT_BUCKETS:
            raise GoldenLiveInputError(f"invalid_unit_spec_at:{position}")
        key = (golden_evaluator.muni_key(municipio), bucket)
        if key in seen:
            raise GoldenLiveInputError(f"duplicate_unit_spec_at:{position}")
        seen.add(key)
        parsed.append((municipio, bucket))
    return tuple(parsed)


def filter_golden_targets(
    targets: tuple[tuple[str, str], ...],
    unit_allowlist: tuple[tuple[str, str], ...] | None,
) -> tuple[tuple[str, str], ...]:
    """Resolve an explicit unit allowlist against the canonical golden universe."""

    if not unit_allowlist:
        return targets
    available = {
        (golden_evaluator.muni_key(municipio), bucket): (municipio, bucket)
        for municipio, bucket in targets
    }
    selected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for municipio, bucket in unit_allowlist:
        if bucket not in VALID_UNIT_BUCKETS:
            raise GoldenLiveInputError(f"requested_unit_invalid_bucket:{bucket}")
        key = (golden_evaluator.muni_key(municipio), bucket)
        if key in seen:
            raise GoldenLiveInputError(
                f"requested_unit_duplicate:{municipio}:{bucket}"
            )
        seen.add(key)
        resolved = available.get(key)
        if resolved is None:
            raise GoldenLiveInputError(
                f"requested_unit_not_in_golden:{municipio}:{bucket}"
            )
        selected.append(resolved)
    return tuple(selected)


def load_url_map(
    path: Path,
    targets: tuple[tuple[str, str], ...],
    *,
    allow_sin_cobertura_v1: bool = False,
    universe_targets: tuple[tuple[str, str], ...] | None = None,
) -> GoldenTargetCoverage:
    expected = {
        (golden_evaluator.muni_key(municipio), bucket): (municipio, bucket)
        for municipio, bucket in targets
    }
    universe = {
        (golden_evaluator.muni_key(municipio), bucket): (municipio, bucket)
        for municipio, bucket in (universe_targets or targets)
    }
    supplied: dict[tuple[str, str], str] = {}
    seen_supplied: set[tuple[str, str]] = set()
    municipality_names: dict[str, str] = {}
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != URL_MAP_COLUMNS:
                raise GoldenLiveInputError(
                    "url_map_columns_must_be:municipio,bucket,url"
                )
            for row_number, row in enumerate(reader, start=2):
                municipio = (row.get("municipio") or "").strip()
                bucket = (row.get("bucket") or "").strip()
                url = (row.get("url") or "").strip()
                normalized_municipio = golden_evaluator.muni_key(municipio)
                previous_name = municipality_names.get(normalized_municipio)
                if previous_name is not None and previous_name != municipio:
                    raise GoldenLiveInputError(
                        f"muni_key_collision:{normalized_municipio}"
                    )
                municipality_names[normalized_municipio] = municipio
                key = (normalized_municipio, bucket)
                if key not in universe:
                    raise GoldenLiveInputError(
                        f"unexpected_url_map_unit_at_row:{row_number}"
                    )
                if key in seen_supplied:
                    raise GoldenLiveInputError(
                        f"duplicate_url_map_unit_at_row:{row_number}"
                    )
                seen_supplied.add(key)
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise GoldenLiveInputError(
                        f"invalid_url_map_url_at_row:{row_number}"
                    )
                if key in expected:
                    supplied[key] = url
    except (OSError, UnicodeError, csv.Error) as exc:
        raise GoldenLiveInputError("url_map_unreadable") from exc
    missing = sorted(set(expected) - set(supplied))
    if missing and universe_targets is not None and not allow_sin_cobertura_v1:
        municipio, bucket = expected[missing[0]]
        raise GoldenLiveInputError(
            f"requested_unit_missing_from_url_map:{municipio}:{bucket}"
        )
    if missing and not allow_sin_cobertura_v1:
        raise GoldenLiveInputError(f"url_map_missing_units:{len(missing)}")
    covered = tuple(
        target
        for target in targets
        if (golden_evaluator.muni_key(target[0]), target[1]) in supplied
    )
    if allow_sin_cobertura_v1 and not covered:
        raise GoldenLiveInputError("no_covered_units")
    exclusions = canonical_sin_cobertura_v1(
        SinCoberturaV1Unit(*expected[key]) for key in missing
    )
    return GoldenTargetCoverage(
        total=len(targets),
        covered=covered,
        target_urls={
            target: supplied[(golden_evaluator.muni_key(target[0]), target[1])]
            for target in covered
        },
        sin_cobertura_v1=exclusions,
    )


def _outcome_audit(outcome: LiveABCOutcome) -> dict[str, Any]:
    return {
        "municipio": outcome.municipio,
        "bucket": outcome.bucket,
        "decision": outcome.decision,
        "url": outcome.url,
        "cause": {
            "kind": outcome.cause.kind.value,
            "code": outcome.cause.code,
            "comment": outcome.cause.comment,
            "revisar_por": outcome.cause.revisar_por,
        },
        "layer_complete": outcome.layer is not None,
        "exception_type": (
            type(outcome.original_exception).__name__
            if outcome.original_exception is not None
            else None
        ),
        "events": [
            {"phase": event.phase, "errors": list(event.errors)}
            for event in outcome.audit_events
        ],
    }


def _citation_mapping(item: CitationLayer) -> dict[str, Any]:
    return {
        "source_id": item.source_id,
        "start": item.start,
        "end": item.end,
        "quote": item.quote,
    }


def _proposal_mapping(item: ProposalLayer | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "decision": item.decision,
        "bucket": item.bucket,
        "candidate_id": item.candidate_id,
        "resource_url": item.resource_url,
        "citations": [_citation_mapping(citation) for citation in item.citations],
        "reason": item.reason,
    }


def _layer_mapping(layer: ABCLayer | None) -> dict[str, Any] | None:
    if layer is None:
        return None
    return {
        "evidence": (
            {
                "snapshot_ref": layer.evidence.snapshot_ref,
                "authority": layer.evidence.authority,
                "identity": layer.evidence.identity,
                "reason": layer.evidence.reason,
            }
            if layer.evidence is not None else None
        ),
        "sources": [
            {
                "source_id": source.source_id,
                "url": source.url,
                "retrieved_at": source.retrieved_at,
                "content": source.content,
            }
            for source in layer.sources
        ],
        "citations": [_citation_mapping(item) for item in layer.citations],
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
        "proposal_a": _proposal_mapping(layer.proposal_a),
        "proposal_b": _proposal_mapping(layer.proposal_b),
        "judge_response": dict(layer.judge_response) if layer.judge_response is not None else None,
    }


def _citation_from_mapping(item: Mapping[str, Any]) -> CitationLayer:
    return CitationLayer(
        source_id=item["source_id"], start=item["start"],
        end=item["end"], quote=item["quote"],
    )


def _proposal_from_mapping(item: Mapping[str, Any] | None) -> ProposalLayer | None:
    if item is None:
        return None
    return ProposalLayer(
        decision=item["decision"], bucket=item["bucket"],
        candidate_id=item["candidate_id"], resource_url=item["resource_url"],
        citations=tuple(_citation_from_mapping(value) for value in item["citations"]),
        reason=item["reason"],
    )


def _layer_from_mapping(raw: Mapping[str, Any] | None) -> ABCLayer | None:
    if raw is None:
        return None
    evidence_raw = raw.get("evidence")
    candidate_raw = raw.get("candidate")
    return ABCLayer(
        evidence=(EvidenceLayer(**evidence_raw) if isinstance(evidence_raw, Mapping) else None),
        sources=tuple(SourceLayer(**item) for item in raw.get("sources", ())),
        citations=tuple(_citation_from_mapping(item) for item in raw.get("citations", ())),
        candidate=(CandidateLayer(**candidate_raw) if isinstance(candidate_raw, Mapping) else None),
        proposal_a=_proposal_from_mapping(raw.get("proposal_a")),
        proposal_b=_proposal_from_mapping(raw.get("proposal_b")),
        judge_response=(dict(raw["judge_response"]) if isinstance(raw.get("judge_response"), Mapping) else None),
    )


def _persisted_outcome(outcome: LiveABCOutcome) -> dict[str, Any]:
    return {
        "municipio": outcome.municipio,
        "bucket": outcome.bucket,
        "decision": outcome.decision,
        "url": outcome.url,
        "cause": {
            "kind": outcome.cause.kind.value,
            "code": outcome.cause.code,
            "comment": outcome.cause.comment,
            "revisar_por": outcome.cause.revisar_por,
        },
        "layer": _layer_mapping(outcome.layer),
        "events": [
            {"phase": event.phase, "errors": list(event.errors)}
            for event in outcome.audit_events
        ],
    }


def _outcome_from_persisted(raw: Mapping[str, Any]) -> LiveABCOutcome:
    cause = raw["cause"]
    return LiveABCOutcome(
        municipio=raw["municipio"],
        bucket=raw["bucket"],
        decision=raw["decision"],
        url=raw["url"],
        cause=LiveCause(
            LiveCauseKind(cause["kind"]), cause["code"], cause["comment"],
            str(cause.get("revisar_por", "")),
        ),
        layer=_layer_from_mapping(raw.get("layer")),
        audit_events=tuple(
            LiveAuditEvent(item["phase"], tuple(item.get("errors", ())))
            for item in raw.get("events", ())
        ),
    )


def _snapshot_from_outcome(outcome: LiveABCOutcome) -> dict[str, Any] | None:
    municipio, bucket = normalize_unit(outcome.municipio, outcome.bucket)
    if outcome.layer is not None and outcome.layer.sources:
        sources = [
            {
                "source_id": source.source_id,
                "url": source.url,
                "retrieved_at": source.retrieved_at,
                "content": source.content,
            }
            for source in outcome.layer.sources
        ]
    elif outcome.evidence_snapshot is not None:
        sources = [
            {
                "source_id": source.source_id,
                "url": source.url,
                "retrieved_at": source.retrieved_at.isoformat(),
                "content": source.content,
            }
            for source in outcome.evidence_snapshot.sources
        ]
    else:
        return None
    return {
        "schema_version": 1,
        "unit": {"municipio": municipio, "bucket": bucket},
        "sources": sources,
    }


def _result_from_outcome(
    outcome: LiveABCOutcome,
    *,
    start: str,
    end: str,
    duration_s: float,
    attempt: int = 1,
) -> dict[str, Any]:
    layer = outcome.layer
    a = layer.proposal_a.decision if layer is not None and layer.proposal_a else ""
    b = layer.proposal_b.decision if layer is not None and layer.proposal_b else ""
    c = (
        str(layer.judge_response.get("decision", ""))
        if layer is not None and isinstance(layer.judge_response, Mapping) else ""
    )
    citation = layer.citations[0] if layer is not None and layer.citations else None
    error_class = ""
    error_message = ""
    status = "complete"
    if outcome.original_exception is not None:
        classified = classify_error(outcome.original_exception)
        error_class = classified.category.value
        error_message = type(outcome.original_exception).__name__
        status = "error"
    elif layer is None:
        cause_categories = {
            LiveCauseKind.ACCESS_FAILURE: ErrorCategory.TRANSPORT_ERROR,
            LiveCauseKind.MODEL_FAILURE: ErrorCategory.SEMANTIC_ERROR,
            LiveCauseKind.EVIDENCE_FAILURE: ErrorCategory.EVIDENCE_INSUFFICIENT,
            LiveCauseKind.DISAGREEMENT_UNRESOLVED: ErrorCategory.SEMANTIC_ERROR,
            LiveCauseKind.CONFIGURATION_FAILURE: ErrorCategory.LOCAL_BUG,
            LiveCauseKind.INTERNAL_FAILURE: ErrorCategory.LOCAL_BUG,
        }
        category = cause_categories.get(outcome.cause.kind)
        if category is not None:
            error_class = category.value
            error_message = outcome.cause.code
            status = "error"
    return {
        "status": status,
        "stage": "final",
        "model": "",
        "provider": "local",
        "start": start,
        "end": end,
        "duration_s": round(duration_s, 6),
        "attempt": attempt,
        "error_class": error_class,
        "error_message": error_message,
        "A": a,
        "B": b,
        "C": c,
        "final": outcome.decision,
        "quote": citation.quote if citation else "",
        "source_id": citation.source_id if citation else "",
        "quote_start": citation.start if citation else "",
        "quote_end": citation.end if citation else "",
        "evidence_complete": bool(layer is not None and layer.sources and layer.evidence),
        "outcome": _persisted_outcome(outcome),
    }


def _should_retry_unit(outcome: LiveABCOutcome, *, attempt: int) -> bool:
    return attempt == 1 and isinstance(
        outcome.original_exception,
        (ModelResponseValidationError, ProposalValidationError),
    )


class _ResumeAwareProvider:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.resumed: dict[tuple[str, str], LiveABCOutcome] = {}

    def add(self, outcome: LiveABCOutcome) -> None:
        self.resumed[(outcome.municipio, outcome.bucket)] = outcome

    def request(self, municipio: str, bucket: str) -> LiveABCOutcome:
        return self.resumed.get((municipio, bucket)) or self.delegate.request(municipio, bucket)

    def get(self, municipio: str, bucket: str) -> ABCLayer | None:
        return self.request(municipio, bucket).layer


def _flips_view(differential: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": differential["schema_version"],
        "coverage": differential["coverage"],
        "sin_cobertura_v1": differential["sin_cobertura_v1"],
        "flips": [
            {
                "municipio": row["municipio"],
                "bucket": row["bucket"],
                "flip_v1_v2": row["flip_v1_v2"],
                "v1_vs_golden": row["v1_vs_golden"],
                "v2_vs_golden": row["v2_vs_golden"],
            }
            for row in differential["rows"]
        ],
    }


V2_ONLY_CSV_FIELDS = (
    "municipio",
    "bucket",
    "golden_expectation",
    "golden_urls",
    "v2_decision",
    "v2_url",
    "v2_vs_golden",
    "cause_kind",
    "cause_code",
    "revisar_por",
)


def _golden_main_extra_by_unit(
    golden_path: Path,
) -> dict[tuple[str, str], tuple[str, str]]:
    """municipio/bucket -> (golden_main, golden_extra); ex-post reporting read.

    Mirrors the per-bucket lookup ``golden_targets`` builds, without deriving
    any target or decision from it -- this feeds golden columns into the V2
    reporting differential only.
    """
    rows = golden_evaluator.read_csv(Path(golden_path))
    lookup: dict[tuple[str, str], tuple[str, str]] = {}
    municipality_names: dict[str, str] = {}
    for row in rows:
        municipio = golden_evaluator.get(row, "municipio")
        if not municipio:
            raise GoldenLiveInputError("golden_row_without_municipio")
        normalized_municipio = golden_evaluator.muni_key(municipio)
        previous_name = municipality_names.get(normalized_municipio)
        if previous_name is not None and previous_name != municipio:
            raise GoldenLiveInputError(f"muni_key_collision:{normalized_municipio}")
        municipality_names[normalized_municipio] = municipio
        for bucket, main_column, extra_column in BUCKET_COLUMNS:
            lookup[(normalized_municipio, bucket)] = (
                golden_evaluator.get(row, main_column),
                golden_evaluator.get(row, extra_column),
            )
    return lookup


def write_v2_only_differential(
    *,
    targets: tuple[tuple[str, str], ...],
    outcomes: Iterable[LiveABCOutcome],
    coverage: GoldenTargetCoverage,
    golden_path: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write the V2-only-vs-golden differential for ``--no-v1-differential``.

    Pure and additive to the schema-1 cassette/differential contract: reuses
    only :func:`golden_runner.compare_to_golden` and
    :func:`golden_runner._golden_expectation`, both ex-post reporting reads
    over already-decided V2 outcomes -- never a decision input. No
    verdict_extract, no V1 coupling, no gating on V1 coverage.
    """
    outcomes_by_unit = {
        normalize_unit(outcome.municipio, outcome.bucket): outcome
        for outcome in outcomes
    }
    golden_lookup = _golden_main_extra_by_unit(golden_path)
    rows: list[dict[str, Any]] = []
    for municipio, bucket in targets:
        key = normalize_unit(municipio, bucket)
        outcome = outcomes_by_unit.get(key)
        if outcome is None:
            raise GoldenLiveInputError(
                f"missing_outcome_for_target:{municipio}:{bucket}"
            )
        golden_main, golden_extra = golden_lookup.get(key, ("", ""))
        expectation, golden_urls = _golden_expectation(golden_main, golden_extra)
        rows.append({
            "municipio": municipio,
            "bucket": bucket,
            "golden_expectation": expectation,
            "golden_urls": golden_urls,
            "v2_decision": outcome.decision,
            "v2_url": outcome.url,
            "v2_vs_golden": compare_to_golden(
                decision=outcome.decision,
                url=outcome.url,
                golden_main=golden_main,
                golden_extra=golden_extra,
            ),
            "cause_kind": outcome.cause.kind.value,
            "cause_code": outcome.cause.code,
            "revisar_por": outcome.cause.revisar_por,
        })
    rows.sort(key=lambda row: (golden_evaluator.muni_key(row["municipio"]), row["bucket"]))
    document = {
        "schema_version": 1,
        "mode": V2_ONLY_MODE,
        "coverage": coverage.summary,
        "sin_cobertura_v1": [unit.as_mapping() for unit in coverage.sin_cobertura_v1],
        "sin_cobertura_v1_note": FUERA_DE_URL_MAP_NOTE,
        "rows": rows,
    }
    destination = Path(output_dir)
    json_path = destination / V2_ONLY_FILENAMES[0]
    csv_path = destination / V2_ONLY_FILENAMES[1]
    _atomic_write(json_path, canonical_json_bytes(document))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=V2_ONLY_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "golden_urls": " | ".join(row["golden_urls"])})
    _atomic_write(csv_path, buffer.getvalue().encode("utf-8"))
    return json_path, csv_path


def run_golden_live(
    *,
    golden_path: Path,
    url_map_path: Path,
    v1_corpus_dir: Path | None = None,
    output_dir: Path,
    environ: Mapping[str, str],
    staging_root: Path = CANONICAL_STAGING_ROOT,
    http_connect_timeout: float = 10.0,
    http_read_timeout: float = 60.0,
    gemini_timeout: float = 60.0,
    seed: int = 0,
    allow_sin_cobertura_v1: bool = False,
    unit_allowlist: tuple[tuple[str, str], ...] | None = None,
    resume: bool = False,
    heartbeat_seconds: float = 30.0,
    isolate_model_calls: bool = True,
    no_v1_differential: bool = False,
    render_fallback: bool = False,
    abc_mode: str = "slim",
    fetcher_factory: Callable[[], Any] = OrionHTTPFetcher,
    adapter_factory: Callable[..., Any] = LiveABCAdapter.from_model_policy_environment,
    differential_runner_factory: Callable[..., GoldenDifferentialRunner] = GoldenDifferentialRunner,
) -> GoldenLiveArtifacts:
    # Credential policy is the first operation: no input parsing, directory
    # creation, fetch, or model construction occurs before it passes.
    free_contract_environment = {}
    if isinstance(environ.get("GEMINI_API_KEY_FREE"), str):
        free_contract_environment["GEMINI_API_KEY_FREE"] = environ["GEMINI_API_KEY_FREE"]
    resolve_free_api_key(free_contract_environment)
    destination = _checked_output_dir(
        output_dir,
        staging_root,
        resume=resume,
        final_filenames=(V2_ONLY_FILENAMES if no_v1_differential else FINAL_FILENAMES),
    )
    golden = Path(golden_path)
    url_map = Path(url_map_path)
    if not golden.is_file():
        raise GoldenLiveInputError("golden_path_not_file")
    if not url_map.is_file():
        raise GoldenLiveInputError("url_map_path_not_file")
    # --no-v1-differential opts out of V1 entirely: v1_corpus_dir (if supplied
    # anyway) is never read, never validated, never turned into a source.
    v1_dir: Path | None = None
    if not no_v1_differential:
        if v1_corpus_dir is None:
            raise GoldenLiveInputError(
                "v1_corpus_dir_required_without_no_v1_differential"
            )
        v1_dir = Path(v1_corpus_dir)
        if not v1_dir.is_dir():
            raise GoldenLiveInputError("v1_corpus_dir_not_directory")

    all_targets = golden_targets(golden)
    expected_targets = filter_golden_targets(all_targets, unit_allowlist)
    # In --no-v1-differential the url_map defines scope: golden units missing
    # a url_map row are excluded (sin_cobertura_v1), never an abort, so this
    # mode never needs --allow-sin-cobertura-v1 to reach that outcome.
    effective_allow_sin_cobertura_v1 = (
        True if no_v1_differential else allow_sin_cobertura_v1
    )
    coverage = load_url_map(
        url_map,
        expected_targets,
        allow_sin_cobertura_v1=effective_allow_sin_cobertura_v1,
        universe_targets=(all_targets if unit_allowlist else None),
    )
    if unit_allowlist and coverage.sin_cobertura_v1:
        unit = coverage.sin_cobertura_v1[0]
        raise GoldenLiveInputError(
            f"requested_unit_sin_cobertura_v1:{unit.municipio}:{unit.bucket}"
        )
    unique_targets: dict[tuple[str, str], tuple[str, str]] = {}
    for target in coverage.covered:
        unique_targets.setdefault(normalize_unit(*target), target)
    targets = tuple(unique_targets.values())

    def explicit_kwargs(factory: Callable[..., Any], optional: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parameters = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            return {}
        return {name: value for name, value in optional.items() if name in parameters}

    fetcher = fetcher_factory(**explicit_kwargs(fetcher_factory, {
        "connect_timeout_seconds": http_connect_timeout,
        "read_timeout_seconds": http_read_timeout,
    }))
    # Opt-in, additive: --render-fallback wraps whatever fetcher_factory built
    # (OrionHTTPFetcher by default, a fake in offline tests) with a single
    # headless-render pass for SPA shells / anti-bot challenges. Off by
    # default, so existing callers/tests that never pass this flag are
    # byte-for-byte unaffected.
    if render_fallback:
        fetcher = RenderFallbackFetcher(fetcher)
    adapter_arguments = {
        "fetcher": fetcher,
        "target_urls": coverage.target_urls,
        "environ": environ,
        "timeout_seconds": http_read_timeout,
    }
    adapter_arguments.update(explicit_kwargs(adapter_factory, {
        "gemini_timeout": gemini_timeout,
        "isolate_model_calls": isolate_model_calls,
        "abc_mode": abc_mode,
        "seed": seed,
    }))
    adapter = adapter_factory(**adapter_arguments)
    provider = _ResumeAwareProvider(adapter)
    models = RoleModels()
    contract = LiveContract(
        provider="gemini_free",
        certifier_model=models.certifier_model,
        prosecutor_model=models.prosecutor_model,
        judge_model=models.judge_model,
        tools=None,
        environ=free_contract_environment,
    )

    lock = RunnerLock(destination / "run_golden_live.lock", resume=resume)
    lock.acquire()
    logger: EventLogger | None = None
    artifact_redactions = tuple(
        value for name, value in environ.items()
        if name in {"GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2", "GEMINI_API_KEY"}
        and isinstance(value, str)
    )
    incomplete_tracker: IncompleteRunTracker | None = IncompleteRunTracker(
        destination, targets, redactions=artifact_redactions
    )
    try:
        logger = EventLogger(
            destination / "events.jsonl",
            redactions=tuple(
                value for name, value in environ.items()
                if name in {"GEMINI_API_KEY_FREE", "GEMINI_API_KEY_FREE_2", "GEMINI_API_KEY"}
                and isinstance(value, str)
            ),
        )
        state = LiveRunState(destination, resume=resume)
        if hasattr(adapter, "set_artifact_writer"):
            adapter.set_artifact_writer(StageArtifactWriter(
                destination, redactions=artifact_redactions
            ))
        current_unit: list[tuple[str, str]] = [("unknown", "unknown")]

        def observe(event: Mapping[str, Any]) -> None:
            municipio = str(event.get("municipio") or current_unit[0][0])
            bucket = str(event.get("bucket") or current_unit[0][1])
            logger.emit(
                municipio=municipio,
                bucket=bucket,
                stage=str(event.get("stage") or event.get("event") or "model"),
                model=str(event.get("model") or ""),
                provider=str(event.get("provider") or "local"),
                status=str(event.get("status") or event.get("event") or "ok"),
                error_class=str(event.get("error_class") or event.get("cause") or ""),
                error_message=str(event.get("error_message") or ""),
                **{
                    key: value for key, value in event.items()
                    if key not in {
                        "municipio", "bucket", "stage", "event", "model", "provider",
                        "status", "error_class", "error_message",
                    }
                },
            )

        if hasattr(adapter, "set_observer"):
            adapter.set_observer(observe)

        outcomes: list[LiveABCOutcome] = []
        run_started = time.monotonic()
        last_heartbeat = run_started
        for index, (municipio, bucket) in enumerate(targets, start=1):
            current_unit[0] = (municipio, bucket)
            if state.should_skip(municipio, bucket):
                persisted = state.load_satisfactory_result(municipio, bucket)
                outcome = _outcome_from_persisted(persisted["outcome"])
                provider.add(outcome)
                logger.emit(
                    municipio=municipio, bucket=bucket, stage="final", model="",
                    provider="checkpoint", status="skipped",
                )
            else:
                attempt = state.next_attempt(municipio, bucket)
                while True:
                    if hasattr(adapter, "set_attempt"):
                        adapter.set_attempt(attempt)
                    start_wall = datetime.now(timezone.utc)
                    start_monotonic = time.monotonic()
                    try:
                        outcome = run_live(
                            contract=contract,
                            enable_live_abc=True,
                            abc_provider=provider,
                            municipio=municipio,
                            bucket=bucket,
                        )
                        if not isinstance(outcome, LiveABCOutcome):
                            raise GoldenLiveError("live_adapter_returned_invalid_outcome")
                    except Exception as exc:
                        outcome = LiveABCAdapter._failure(
                            municipio, bucket, kind=LiveCauseKind.INTERNAL_FAILURE,
                            code=type(exc).__name__, error=exc, phase="runner",
                        )
                    end_wall = datetime.now(timezone.utc)
                    unit_result = _result_from_outcome(
                        outcome, start=start_wall.isoformat(), end=end_wall.isoformat(),
                        duration_s=time.monotonic() - start_monotonic, attempt=attempt,
                    )
                    artifact_reference = getattr(adapter, "artifact_reference", None)
                    if callable(artifact_reference):
                        reference = artifact_reference(municipio, bucket, attempt)
                        if isinstance(reference, Mapping):
                            unit_result.update({
                                key: value for key, value in reference.items()
                                if key in {"observability_path", "observability_hash"}
                                and isinstance(value, str)
                            })
                    state.record_unit(
                        municipio=municipio, bucket=bucket,
                        url=coverage.target_urls[(municipio, bucket)], result=unit_result,
                        snapshot=_snapshot_from_outcome(outcome),
                    )
                    logger.emit(
                        municipio=municipio, bucket=bucket, stage="final", model="",
                        provider="local",
                        status="ok" if unit_result["status"] == "complete" else "error",
                        error_class=unit_result["error_class"],
                        error_message=unit_result["error_message"], final=outcome.decision,
                    )
                    if not _should_retry_unit(outcome, attempt=attempt):
                        break
                    attempt += 1
                    reset_unit = getattr(adapter, "reset_unit", None)
                    if callable(reset_unit):
                        reset_unit(municipio, bucket)
            outcomes.append(outcome)
            incomplete_tracker.record_terminal(
                (municipio, bucket),
                status=(
                    "error"
                    if outcome.original_exception is not None or outcome.layer is None
                    else "complete"
                ),
                decision=outcome.decision,
                revisar_por=outcome.cause.revisar_por,
                stage=(
                    outcome.audit_events[-1].phase
                    if outcome.audit_events else "final"
                ),
                error_class=(
                    type(outcome.original_exception).__name__
                    if outcome.original_exception is not None
                    else outcome.cause.code
                ),
            )
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds or index == len(targets):
                logger.heartbeat(
                    municipio=municipio,
                    bucket=bucket,
                    completed=index,
                    total=len(targets),
                    last_stage="final",
                    elapsed_s=now - run_started,
                )
                last_heartbeat = now

        telemetry = (
            adapter.telemetry.summary()
            if getattr(adapter, "telemetry", None) is not None
            else {
                "free_calls": 0, "paid_calls": 0,
                "paid_fallback_reasons": {}, "tokens": 0,
                "quota_429": 0, "approx_rpm": 0, "approx_tpm": 0,
                "approx_rpd": 0,
                "providers": {
                    name: {"calls": 0, "tokens": 0, "errors": 0, "quota_rate": 0}
                    for name in ("gemini_free_1", "gemini_free_2", "gemini_paid")
                },
            }
        )
        audit_path = destination / AUDIT_FILENAME

        if no_v1_differential:
            # No Run497V1Source, no CassetteProducer: this mode never builds a
            # V1-gated cassette. "complete" is whether every scoped unit ran
            # exactly once -- V1 coverage/justification is not the criterion.
            producer = None
            result = None
            complete = len(outcomes) == len(targets)
            producer_diagnostics_view: list[dict[str, Any]] = []
            v1_manifest_sha256 = None
            incomplete_reason = f"units_missing={len(targets) - len(outcomes)}"
        else:
            producer = CassetteProducer(
                v1_source=Run497V1Source(v1_dir),
                abc_provider=provider,
            )
            result = producer.produce(
                targets,
                sin_cobertura_v1=coverage.sin_cobertura_v1,
            )
            complete = result.complete
            producer_diagnostics_view = [
                {
                    "municipio": diagnostic.unit[0],
                    "bucket": diagnostic.unit[1],
                    "code": diagnostic.code.value,
                }
                for diagnostic in result.diagnostics
            ]
            v1_manifest_sha256 = _directory_manifest_sha256(v1_dir)
            incomplete_reason = f"diagnostics={len(result.diagnostics)}"

        audit = {
            "schema_version": 1,
            "mode": V2_ONLY_MODE if no_v1_differential else V1_DIFFERENTIAL_MODE,
            "complete": complete,
            "coverage": coverage.summary,
            "sin_cobertura_v1": [
                unit.as_mapping() for unit in coverage.sin_cobertura_v1
            ],
            "inputs": {
                "golden_sha256": _file_sha256(golden),
                "url_map_sha256": _file_sha256(url_map),
                "v1_manifest_sha256": v1_manifest_sha256,
                "render_fallback": render_fallback,
            },
            "units": [_outcome_audit(outcome) for outcome in outcomes],
            "producer_diagnostics": producer_diagnostics_view,
            "telemetry": telemetry,
        }
        if no_v1_differential:
            audit["sin_cobertura_v1_note"] = FUERA_DE_URL_MAP_NOTE
        _atomic_write(audit_path, canonical_json_bytes(audit))
        logger.emit(
            municipio=targets[-1][0], bucket=targets[-1][1], stage="summary",
            model="", provider="local", status="ok" if complete else "error",
            **telemetry,
        )
        if not complete:
            raise GoldenLiveIncompleteError(
                f"live_corpus_incomplete:{incomplete_reason}"
            )

        if no_v1_differential:
            v2_only_json, v2_only_csv = write_v2_only_differential(
                targets=targets,
                outcomes=outcomes,
                coverage=coverage,
                golden_path=golden,
                output_dir=destination,
            )
            return GoldenLiveArtifacts(
                output_dir=destination,
                cassette=None,
                differential_json=None,
                differential_csv=None,
                flips=None,
                audit=audit_path,
                coverage=coverage.summary,
                sin_cobertura_v1=coverage.sin_cobertura_v1,
                telemetry=telemetry,
                v2_only_differential_json=v2_only_json,
                v2_only_differential_csv=v2_only_csv,
            )

        cassette_path = destination / FINAL_FILENAMES[0]
        producer.publish(
            result,
            destination=cassette_path,
            golden_path=golden,
            unit_allowlist=(targets if unit_allowlist else None),
        )
        differential = differential_runner_factory(seed=seed).run_replay(
            golden_path=golden,
            corpus_path=cassette_path,
            unit_allowlist=(targets if unit_allowlist else None),
        )
        differential_json = destination / FINAL_FILENAMES[1]
        differential_csv = destination / FINAL_FILENAMES[2]
        flips_path = destination / FINAL_FILENAMES[3]
        _atomic_write(differential_json, canonical_json_bytes(differential))
        _atomic_write(differential_csv, derived_csv_bytes(differential))
        _atomic_write(flips_path, canonical_json_bytes(_flips_view(differential)))
        return GoldenLiveArtifacts(
            output_dir=destination,
            cassette=cassette_path,
            differential_json=differential_json,
            differential_csv=differential_csv,
            flips=flips_path,
            audit=audit_path,
            coverage=coverage.summary,
            sin_cobertura_v1=coverage.sin_cobertura_v1,
            telemetry=telemetry,
        )
    finally:
        if incomplete_tracker is not None:
            incomplete_tracker.write_from_finally(sys.exception())
        if logger is not None:
            logger.close()
        lock.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observable/resumable golden live cassette and differential"
    )
    parser.add_argument("--provider", choices=("gemini_free", "gemini_policy"), required=True)
    parser.add_argument("--tools", choices=("none",), required=True)
    parser.add_argument("--grounding", choices=("off",), required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--url-map", type=Path, required=True)
    parser.add_argument(
        "--v1-corpus-dir",
        type=Path,
        default=None,
        help=(
            "required unless --no-v1-differential is set (that mode never "
            "reads a V1 corpus)"
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--http-connect-timeout", type=float,
        default=float(os.environ.get("CONCURSOS_HTTP_CONNECT_TIMEOUT", "10")),
    )
    parser.add_argument(
        "--http-read-timeout", type=float,
        default=float(os.environ.get("CONCURSOS_HTTP_READ_TIMEOUT", "60")),
    )
    parser.add_argument(
        "--gemini-timeout", type=float,
        default=float(os.environ.get("CONCURSOS_GEMINI_TIMEOUT", "60")),
    )
    parser.add_argument(
        "--heartbeat-seconds", type=float,
        default=float(os.environ.get("CONCURSOS_HEARTBEAT_SECONDS", "30")),
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=Path(os.environ.get(
            "GEMINI_CONCURSOS_ENV", "~/.hermes/gemini_concursos.env"
        )).expanduser(),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-model-subprocess", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--abc-mode", choices=("slim", "full"), default="slim")
    parser.add_argument("--allow-sin-cobertura-v1", action="store_true")
    parser.add_argument(
        "--no-v1-differential",
        action="store_true",
        help=(
            "pure V2-vs-golden evaluation: --v1-corpus-dir becomes optional "
            "and unread, no schema-1 cassette/differential/flips are "
            "written; writes v2_only_differential.json/.csv instead"
        ),
    )
    parser.add_argument(
        "--units",
        action="append",
        metavar="MUNICIPIO:BUCKET",
        help=(
            "repeatable exact unit allowlist; bucket is concurso_publico or "
            "processo_seletivo"
        ),
    )
    parser.add_argument(
        "--render-fallback",
        action="store_true",
        help=(
            "opt-in single headless-render pass for SPA shells / anti-bot "
            "challenges (RenderFallbackFetcher wrapping the transport "
            "fetcher); off by default, never a second HTTP fetch"
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    staging_root: Path = CANONICAL_STAGING_ROOT,
    fetcher_factory: Callable[[], Any] = OrionHTTPFetcher,
    adapter_factory: Callable[..., Any] | None = None,
    differential_runner_factory: Callable[..., GoldenDifferentialRunner] = GoldenDifferentialRunner,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.no_v1_differential and args.v1_corpus_dir is None:
        parser.error(
            "--v1-corpus-dir is required unless --no-v1-differential is set"
        )
    if args.no_v1_differential and args.allow_sin_cobertura_v1:
        print(
            "golden_live=warning --allow-sin-cobertura-v1 is ignored with "
            "--no-v1-differential (the url_map already defines scope)",
            file=sys.stderr,
        )
    if adapter_factory is None:
        adapter_factory = {
            "gemini_free": LiveABCAdapter.from_free_environment,
            "gemini_policy": LiveABCAdapter.from_model_policy_environment,
        }[args.provider]
    os.environ["PYTHONUNBUFFERED"] = "1"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True, write_through=True)
    try:
        environment = (
            load_model_credentials(args.credentials_file)
            if environ is None else dict(environ)
        )
        if args.provider == "gemini_free":
            # Free-only estructural (canario): las credenciales pagas/prohibidas
            # se descartan en la frontera CLI, antes de tocar el adapter free
            # (from_free_environment rechaza su sola presencia). El archivo de
            # credenciales exige ambas keys por contrato de gemini_policy; aqui
            # la paga se vuelve inalcanzable. gemini_policy conserva el environ
            # completo.
            environment = gentle_free_only_environment(environment)
        artifacts = run_golden_live(
            golden_path=args.golden,
            url_map_path=args.url_map,
            v1_corpus_dir=args.v1_corpus_dir,
            output_dir=args.output_dir,
            environ=environment,
            staging_root=staging_root,
            http_connect_timeout=args.http_connect_timeout,
            http_read_timeout=args.http_read_timeout,
            gemini_timeout=args.gemini_timeout,
            seed=args.seed,
            abc_mode=args.abc_mode,
            allow_sin_cobertura_v1=args.allow_sin_cobertura_v1,
            unit_allowlist=parse_unit_specs(args.units),
            resume=args.resume,
            heartbeat_seconds=args.heartbeat_seconds,
            isolate_model_calls=not args.no_model_subprocess,
            no_v1_differential=args.no_v1_differential,
            render_fallback=args.render_fallback,
            fetcher_factory=fetcher_factory,
            adapter_factory=adapter_factory,
            differential_runner_factory=differential_runner_factory,
        )
    except (
        CredentialConfigError,
        GeminiClientError,
        GoldenLiveError,
        RunnerLockError,
        ValueError,
    ) as exc:
        print(
            f"golden_live=failed error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    print(
        "golden_live=complete "
        f"output_dir={artifacts.output_dir} "
        f"total={artifacts.coverage['total']} "
        f"covered={artifacts.coverage['covered']} "
        f"sin_cobertura_v1={artifacts.coverage['sin_cobertura_v1']} "
        f"free_calls={artifacts.telemetry['free_calls']} "
        f"paid_calls={artifacts.telemetry['paid_calls']} "
        f"paid_fallback_reasons={json.dumps(artifacts.telemetry['paid_fallback_reasons'], sort_keys=True)} "
        f"tokens={artifacts.telemetry.get('tokens', 0)}"
        + (
            f" cost={artifacts.telemetry['cost']}"
            if "cost" in artifacts.telemetry else ""
        )
    )
    excluded_motivo = "fuera_de_url_map" if args.no_v1_differential else "sin_cobertura_v1"
    for unit in artifacts.sin_cobertura_v1:
        print(
            "golden_live=excluded "
            f"municipio={unit.municipio!r} bucket={unit.bucket} "
            f"executed=false motivo={excluded_motivo}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
