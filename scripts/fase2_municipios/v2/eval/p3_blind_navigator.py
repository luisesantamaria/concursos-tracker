#!/usr/bin/env python3
"""RUNNER-P3: blind, deterministic Playwright navigation without AI models.

The module deliberately has no dependency on the existing discovery engine.  Its
only inputs are the four command-line paths accepted by :func:`build_parser`.
All application-level local file access goes through :class:`BlindFileAccess`.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import csv
import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence, TextIO
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


MAX_ADDITIONAL_PAGES = 3
MAX_DEPTH = 2
MAX_INTERACTIONS = 5
MAX_SNAPSHOT_BYTES = 50 * 1024
MANIFEST_FIELDNAMES = ["municipio", "bucket", "site_base"]

NAVIGATION_TERMS = (
    "concursos",
    "concurso",
    "processos seletivos",
    "processo seletivo",
    "editais",
    "edital",
    "publicacoes",
    "publicações",
    "transparencia",
    "transparência",
    "acesso a informacao",
    "acesso à informação",
    "acessar",
)

# Deliberate local copy of the engine/certifier ITEM-POSITIVE rule.  It is not
# imported so this blind runner cannot drag in engine loaders or hidden sources.
ITEM_POSITIVE_KEYWORD_PATTERN = re.compile(
    r"\b(?:editais|edital|concursos?|processos?\s+seletivos?"
    r"|processos?\s+simplificados?|selecao|selecoes)\b"
)
ITEM_POSITIVE_INSTANCE_PATTERN = re.compile(
    r"(?:"
    r"\bn[o°]\.?\s*\d+"
    r"|\bnum\.?\s*\d+"
    r"|\d{1,4}\s*/\s*\d{2,4}"
    r"|\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}"
    r")"
)

INDEX_CONTEXT_PATTERN = re.compile(
    r"\b(?:filtr|pesquis|buscar|resultado|publicacoes|publica[cç][aã]o|"
    r"editais|ano|pagina|p[aá]gina)\b",
    re.IGNORECASE,
)
DETAIL_URL_PATTERN = re.compile(
    r"(?:\.pdf(?:$|\?)|/(?:noticia|noticias|detalhe|detalhes|visualizar)/|"
    r"/(?:edital|concurso|processo-seletivo)[-_]?\d+(?:/|$))",
    re.IGNORECASE,
)
RESTRICTED_PATTERN = re.compile(
    r"\b(?:captcha|recaptcha|hcaptcha|login|senha|autentica[cç][aã]o|"
    r"acesso negado|access denied|verifique que voce e humano|"
    r"verifique que você é humano|cloudflare|security check|robot)\b",
    re.IGNORECASE,
)
ABSENCE_PATTERN = re.compile(
    r"\b(?:nenhum resultado|nenhum registro|nao encontrado|não encontrado|"
    r"sem resultados|0 resultados)\b",
    re.IGNORECASE,
)
CULTURAL_PATTERN = re.compile(
    r"\b(?:soberanas?|rainhas?|concurso cultural|fotograf(?:ia|ico))\b",
    re.IGNORECASE,
)

FORBIDDEN_BASENAMES = {"golden_set_v1.csv", "progress.csv", "authority.py"}
FORBIDDEN_NAME_PREFIXES = ("url_map",)
FORBIDDEN_REGISTRY_PATTERN = re.compile(
    r"(?:domain|dominio|authority).*(?:registry|registro)|"
    r"(?:registry|registro).*(?:domain|dominio|authority)",
    re.IGNORECASE,
)


class BlindAccessViolation(PermissionError):
    """Raised before an undeclared local path can be opened."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


class AuditLogger:
    """Append-only JSONL audit sink, kept open to avoid recursive self-logging."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.output_dir / "audit_log.jsonl"
        # This is the one bootstrap open: it records itself immediately below.
        self._handle: TextIO = builtins.open(self.path, "a", encoding="utf-8")
        self.record(
            "file_open",
            path=str(self.path),
            mode="a",
            purpose="audit_sink_bootstrap",
            allowed=True,
        )

    def record(self, event: str, **payload: Any) -> None:
        entry = {"timestamp": utc_now_iso(), "event": event, **payload}
        self._handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self.record("audit_closed")
            self._handle.close()

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class BlindFileAccess:
    """Strict read whitelist and output-only writer with complete open auditing."""

    def __init__(
        self,
        *,
        manifest: Path,
        gate: Path,
        equivalencias: Path,
        output_dir: Path,
        audit: AuditLogger,
    ) -> None:
        self.inputs = {
            manifest.resolve(),
            gate.resolve(),
            equivalencias.resolve(),
        }
        self.output_dir = output_dir.resolve()
        self.audit = audit

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _explicitly_forbidden(path: Path) -> str | None:
        name = path.name.casefold()
        if name in FORBIDDEN_BASENAMES:
            return f"forbidden basename: {path.name}"
        if any(name.startswith(prefix) for prefix in FORBIDDEN_NAME_PREFIXES):
            return f"forbidden name prefix: {path.name}"
        if FORBIDDEN_REGISTRY_PATTERN.search(name):
            return f"forbidden domain/authority registry: {path.name}"
        return None

    def _authorize(self, path: Path, mode: str) -> tuple[Path, str]:
        resolved = path.resolve()
        reason = self._explicitly_forbidden(resolved)
        reading = "r" in mode or "+" in mode
        writing = any(flag in mode for flag in ("w", "a", "x", "+"))

        if reason is not None:
            raise BlindAccessViolation(reason)
        if "b" in mode:
            raise BlindAccessViolation("binary file access is disabled; UTF-8 text is mandatory")
        if reading and resolved not in self.inputs and not self._is_relative_to(
            resolved, self.output_dir
        ):
            raise BlindAccessViolation(
                f"read outside declared inputs/output-dir blocked: {resolved}"
            )
        if writing and not self._is_relative_to(resolved, self.output_dir):
            raise BlindAccessViolation(f"write outside output-dir blocked: {resolved}")
        if not reading and not writing:
            raise BlindAccessViolation(f"unsupported open mode: {mode}")
        return resolved, "read" if reading and not writing else "write"

    def open(self, path: str | os.PathLike[str], mode: str = "r") -> TextIO:
        candidate = Path(path)
        try:
            resolved, purpose = self._authorize(candidate, mode)
        except BlindAccessViolation as exc:
            self.audit.record(
                "file_open",
                path=str(candidate.resolve()),
                mode=mode,
                purpose="blocked",
                allowed=False,
                reason=str(exc),
            )
            raise
        self.audit.record(
            "file_open",
            path=str(resolved),
            mode=mode,
            purpose=purpose,
            allowed=True,
        )
        return builtins.open(resolved, mode, encoding="utf-8", newline="")

    def read_text(self, path: str | os.PathLike[str]) -> str:
        with self.open(path, "r") as handle:
            return handle.read()

    def atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        target = path.resolve()
        self._authorize(target, "w")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self.open(temporary, "w") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self.audit.record(
                "file_replace",
                source=str(temporary),
                destination=str(target),
                allowed=True,
            )
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True)
class Unit:
    municipio: str
    bucket: str
    site_base: str


@dataclass(frozen=True)
class Link:
    text: str
    href: str


@dataclass(frozen=True)
class PageState:
    url: str
    text: str
    links: tuple[Link, ...] = ()
    title: str = ""
    canonical_url: str | None = None


class NavigationSession(Protocol):
    async def visit(self, url: str) -> PageState: ...

    async def close(self) -> None: ...


class PlaywrightSession:
    """Small adapter keeping Playwright details outside the heuristic core."""

    def __init__(self, page: Any) -> None:
        self.page = page

    async def visit(self, url: str) -> PageState:
        await self.page.goto(url, wait_until="networkidle")
        await self.page.wait_for_load_state("networkidle")
        text = await self.page.locator("body").inner_text(timeout=15_000)
        title = await self.page.title()
        raw_links = await self.page.locator("a[href]").evaluate_all(
            """els => els.map(el => ({
                text: (el.innerText || el.textContent || '').trim(),
                href: el.getAttribute('href') || ''
            }))"""
        )
        canonical_locator = self.page.locator("link[rel='canonical']")
        canonical_url = None
        if await canonical_locator.count():
            canonical_url = await canonical_locator.first.get_attribute(
                "href", timeout=2_000
            )
        links = tuple(
            Link(text=str(item.get("text", "")), href=str(item.get("href", "")))
            for item in raw_links
        )
        return PageState(
            url=self.page.url,
            text=text,
            links=links,
            title=title,
            canonical_url=canonical_url,
        )

    async def close(self) -> None:
        await self.page.close()


@dataclass
class Candidate:
    url: str
    quotes: list[str]
    kind: str
    path: list[dict[str, Any]]


@dataclass
class NavigationOutcome:
    result: str
    reason: str | None
    final_path: list[dict[str, Any]]
    explored: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    citations: list[str]
    interaction_count: int
    additional_pages: int


def canonicalize_url(url: str) -> str:
    split = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (
            split.scheme.lower(),
            split.netloc.lower(),
            re.sub(r"/{2,}", "/", split.path) or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def related_to_official(candidate_url: str, official_url: str) -> bool:
    candidate = (urlsplit(candidate_url).hostname or "").casefold().removeprefix("www.")
    official = (urlsplit(official_url).hostname or "").casefold().removeprefix("www.")
    if not candidate or not official:
        return False
    return (
        candidate == official
        or candidate.endswith("." + official)
        or official.endswith("." + candidate)
    )


def is_http_url(url: str) -> bool:
    return urlsplit(url).scheme.casefold() in {"http", "https"}


def truncate_snapshot(text: str) -> tuple[str, str, bool]:
    normalized = unicodedata.normalize("NFC", text)
    encoded = normalized.encode("utf-8")
    truncated = len(encoded) > MAX_SNAPSHOT_BYTES
    data = encoded[:MAX_SNAPSHOT_BYTES]
    while True:
        try:
            snapshot = data.decode("utf-8")
            break
        except UnicodeDecodeError:
            data = data[:-1]
    return snapshot, hashlib.sha256(data).hexdigest(), truncated


def is_item_positive(value: str) -> bool:
    folded = fold_text(value)
    return bool(
        ITEM_POSITIVE_KEYWORD_PATTERN.search(folded)
        and ITEM_POSITIVE_INSTANCE_PATTERN.search(folded)
        and not ABSENCE_PATTERN.search(value)
        and not CULTURAL_PATTERN.search(value)
    )


def bucket_matches(value: str, bucket: str) -> bool:
    folded = fold_text(value)
    normalized_bucket = fold_text(bucket).replace("-", "_").replace(" ", "_")
    if normalized_bucket in {"concurso", "concursos", "concurso_publico"}:
        return bool(re.search(r"\bconcursos?\b", folded))
    if normalized_bucket in {
        "processo_seletivo",
        "processos_seletivos",
        "pss",
        "selecao_simplificada",
    }:
        return bool(
            re.search(
                r"\b(?:processos?\s+(?:seletivos?|simplificados?)|selecao|selecoes)\b",
                folded,
            )
        )
    return False


def extract_item_positive_quotes(text: str, bucket: str) -> list[str]:
    quotes: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        chunks = [line] if len(line) <= 600 else re.split(r"(?<=[.;])\s+", line)
        for chunk in chunks:
            literal = chunk[:600].strip()
            if (
                literal
                and literal not in seen
                and is_item_positive(literal)
                and bucket_matches(literal, bucket)
            ):
                seen.add(literal)
                quotes.append(literal)
    return quotes


def navigation_link_priority(link: Link) -> tuple[int, str, str] | None:
    folded = fold_text(link.text)
    for index, term in enumerate(NAVIGATION_TERMS):
        if fold_text(term) in folded:
            return index, folded, link.href
    return None


def page_candidate(
    state: PageState,
    bucket: str,
    path: list[dict[str, Any]],
) -> Candidate | None:
    candidate_url = canonicalize_url(urljoin(state.url, state.canonical_url or state.url))
    if DETAIL_URL_PATTERN.search(candidate_url):
        return None
    quotes = extract_item_positive_quotes(state.text, bucket)
    if not quotes:
        return None
    if len(quotes) >= 2:
        kind = "multiple_items"
    elif INDEX_CONTEXT_PATTERN.search(state.text):
        kind = "index_context"
    else:
        return None
    return Candidate(url=candidate_url, quotes=quotes, kind=kind, path=path)


def choose_candidate(candidates: Sequence[Candidate]) -> tuple[Candidate | None, str | None]:
    by_url: dict[str, Candidate] = {}
    for candidate in candidates:
        previous = by_url.get(candidate.url)
        if previous is None or (
            previous.kind == "index_context" and candidate.kind == "multiple_items"
        ):
            by_url[candidate.url] = candidate
    unique = list(by_url.values())
    if not unique:
        return None, "sin_superficie_con_bucket_e_items_item_positive"

    strongest = [item for item in unique if item.kind == "multiple_items"]
    pool = strongest or unique
    if len(pool) == 1:
        return pool[0], None
    return None, "ambiguedad_entre_superficies_plausibles"


def _snapshot_record(state: PageState, requested_url: str) -> dict[str, Any]:
    snapshot, digest, truncated = truncate_snapshot(state.text)
    return {
        "requested_url": requested_url,
        "url": state.url,
        "title": state.title,
        "text": snapshot,
        "sha256": digest,
        "truncated": truncated,
        "byte_limit": MAX_SNAPSHOT_BYTES,
    }


async def navigate_unit(
    session: NavigationSession,
    unit: Unit,
    audit: AuditLogger,
) -> NavigationOutcome:
    candidates: list[Candidate] = []
    explored: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    visited: set[str] = set()
    interaction_count = 0
    additional_pages = 0
    restricted = False

    async def explore(
        requested_url: str,
        depth: int,
        path: list[dict[str, Any]],
        from_url: str | None,
        via_text: str | None,
    ) -> None:
        nonlocal interaction_count, additional_pages, restricted
        if restricted or depth > MAX_DEPTH:
            return
        if depth > 0 and additional_pages >= MAX_ADDITIONAL_PAGES:
            return

        audit.record(
            "url_visit",
            municipio=unit.municipio,
            bucket=unit.bucket,
            requested_url=requested_url,
            depth=depth,
        )
        state = await session.visit(requested_url)
        actual_url = canonicalize_url(state.url)
        if actual_url != canonicalize_url(requested_url):
            audit.record(
                "url_visit_redirect",
                municipio=unit.municipio,
                bucket=unit.bucket,
                requested_url=requested_url,
                actual_url=state.url,
                depth=depth,
            )
        if depth > 0:
            additional_pages += 1
        current_step = {
            "url": state.url,
            "via_texto_exacto": via_text,
        }
        current_path = [*path, current_step]
        explored.append(
            {
                "from_url": from_url,
                "url": state.url,
                "via_texto_exacto": via_text,
                "depth": depth,
            }
        )
        snapshots.append(_snapshot_record(state, requested_url))

        if not related_to_official(state.url, unit.site_base):
            restricted = True
            explored[-1]["blocked"] = "external_redirect_not_related"
            return
        restricted_text = f"{state.title}\n{state.url}\n{state.text[:10000]}"
        if RESTRICTED_PATTERN.search(restricted_text):
            restricted = True
            return
        if actual_url in visited:
            return
        visited.add(actual_url)

        candidate = page_candidate(state, unit.bucket, current_path)
        if candidate is not None:
            if related_to_official(candidate.url, unit.site_base):
                candidates.append(candidate)

        if depth >= MAX_DEPTH:
            return
        prioritized: list[tuple[tuple[int, str, str], Link, str]] = []
        for link in state.links:
            priority = navigation_link_priority(link)
            href = urljoin(state.url, link.href.strip())
            if (
                priority is None
                or not link.href.strip()
                or not is_http_url(href)
                or not related_to_official(href, unit.site_base)
                or canonicalize_url(href) in visited
            ):
                continue
            prioritized.append((priority, link, href))

        for _priority, link, href in sorted(prioritized, key=lambda item: item[0]):
            if restricted:
                return
            if interaction_count >= MAX_INTERACTIONS:
                return
            if additional_pages >= MAX_ADDITIONAL_PAGES:
                return
            interaction_count += 1
            audit.record(
                "semantic_interaction",
                municipio=unit.municipio,
                bucket=unit.bucket,
                number=interaction_count,
                action="follow_real_href",
                from_url=state.url,
                href=href,
                exact_text=link.text,
            )
            await explore(href, depth + 1, current_path, state.url, link.text)

    try:
        await explore(unit.site_base, 0, [], None, None)
        if restricted:
            return NavigationOutcome(
                result="REVISAR",
                reason="REVISION_HUMANA_ACCESO_RESTRINGIDO",
                final_path=[],
                explored=explored,
                snapshots=snapshots,
                citations=[],
                interaction_count=interaction_count,
                additional_pages=additional_pages,
            )
        selected, reason = choose_candidate(candidates)
        if selected is None:
            return NavigationOutcome(
                result="REVISAR",
                reason=reason,
                final_path=[],
                explored=explored,
                snapshots=snapshots,
                citations=[],
                interaction_count=interaction_count,
                additional_pages=additional_pages,
            )
        return NavigationOutcome(
            result=selected.url,
            reason=None,
            final_path=selected.path,
            explored=explored,
            snapshots=snapshots,
            citations=selected.quotes,
            interaction_count=interaction_count,
            additional_pages=additional_pages,
        )
    except Exception as exc:
        audit.record(
            "unit_navigation_error",
            municipio=unit.municipio,
            bucket=unit.bucket,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        return NavigationOutcome(
            result="REVISAR",
            reason=f"error_navegacion:{type(exc).__name__}",
            final_path=[],
            explored=explored,
            snapshots=snapshots,
            citations=[],
            interaction_count=interaction_count,
            additional_pages=additional_pages,
        )


def safe_unit_filename(unit: Unit) -> str:
    def clean(value: str) -> str:
        normalized = unicodedata.normalize("NFC", value).strip()
        return re.sub(r"[^\w.-]+", "_", normalized, flags=re.UNICODE).strip("_.") or "vacio"

    return f"unidad_{clean(unit.municipio)}_{clean(unit.bucket)}.json"


def load_inputs(
    files: BlindFileAccess,
    manifest_path: Path,
    gate_path: Path,
    equivalencias_path: Path,
) -> tuple[list[Unit], dict[str, str]]:
    # Keep this as the first structural check: a contaminated manifest must be
    # rejected before other inputs are interpreted or Playwright is imported.
    with files.open(manifest_path, "r") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MANIFEST_FIELDNAMES:
            raise BlindAccessViolation(
                "invalid manifest columns: "
                f"expected {MANIFEST_FIELDNAMES!r}, found {reader.fieldnames!r}"
            )
        units = [
            Unit(
                municipio=(row.get("municipio") or "").strip(),
                bucket=(row.get("bucket") or "").strip(),
                site_base=(row.get("site_base") or "").strip(),
            )
            for row in reader
        ]

    gate_text = files.read_text(gate_path)
    equivalencias_text = files.read_text(equivalencias_path)
    if not gate_text.strip() or not equivalencias_text.strip():
        raise ValueError("gate and equivalencias must be non-empty UTF-8 documents")

    if not units:
        raise ValueError("manifest contains no units")
    identities: set[tuple[str, str]] = set()
    for unit in units:
        if not unit.municipio or not unit.bucket or not is_http_url(unit.site_base):
            raise ValueError(f"invalid manifest row: {unit!r}")
        identity = (unit.municipio, unit.bucket)
        if identity in identities:
            raise ValueError(f"duplicate municipio+bucket in manifest: {identity!r}")
        identities.add(identity)

    provenance = {
        "gate_sha256": hashlib.sha256(gate_text.encode("utf-8")).hexdigest(),
        "equivalencias_sha256": hashlib.sha256(
            equivalencias_text.encode("utf-8")
        ).hexdigest(),
    }
    return units, provenance


def outcome_payload(
    unit: Unit,
    outcome: NavigationOutcome,
    started_at: str,
    finished_at: str,
    provenance: dict[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "municipio": unit.municipio,
        "bucket": unit.bucket,
        "site_base": unit.site_base,
        "resultado": outcome.result,
        "ruta_navegacion_final": outcome.final_path,
        "estados_explorados": outcome.explored,
        "snapshots": outcome.snapshots,
        "citas_item_positive": outcome.citations,
        "limites": {
            "profundidad_maxima": MAX_DEPTH,
            "paginas_adicionales": outcome.additional_pages,
            "paginas_adicionales_maximas": MAX_ADDITIONAL_PAGES,
            "interacciones_semanticas": outcome.interaction_count,
            "interacciones_semanticas_maximas": MAX_INTERACTIONS,
        },
        "timestamps": {"inicio": started_at, "fin": finished_at},
        "provenance": provenance,
    }
    if outcome.result == "REVISAR":
        payload["motivo"] = outcome.reason or "revision_sin_motivo_especifico"
    return payload


async def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    with AuditLogger(output_dir) as audit:
        files = BlindFileAccess(
            manifest=Path(args.manifest),
            gate=Path(args.gate),
            equivalencias=Path(args.equivalencias),
            output_dir=output_dir,
            audit=audit,
        )
        units, provenance = load_inputs(
            files,
            Path(args.manifest),
            Path(args.gate),
            Path(args.equivalencias),
        )
        audit.record("run_inputs_loaded", unit_count=len(units), **provenance)

        # Imported only here: importing this module for unit tests needs no
        # Playwright installation and cannot initialize any existing engine.
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                for unit in units:
                    started_at = utc_now_iso()
                    page = await browser.new_page()
                    session = PlaywrightSession(page)
                    try:
                        outcome = await navigate_unit(session, unit, audit)
                    finally:
                        await session.close()
                    finished_at = utc_now_iso()
                    payload = outcome_payload(
                        unit, outcome, started_at, finished_at, provenance
                    )
                    destination = output_dir / safe_unit_filename(unit)
                    files.atomic_write_json(destination, payload)
                    audit.record(
                        "unit_output_written",
                        municipio=unit.municipio,
                        bucket=unit.bucket,
                        path=str(destination),
                        result=outcome.result,
                    )
            finally:
                await browser.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RUNNER-P3 blind heuristic Playwright navigator (no AI/LLM)"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--gate", required=True)
    parser.add_argument("--equivalencias", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
