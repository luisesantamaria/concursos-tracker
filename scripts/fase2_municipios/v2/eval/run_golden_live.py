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
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios.v2.eval.cassette_producer import (
    CassetteProducer,
    Run497V1Source,
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
    canonical_json_bytes,
    derived_csv_bytes,
    run_live,
)
from scripts.fase2_municipios.v2.eval.live_abc_adapter import (
    LiveABCAdapter,
    LiveABCOutcome,
    OrionHTTPFetcher,
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
FINAL_FILENAMES = (
    "golden_cassette.schema1.json",
    "differential.json",
    "differential.csv",
    "flips.json",
)
AUDIT_FILENAME = "live_audit.json"


class GoldenLiveError(RuntimeError):
    """Secret-free turnkey execution failure."""


class GoldenLiveInputError(GoldenLiveError):
    """An explicit input is missing, ambiguous, or outside its contract."""


class GoldenLiveIncompleteError(GoldenLiveError):
    """At least one unit failed closed; no cassette/differential was published."""


@dataclass(frozen=True)
class GoldenLiveArtifacts:
    output_dir: Path
    cassette: Path
    differential_json: Path
    differential_csv: Path
    flips: Path
    audit: Path
    coverage: Mapping[str, int]
    sin_cobertura_v1: tuple[SinCoberturaV1Unit, ...]


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


def _checked_output_dir(output_dir: Path, staging_root: Path) -> Path:
    root = Path(staging_root).resolve()
    destination = Path(output_dir).resolve()
    if not _is_relative_to(destination, root):
        raise GoldenLiveInputError("output_dir_must_be_inside_staging_root")
    for filename in (*FINAL_FILENAMES, AUDIT_FILENAME):
        if (destination / filename).exists():
            raise GoldenLiveInputError(f"output_artifact_already_exists:{filename}")
    return destination


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
                raise GoldenLiveInputError(f"duplicate_golden_unit:{municipio}/{bucket}")
            seen.add(key)
            targets.append((municipio, bucket))
    if not targets:
        raise GoldenLiveInputError("golden_has_no_targets")
    return tuple(targets)


def load_url_map(
    path: Path,
    targets: tuple[tuple[str, str], ...],
    *,
    allow_sin_cobertura_v1: bool = False,
) -> GoldenTargetCoverage:
    expected = {
        (golden_evaluator.muni_key(municipio), bucket): (municipio, bucket)
        for municipio, bucket in targets
    }
    supplied: dict[tuple[str, str], str] = {}
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
                if key not in expected:
                    raise GoldenLiveInputError(
                        f"unexpected_url_map_unit_at_row:{row_number}"
                    )
                if key in supplied:
                    raise GoldenLiveInputError(
                        f"duplicate_url_map_unit_at_row:{row_number}"
                    )
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise GoldenLiveInputError(
                        f"invalid_url_map_url_at_row:{row_number}"
                    )
                supplied[key] = url
    except (OSError, UnicodeError, csv.Error) as exc:
        raise GoldenLiveInputError("url_map_unreadable") from exc
    missing = sorted(set(expected) - set(supplied))
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


def run_golden_live(
    *,
    golden_path: Path,
    url_map_path: Path,
    v1_corpus_dir: Path,
    output_dir: Path,
    environ: Mapping[str, str],
    staging_root: Path = CANONICAL_STAGING_ROOT,
    timeout_seconds: float = 15.0,
    seed: int = 0,
    allow_sin_cobertura_v1: bool = False,
    fetcher_factory: Callable[[], Any] = OrionHTTPFetcher,
    adapter_factory: Callable[..., Any] = LiveABCAdapter.from_free_environment,
    differential_runner_factory: Callable[..., GoldenDifferentialRunner] = GoldenDifferentialRunner,
) -> GoldenLiveArtifacts:
    # Credential policy is the first operation: no input parsing, directory
    # creation, fetch, or model construction occurs before it passes.
    resolve_free_api_key(environ)
    destination = _checked_output_dir(output_dir, staging_root)
    golden = Path(golden_path)
    url_map = Path(url_map_path)
    v1_dir = Path(v1_corpus_dir)
    if not golden.is_file():
        raise GoldenLiveInputError("golden_path_not_file")
    if not url_map.is_file():
        raise GoldenLiveInputError("url_map_path_not_file")
    if not v1_dir.is_dir():
        raise GoldenLiveInputError("v1_corpus_dir_not_directory")

    expected_targets = golden_targets(golden)
    coverage = load_url_map(
        url_map,
        expected_targets,
        allow_sin_cobertura_v1=allow_sin_cobertura_v1,
    )
    targets = coverage.covered
    adapter = adapter_factory(
        fetcher=fetcher_factory(),
        target_urls=coverage.target_urls,
        environ=environ,
        timeout_seconds=timeout_seconds,
    )
    models = RoleModels()
    contract = LiveContract(
        provider="gemini_free",
        certifier_model=models.certifier_model,
        prosecutor_model=models.prosecutor_model,
        judge_model=models.judge_model,
        tools=None,
        environ=environ,
    )

    outcomes: list[LiveABCOutcome] = []
    for municipio, bucket in targets:
        outcome = run_live(
            contract=contract,
            enable_live_abc=True,
            abc_provider=adapter,
            municipio=municipio,
            bucket=bucket,
        )
        if not isinstance(outcome, LiveABCOutcome):
            raise GoldenLiveError("live_adapter_returned_invalid_outcome")
        outcomes.append(outcome)

    producer = CassetteProducer(
        v1_source=Run497V1Source(v1_dir),
        abc_provider=adapter,
    )
    result = producer.produce(
        targets,
        sin_cobertura_v1=coverage.sin_cobertura_v1,
    )
    audit_path = destination / AUDIT_FILENAME
    audit = {
        "schema_version": 1,
        "complete": result.complete,
        "coverage": coverage.summary,
        "sin_cobertura_v1": [
            unit.as_mapping() for unit in coverage.sin_cobertura_v1
        ],
        "inputs": {
            "golden_sha256": _file_sha256(golden),
            "url_map_sha256": _file_sha256(url_map),
            "v1_manifest_sha256": _directory_manifest_sha256(v1_dir),
        },
        "units": [_outcome_audit(outcome) for outcome in outcomes],
        "producer_diagnostics": [
            {
                "municipio": diagnostic.unit[0],
                "bucket": diagnostic.unit[1],
                "code": diagnostic.code.value,
            }
            for diagnostic in result.diagnostics
        ],
    }
    _atomic_write(audit_path, canonical_json_bytes(audit))
    if not result.complete:
        raise GoldenLiveIncompleteError(
            f"live_corpus_incomplete:diagnostics={len(result.diagnostics)}"
        )

    cassette_path = destination / FINAL_FILENAMES[0]
    producer.publish(result, destination=cassette_path, golden_path=golden)
    differential = differential_runner_factory(seed=seed).run_replay(
        golden_path=golden,
        corpus_path=cassette_path,
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
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Turnkey free-only golden live cassette and differential"
    )
    parser.add_argument("--provider", choices=("gemini_free",), required=True)
    parser.add_argument("--tools", choices=("none",), required=True)
    parser.add_argument("--grounding", choices=("off",), required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--url-map", type=Path, required=True)
    parser.add_argument("--v1-corpus-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--allow-sin-cobertura-v1", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    staging_root: Path = CANONICAL_STAGING_ROOT,
    fetcher_factory: Callable[[], Any] = OrionHTTPFetcher,
    adapter_factory: Callable[..., Any] = LiveABCAdapter.from_free_environment,
    differential_runner_factory: Callable[..., GoldenDifferentialRunner] = GoldenDifferentialRunner,
) -> int:
    args = _parser().parse_args(argv)
    source_environment = os.environ if environ is None else environ
    environment = gentle_free_only_environment(source_environment)
    if environ is None:
        # This process is the turnkey CLI child. Remove only paid credential
        # variables from its real environment so the SDK cannot discover them;
        # proxy/resolver/CA/SSL/locale and every other runtime setting survive.
        for name in tuple(os.environ):
            if name not in environment:
                os.environ.pop(name, None)
    try:
        artifacts = run_golden_live(
            golden_path=args.golden,
            url_map_path=args.url_map,
            v1_corpus_dir=args.v1_corpus_dir,
            output_dir=args.output_dir,
            environ=environment,
            staging_root=staging_root,
            timeout_seconds=args.timeout_seconds,
            seed=args.seed,
            allow_sin_cobertura_v1=args.allow_sin_cobertura_v1,
            fetcher_factory=fetcher_factory,
            adapter_factory=adapter_factory,
            differential_runner_factory=differential_runner_factory,
        )
    except (GeminiClientError, GoldenLiveError) as exc:
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
        f"sin_cobertura_v1={artifacts.coverage['sin_cobertura_v1']}"
    )
    for unit in artifacts.sin_cobertura_v1:
        print(
            "golden_live=excluded "
            f"municipio={unit.municipio!r} bucket={unit.bucket} "
            "executed=false motivo=sin_cobertura_v1"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
