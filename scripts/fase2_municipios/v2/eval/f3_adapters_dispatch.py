"""Fail-closed dispatch from rescue micro-acquisition to frozen F3 adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
import re

from scripts.fase2_municipios.v2.eval import (
    f3_atende_adapter,
    f3_datatables_adapter,
    f3_multi24_adapter,
)


_MULTI24_HOST = re.compile(r"(?:^|\.)(?:sistemas\.|[^.]+\.)?.*(?:multi24h?)(?:\.|$)", re.I)


def detect_platform(url: str, page_html: str) -> str | None:
    """Select exactly one adapter from URL-first, fail-closed signals."""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if (
        "multi24" in path
        or "multi24" in host
        or (host.startswith("sistemas.") and "/sistemas/transparencia" in path)
        or _MULTI24_HOST.search(host)
    ):
        return "multi24"
    if host.endswith(".atende.net"):
        return "atende"
    if f3_datatables_adapter.detect_datatables_server_side(page_html, url) is not None:
        return "datatables"
    return None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _normalized_candidate(
    raw: Mapping[str, Any],
    *,
    adapter: str,
    source_url: str,
    snapshot_sha256: Any,
) -> dict[str, Any]:
    disposition = str(raw.get("disposition") or "propose").casefold()
    if disposition == "candidata":
        disposition = "propose"
    if disposition not in {"propose", "revisar"}:
        disposition = "revisar"
    candidate_url = str(
        raw.get("url_candidata")
        or raw.get("node_url")
        or raw.get("source_url")
        or source_url
    )
    evidence = _as_mapping(raw.get("evidence"))
    provenance = {
        "adapter": adapter,
        "source_url": source_url,
        "snapshot_sha256": snapshot_sha256,
        "adapter_provenance": raw.get("provenance", ()),
        "evidence": evidence,
    }
    return {
        "url_candidata": candidate_url,
        "disposition": disposition,
        # The runner owns this invariant even if a mocked/malformed adapter lies.
        "confirmed": False,
        "title": str(raw.get("title") or raw.get("label") or ""),
        "item_markers": int(raw.get("item_markers") or 1),
        "provenance": provenance,
    }


def _candidate_bucket(raw: Mapping[str, Any]) -> str:
    """Classify adapter-neutral candidate text with the engine taxonomy."""

    evidence = _as_mapping(raw.get("evidence"))
    text = " ".join(
        str(value or "")
        for value in (
            raw.get("title"),
            raw.get("label"),
            evidence.get("row_text"),
            evidence.get("quote"),
        )
    )
    # Reuse Multi24's existing CP/PSS/PSP taxonomy; do not create a parallel one.
    return f3_multi24_adapter._classify_path((text,))


def _filter_candidates_for_bucket(
    proposals: list[Mapping[str, Any]],
    *,
    bucket: str,
    adapter: str,
    source_url: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for proposal in proposals:
        evidence = _as_mapping(proposal.get("evidence"))
        candidate = _normalized_candidate(
            proposal,
            adapter=adapter,
            source_url=source_url,
            snapshot_sha256=evidence.get("raw_response_sha256", ""),
        )
        classified_bucket = _candidate_bucket(proposal)
        if classified_bucket != bucket:
            candidate["provenance"].update({
                "reason": "bucket_mismatch",
                "requested_bucket": bucket,
                "classified_bucket": classified_bucket or "ambiguous",
            })
            rejected.append(candidate)
            continue
        candidates.append(candidate)
    return candidates, rejected


def _dispatch_multi24(
    *,
    url: str,
    page_html: str,
    municipio: str,
    bucket: str,
    current_year: int,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    authority = context.get("multi24_authority")
    linked_pages = context.get("multi24_linked_pages")
    if authority is None or not isinstance(linked_pages, Mapping):
        return {
            "platform": "multi24",
            "adapter": "f3_multi24_adapter",
            "source_url": url,
            "candidates": [],
            "refusal_reason": "multi24_authority_or_linked_pages_missing",
        }
    entry = context.get("multi24_entry_snapshot")
    if entry is None:
        entry = f3_multi24_adapter.Multi24Snapshot(
            requested_url=url,
            final_url=url,
            status_code=int(context.get("status_code", 200)),
            body=page_html.encode("utf-8"),
            content_type=str(context.get("content_type", "text/html; charset=utf-8")),
            retrieved_at=str(context.get("retrieved_at", "")),
        )
    result = f3_multi24_adapter.analyze_multi24(
        entry=entry,
        linked_pages=linked_pages,
        authority=authority,
        municipio=municipio,
        bucket=bucket,
        current_year=current_year,
    )
    hashes = dict(result.raw_sha256_by_url)
    candidates = []
    if result.disposition == "candidata":
        for candidate in result.candidates:
            candidates.append(_normalized_candidate(
                {
                    "node_url": candidate.node_url,
                    "label": candidate.label,
                    "disposition": result.disposition,
                    "item_markers": len(candidate.items),
                    "provenance": candidate.provenance,
                    "evidence": {
                        "items": [_as_mapping(item) for item in candidate.items],
                        "authority_evidence": list(result.authority_evidence),
                        "identity_evidence": list(result.identity_evidence),
                        "platform_evidence": list(result.platform_evidence),
                    },
                },
                adapter="f3_multi24_adapter",
                source_url=url,
                snapshot_sha256=hashes,
            ))
    return {
        "platform": "multi24",
        "adapter": "f3_multi24_adapter",
        "source_url": url,
        "disposition": "propose" if candidates else "revisar",
        "candidates": candidates,
        "refusal_reason": "" if candidates else result.reason,
        "adapter_snapshot_sha256": hashes,
    }


def _dispatch_atende(
    *, url: str, page_html: str, bucket: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    proposals = f3_atende_adapter.propose_candidates(
        url,
        page_html=page_html,
        plugin_response=context.get("atende_plugin_response"),
        plugin_response_url=str(context.get("atende_plugin_response_url", "")),
        rendered_html=str(context.get("atende_rendered_html", "")),
        iframe_capture=context.get("atende_iframe_capture"),
    )
    candidates, rejected = _filter_candidates_for_bucket(
        proposals,
        bucket=bucket,
        adapter="f3_atende_adapter",
        source_url=url,
    )
    plan = None if candidates else f3_atende_adapter.plan_playwright(url, page_html=page_html)
    return {
        "platform": "atende",
        "adapter": "f3_atende_adapter",
        "source_url": url,
        "disposition": "propose" if candidates else "revisar",
        "candidates": candidates,
        "rejected_candidates": rejected,
        "refusal_reason": "" if candidates else (
            "bucket_mismatch" if rejected else "atende_item_evidence_not_materialized"
        ),
        "hook": dict(plan) if plan else None,
    }


def _dispatch_datatables(
    *, url: str, page_html: str, bucket: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    proof = context.get("delegation_proof")
    fetcher = context.get("datatables_fetcher")
    if not proof or not callable(fetcher):
        return {
            "platform": "datatables",
            "adapter": "f3_datatables_adapter",
            "source_url": url,
            "candidates": [],
            "disposition": "revisar",
            "refusal_reason": "delegation_proof_or_datatables_fetcher_missing",
        }
    proposals = f3_datatables_adapter.propose_candidates(page_html, url, str(proof), fetcher)
    candidates, rejected = _filter_candidates_for_bucket(
        proposals,
        bucket=bucket,
        adapter="f3_datatables_adapter",
        source_url=url,
    )
    return {
        "platform": "datatables",
        "adapter": "f3_datatables_adapter",
        "source_url": url,
        "disposition": "propose" if candidates else "revisar",
        "candidates": candidates,
        "rejected_candidates": rejected,
        "refusal_reason": "" if candidates else (
            "bucket_mismatch" if rejected else "datatables_no_item_positive_rows"
        ),
    }


def dispatch_f3_adapter(
    *,
    url: str,
    page_html: str,
    municipio: str,
    bucket: str,
    current_year: int | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a captured page; adapter refusal is data, never a runner error."""

    inputs = dict(context or {})
    platform = detect_platform(url, page_html)
    if platform is None:
        return {"platform": None, "candidates": []}
    try:
        if platform == "multi24":
            return _dispatch_multi24(
                url=url,
                page_html=page_html,
                municipio=municipio,
                bucket=bucket,
                current_year=current_year or datetime.now().year,
                context=inputs,
            )
        if platform == "atende":
            return _dispatch_atende(url=url, page_html=page_html, bucket=bucket, context=inputs)
        return _dispatch_datatables(
            url=url, page_html=page_html, bucket=bucket, context=inputs
        )
    except (ValueError, TypeError, f3_datatables_adapter.DataTablesAdapterError) as exc:
        return {
            "platform": platform,
            "adapter": f"f3_{platform}_adapter",
            "source_url": url,
            "disposition": "revisar",
            "candidates": [],
            "refusal_reason": f"{type(exc).__name__}:{exc}",
        }


__all__ = ["detect_platform", "dispatch_f3_adapter"]
