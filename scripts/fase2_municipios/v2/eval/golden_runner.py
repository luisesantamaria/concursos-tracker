"""Deterministic per-bucket golden differential runner for V1/V2."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2.agents import ABCOrchestrator, ConflictJudge
from scripts.fase2_municipios.v2.gemini import (
    GeminiClientError,
    RoleModels,
    resolve_free_api_key,
)
from scripts.fase2_municipios.v2.snapshot import (
    CitationVerificationError,
    EvidenceSource,
    anchor_citation,
    build_snapshot,
    verify_all,
)


SCHEMA_VERSION = 1
MAX_MUNICIPIO_CHARS = 200
MAX_REASON_CHARS = 4_000
MAX_QUOTE_CHARS = 4_000
MAX_SOURCE_CHARS = 200_000
MAX_SOURCES = 32
CONFIRMATIONS = frozenset({
    "indice_oficial",
    "indice_oficial_combinado",
    "portal_externo_oficial",
})
FLIP_VALUES = frozenset({
    "both_confirm_same_resource",
    "both_confirm_distinct_resource",
    "v2_confirm_v1_review",
    "v1_confirm_v2_review",
    "both_review",
    "both_negative",
    "v2_confirm_v1_negative",
    "v1_confirm_v2_negative",
    "v2_review_v1_negative",
    "v1_review_v2_negative",
})
GOLDEN_COMPARISON_VALUES = frozenset({"match", "differ", "golden_na"})
BUCKET_COLUMNS = (
    (
        "concurso_publico",
        "url_concursos",
        "urls_concursos_extra",
    ),
    (
        "processo_seletivo",
        "url_processos_seletivos",
        "urls_processos_extra",
    ),
)


class ReplayEvidenceError(ValueError):
    """A required replay unit lacks frozen evidence or cassette data."""


class LiveContractError(ValueError):
    """Live execution is unsafe and must abort before any request."""


class FetchAdapter(Protocol):
    def cases(self) -> tuple[Mapping[str, Any], ...]: ...


class ModelAdapter(Protocol):
    def response_for(self, case: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ClockAdapter(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FixedReplayClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class JsonReplayFetchAdapter:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def cases(self) -> tuple[Mapping[str, Any], ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayEvidenceError("invalid replay corpus") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ReplayEvidenceError("unsupported replay corpus schema_version")
        cases = raw.get("cases")
        if not isinstance(cases, list):
            raise ReplayEvidenceError("replay corpus cases must be a list")
        return tuple(cases)


class CassetteModelAdapter:
    def response_for(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        v2 = case.get("v2")
        response = v2.get("judge_response") if isinstance(v2, Mapping) else None
        if not isinstance(response, Mapping):
            raise ReplayEvidenceError("missing judge_response cassette")
        return dict(response)


class _CassetteJudgeClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)
        self.calls = 0

    def generate_structured(self, _contents, *, estimated_tokens: int):
        if not isinstance(estimated_tokens, int) or isinstance(estimated_tokens, bool):
            raise ReplayEvidenceError("invalid estimated_tokens")
        self.calls += 1
        return dict(self.response)


def _bounded_string(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ReplayEvidenceError(f"{field} must be string")
    if len(value) > limit:
        raise ReplayEvidenceError(f"{field} exceeds size limit")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise ReplayEvidenceError(f"{field} contains control characters")
    return value


def _decision_kind(decision: str) -> str:
    if decision in CONFIRMATIONS:
        return "confirm"
    if decision == "revisar":
        return "review"
    if decision == "nao_encontrado" or decision.endswith(("_rechazado", "_rechazada")):
        return "negative"
    raise ReplayEvidenceError(f"unknown discrete decision: {decision}")


def decision_covers_bucket(decision: str, bucket: str) -> bool:
    if bucket not in {item[0] for item in BUCKET_COLUMNS}:
        raise ReplayEvidenceError(f"unknown bucket: {bucket}")
    return decision in CONFIRMATIONS


def classify_flip(
    *, v1_decision: str, v1_url: str, v2_decision: str, v2_url: str
) -> str:
    v1_kind = _decision_kind(v1_decision)
    v2_kind = _decision_kind(v2_decision)
    if v1_kind == v2_kind == "confirm":
        v1_resource = cascade._normalized_candidate_url(v1_url)
        v2_resource = cascade._normalized_candidate_url(v2_url)
        return (
            "both_confirm_same_resource"
            if v1_resource == v2_resource
            else "both_confirm_distinct_resource"
        )
    table = {
        ("review", "confirm"): "v2_confirm_v1_review",
        ("confirm", "review"): "v1_confirm_v2_review",
        ("review", "review"): "both_review",
        ("negative", "negative"): "both_negative",
        ("negative", "confirm"): "v2_confirm_v1_negative",
        ("confirm", "negative"): "v1_confirm_v2_negative",
        ("negative", "review"): "v2_review_v1_negative",
        ("review", "negative"): "v1_review_v2_negative",
    }
    result = table.get((v1_kind, v2_kind))
    if result is None:
        raise ReplayEvidenceError("unclassified flip")
    return result


def _golden_verdict(
    *, golden_main: str, golden_extra: str, url: str
) -> str:
    return golden_evaluator.judge_bucket(golden_main, golden_extra, url)


def compare_to_golden(
    *, decision: str, url: str, golden_main: str, golden_extra: str
) -> str:
    verdict = _golden_verdict(
        golden_main=golden_main,
        golden_extra=golden_extra,
        url=url,
    )
    if verdict == "SKIP":
        return "golden_na"
    kind = _decision_kind(decision)
    if verdict == "HIT" and kind == "confirm":
        return "match"
    if verdict == "T-NEG" and kind == "negative":
        return "match"
    return "differ"


def canonical_json_bytes(artifact: dict) -> bytes:
    return (
        json.dumps(
            artifact,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


CSV_FIELDS = (
    "municipio",
    "bucket",
    "flip_v1_v2",
    "v1_vs_golden",
    "v2_vs_golden",
    "golden_expectation",
    "golden_urls",
    "v1_decision",
    "v1_url",
    "v1_snapshot_ref",
    "v1_reason",
    "v2_decision",
    "v2_url",
    "v2_snapshot_ref",
    "v2_reason",
    "v2_citations",
)


def derived_csv_bytes(artifact: Mapping[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in artifact["rows"]:
        writer.writerow({
            "municipio": row["municipio"],
            "bucket": row["bucket"],
            "flip_v1_v2": row["flip_v1_v2"],
            "v1_vs_golden": row["v1_vs_golden"],
            "v2_vs_golden": row["v2_vs_golden"],
            "golden_expectation": row["golden"]["expectation"],
            "golden_urls": " | ".join(row["golden"]["urls"]),
            "v1_decision": row["v1"]["decision"],
            "v1_url": row["v1"]["url"],
            "v1_snapshot_ref": row["v1"]["evidence"]["snapshot_ref"],
            "v1_reason": row["v1"]["evidence"]["reason"],
            "v2_decision": row["v2"]["decision"],
            "v2_url": row["v2"]["url"],
            "v2_snapshot_ref": row["v2"]["evidence"]["snapshot_ref"],
            "v2_reason": row["v2"]["evidence"]["reason"],
            "v2_citations": json.dumps(
                row["v2"]["citations"],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        })
    return output.getvalue().encode("utf-8")


def _golden_expectation(main: str, extra: str) -> tuple[str, list[str]]:
    values = [value.strip() for value in (main, extra) if value and value.strip()]
    urls = sorted(value for value in values if value.lower() != "no_existe")
    if urls:
        return "confirm", urls
    if any(value.lower() == "no_existe" for value in values):
        return "negative", []
    return "golden_na", []


def _checked_evidence(raw: Any, *, prefix: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ReplayEvidenceError(f"missing {prefix} evidence")
    required = ("snapshot_ref", "authority", "identity", "reason")
    if any(field not in raw for field in required):
        raise ReplayEvidenceError(f"missing {prefix} evidence field")
    return {
        "snapshot_ref": _bounded_string(
            raw["snapshot_ref"], field=f"{prefix}.snapshot_ref", limit=MAX_REASON_CHARS
        ),
        "authority": _bounded_string(
            raw["authority"], field=f"{prefix}.authority", limit=200
        ),
        "identity": _bounded_string(
            raw["identity"], field=f"{prefix}.identity", limit=200
        ),
        "reason": _bounded_string(
            raw["reason"], field=f"{prefix}.reason", limit=MAX_REASON_CHARS
        ),
    }


def _build_v2_snapshot(v2: Mapping[str, Any]):
    evidence_raw = v2.get("evidence")
    checked = _checked_evidence(evidence_raw, prefix="v2")
    sources_raw = evidence_raw.get("sources") if isinstance(evidence_raw, Mapping) else None
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ReplayEvidenceError("missing v2 evidence sources")
    if len(sources_raw) > MAX_SOURCES:
        raise ReplayEvidenceError("too many v2 evidence sources")
    sources = []
    previews = []
    for index, source in enumerate(sources_raw):
        if not isinstance(source, Mapping):
            raise ReplayEvidenceError("invalid v2 source")
        try:
            retrieved_at = datetime.fromisoformat(source["retrieved_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayEvidenceError("invalid v2 source retrieved_at") from exc
        content = _bounded_string(
            source.get("content"),
            field=f"v2.sources[{index}].content",
            limit=MAX_SOURCE_CHARS,
        )
        evidence_source = EvidenceSource(
            source_id=_bounded_string(
                source.get("source_id"), field="source_id", limit=200
            ),
            url=_bounded_string(source.get("url"), field="source_url", limit=2_000),
            retrieved_at=retrieved_at,
            content=content,
        )
        sources.append(evidence_source)
        previews.append({
            "source_id": evidence_source.source_id,
            "url": evidence_source.url,
            "content_sha256": evidence_source.content_sha256,
            "content_preview": content[:MAX_QUOTE_CHARS],
        })
    snapshot = build_snapshot(sources)
    checked["sources"] = sorted(previews, key=lambda item: item["source_id"])
    return snapshot, checked


def _checked_citations(snapshot, raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ReplayEvidenceError("v2 citations must be list")
    citations = []
    try:
        for item in raw:
            if not isinstance(item, Mapping):
                raise ReplayEvidenceError("invalid v2 citation object")
            quote = item.get("quote")
            if not isinstance(quote, str) or len(quote) > MAX_QUOTE_CHARS:
                raise ReplayEvidenceError("v2 citation quote exceeds limit")
            citation = anchor_citation(snapshot, item, require_offsets=True)
            citations.append(citation)
        verify_all(snapshot, citations)
    except CitationVerificationError as exc:
        raise ReplayEvidenceError("invalid v2 citation") from exc
    return sorted(
        [
            {
                "source_id": citation.source_id,
                "start": citation.start,
                "end": citation.end,
                "quote": citation.quote,
            }
            for citation in citations
        ],
        key=lambda item: (
            item["source_id"], item["start"], item["end"], item["quote"]
        ),
    )


def _candidate_from_cassette(
    raw: Any, *, municipio: str, snapshot, bucket: str
) -> cascade.CandidateRecord | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ReplayEvidenceError("invalid v2 candidate cassette")
    url = _bounded_string(raw.get("url"), field="candidate.url", limit=2_000)
    source = snapshot.sources[0]
    v1_snapshot = cascade.EvidenceSnapshot(
        html=f"<html><body>{source.content}</body></html>",
        text=source.content,
        title="replay cassette",
        requested_url=url,
        final_url=url,
        status=200,
        source="v2_replay_cassette",
        evidence_state=raw.get("evidence_state"),
    )
    return cascade.CandidateRecord(
        candidate_id=_bounded_string(
            raw.get("candidate_id"), field="candidate_id", limit=500
        ),
        requested_url=url,
        final_url=url,
        source="v2_replay_cassette",
        tier="replay",
        municipio=municipio,
        bucket_hint=bucket,
        evidence_snapshot=v1_snapshot,
        authority=raw.get("authority"),
        identity=raw.get("identity"),
        page_role="indice_listado",
        evidence_state=raw.get("evidence_state"),
        bucket=raw.get("bucket"),
        decision=raw.get("decision"),
        reason="replay cassette",
        source_kind=raw.get("source_kind"),
        accessible=True,
    )


class GoldenDifferentialRunner:
    def __init__(
        self,
        *,
        seed: int = 0,
        clock: ClockAdapter | None = None,
        model_adapter: ModelAdapter | None = None,
    ) -> None:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("seed must be integer")
        self.seed = seed
        self.clock = clock or FixedReplayClock(datetime.fromisoformat(
            "2000-01-01T00:00:00+00:00"
        ))
        self.model_adapter = model_adapter or CassetteModelAdapter()

    def _v1_view(self, case: Mapping[str, Any]) -> dict[str, Any]:
        raw = case.get("v1")
        if not isinstance(raw, Mapping):
            raise ReplayEvidenceError("missing v1 replay evidence")
        decision = _bounded_string(raw.get("decision"), field="v1.decision", limit=100)
        _decision_kind(decision)
        return {
            "decision": decision,
            "url": _bounded_string(raw.get("url"), field="v1.url", limit=2_000),
            "evidence": _checked_evidence(raw.get("evidence"), prefix="v1"),
        }

    def _v2_view(
        self, case: Mapping[str, Any], *, municipio: str, bucket: str
    ) -> dict[str, Any]:
        raw = case.get("v2")
        if not isinstance(raw, Mapping):
            raise ReplayEvidenceError("missing v2 replay evidence")
        snapshot, evidence = _build_v2_snapshot(raw)
        citations = _checked_citations(snapshot, raw.get("citations"))
        response = self.model_adapter.response_for(case)
        client = _CassetteJudgeClient(response)
        candidate = _candidate_from_cassette(
            raw.get("candidate"), municipio=municipio, snapshot=snapshot, bucket=bucket
        )
        proposal_a = raw.get("proposal_a")
        proposal_b = raw.get("proposal_b")
        if not isinstance(proposal_a, Mapping) or not isinstance(proposal_b, Mapping):
            raise ReplayEvidenceError("missing A/B proposal cassette")
        result = ABCOrchestrator(
            judge=ConflictJudge(client=client)
        ).resolve(
            snapshot=snapshot,
            candidates=(candidate,) if candidate else (),
            proposal_a=proposal_a,
            proposal_b=proposal_b,
        )
        return {
            "decision": result.final_decision.decision,
            "url": result.final_decision.url,
            "citations": citations,
            "evidence": evidence,
            "reason_code": result.reason_code,
        }

    def run_replay(self, *, golden_path: Path, corpus_path: Path) -> dict:
        # Exercise the injected clock without allowing it into the artifact.
        injected_now = self.clock.now()
        if not isinstance(injected_now, datetime):
            raise ReplayEvidenceError("clock must return datetime")
        cases = JsonReplayFetchAdapter(corpus_path).cases()
        by_unit: dict[tuple[str, str], Mapping[str, Any]] = {}
        for case in cases:
            if not isinstance(case, Mapping):
                raise ReplayEvidenceError("replay case must be object")
            municipio = _bounded_string(
                case.get("municipio"), field="municipio", limit=MAX_MUNICIPIO_CHARS
            )
            bucket = case.get("bucket")
            if bucket not in {item[0] for item in BUCKET_COLUMNS}:
                raise ReplayEvidenceError(f"invalid replay bucket: {bucket}")
            key = (golden_evaluator.muni_key(municipio), bucket)
            if key in by_unit:
                raise ReplayEvidenceError(f"duplicate replay unit: {municipio}/{bucket}")
            by_unit[key] = case

        golden_rows = golden_evaluator.read_csv(Path(golden_path))
        rows = []
        metric_counts = {
            "v1": {bucket: Counter() for bucket, _main, _extra in BUCKET_COLUMNS},
            "v2": {bucket: Counter() for bucket, _main, _extra in BUCKET_COLUMNS},
        }
        for golden_row in golden_rows:
            municipio = golden_evaluator.get(golden_row, "municipio")
            if not municipio:
                raise ReplayEvidenceError("golden row without municipio")
            for bucket, main_column, extra_column in BUCKET_COLUMNS:
                key = (golden_evaluator.muni_key(municipio), bucket)
                case = by_unit.get(key)
                if case is None:
                    raise ReplayEvidenceError(
                        f"missing replay evidence/cassette for {municipio}/{bucket}"
                    )
                v1 = self._v1_view(case)
                v2 = self._v2_view(case, municipio=municipio, bucket=bucket)
                golden_main = golden_evaluator.get(golden_row, main_column)
                golden_extra = golden_evaluator.get(golden_row, extra_column)
                expectation, golden_urls = _golden_expectation(
                    golden_main, golden_extra
                )
                v1_verdict = _golden_verdict(
                    golden_main=golden_main,
                    golden_extra=golden_extra,
                    url=v1["url"],
                )
                v2_verdict = _golden_verdict(
                    golden_main=golden_main,
                    golden_extra=golden_extra,
                    url=v2["url"],
                )
                metric_counts["v1"][bucket][v1_verdict] += 1
                metric_counts["v2"][bucket][v2_verdict] += 1
                rows.append({
                    "municipio": municipio,
                    "bucket": bucket,
                    "golden": {
                        "expectation": expectation,
                        "urls": golden_urls,
                    },
                    "v1": v1,
                    "v2": v2,
                    "flip_v1_v2": classify_flip(
                        v1_decision=v1["decision"],
                        v1_url=v1["url"],
                        v2_decision=v2["decision"],
                        v2_url=v2["url"],
                    ),
                    "v1_vs_golden": compare_to_golden(
                        decision=v1["decision"], url=v1["url"],
                        golden_main=golden_main, golden_extra=golden_extra,
                    ),
                    "v2_vs_golden": compare_to_golden(
                        decision=v2["decision"], url=v2["url"],
                        golden_main=golden_main, golden_extra=golden_extra,
                    ),
                })
        rows.sort(key=lambda row: (
            golden_evaluator.muni_key(row["municipio"]), row["bucket"]
        ))
        equivalent_flips = {
            "both_confirm_same_resource", "both_review", "both_negative"
        }
        adjudication = [
            {
                "municipio": row["municipio"],
                "bucket": row["bucket"],
                "flip_v1_v2": row["flip_v1_v2"],
                "v1_vs_golden": row["v1_vs_golden"],
                "v2_vs_golden": row["v2_vs_golden"],
                "v1_evidence": row["v1"]["evidence"],
                "v2_evidence": {
                    **row["v2"]["evidence"],
                    "citations": row["v2"]["citations"],
                    "review_reason_code": row["v2"]["reason_code"],
                },
            }
            for row in rows
            if (
                row["flip_v1_v2"] not in equivalent_flips
                or row["v1_vs_golden"] == "differ"
                or row["v2_vs_golden"] == "differ"
            )
        ]
        metrics = {
            system: {
                bucket: golden_evaluator.precision_recall(dict(counts))
                for bucket, counts in sorted(by_bucket.items())
            }
            for system, by_bucket in sorted(metric_counts.items())
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "rows": rows,
            "adjudication": adjudication,
            "golden_metrics": metrics,
        }


@dataclass(frozen=True)
class LiveContract:
    provider: str
    certifier_model: str
    prosecutor_model: str
    judge_model: str
    tools: Any
    environ: Mapping[str, str]

    @classmethod
    def valid_for_tests(cls) -> "LiveContract":
        models = RoleModels()
        return cls(
            provider="gemini_free",
            certifier_model=models.certifier_model,
            prosecutor_model=models.prosecutor_model,
            judge_model=models.judge_model,
            tools=None,
            environ={"GEMINI_API_KEY_FREE": "test-free-key"},
        )

    def with_overrides(self, **overrides) -> "LiveContract":
        return replace(self, **overrides)


def validate_live_contract(contract: LiveContract) -> None:
    if not isinstance(contract, LiveContract):
        raise LiveContractError("invalid live contract")
    expected = RoleModels()
    if contract.provider != "gemini_free":
        raise LiveContractError("provider must be gemini_free")
    if (
        contract.certifier_model != expected.certifier_model
        or contract.prosecutor_model != expected.prosecutor_model
        or contract.judge_model != expected.judge_model
    ):
        raise LiveContractError("live model contract mismatch")
    # Exact Gemini SDK/config knob: grounding is enabled through `tools`.
    if contract.tools is not None:
        raise LiveContractError("SDK config option tools must be absent/none")
    try:
        resolve_free_api_key(contract.environ)
    except GeminiClientError as exc:
        raise LiveContractError("free-only credential contract failed") from exc


def run_live(*, contract: LiveContract, request_adapter: Any) -> Any:
    validate_live_contract(contract)
    return request_adapter.request()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproducible V1/V2 golden differential runner"
    )
    commands = parser.add_subparsers(dest="mode", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--golden", type=Path, required=True)
    replay.add_argument("--corpus", type=Path, required=True)
    replay.add_argument("--output-json", type=Path, required=True)
    replay.add_argument("--output-csv", type=Path, required=True)
    replay.add_argument("--seed", type=int, default=0)
    live = commands.add_parser("live")
    live.add_argument("--provider", required=True)
    models = RoleModels()
    live.add_argument("--certifier-model", default=models.certifier_model)
    live.add_argument("--prosecutor-model", default=models.prosecutor_model)
    live.add_argument("--judge-model", default=models.judge_model)
    live.add_argument("--tools", choices=("none", "google_search"), required=True)
    live.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "replay":
        artifact = GoldenDifferentialRunner(seed=args.seed).run_replay(
            golden_path=args.golden,
            corpus_path=args.corpus,
        )
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_bytes(canonical_json_bytes(artifact))
        args.output_csv.write_bytes(derived_csv_bytes(artifact))
        return 0
    contract = LiveContract(
        provider=args.provider,
        certifier_model=args.certifier_model,
        prosecutor_model=args.prosecutor_model,
        judge_model=args.judge_model,
        tools=None if args.tools == "none" else [{"google_search": {}}],
        environ=os.environ,
    )
    validate_live_contract(contract)
    if not args.validate_only:
        raise SystemExit(
            "live contract valid; inject Orion fetch/model/clock adapters via run_live"
        )
    print("live_contract=valid provider=gemini_free tools=none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
