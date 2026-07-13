"""Mision D, Parte 1 -- replay determinista del gate final V2 sobre holdout.

Lee artefactos YA GUARDADOS de una corrida (observability JSON por unidad,
con los stages fetch/A/B/C congelados) y RE-COMPUTA la decision final con el
codigo ACTUAL de gate/autoridad (``authority.py`` + ``structural_evidence.py``
+ ``agents.orchestration.ABCOrchestrator``). Cero llamadas a modelo, cero
refetch: A/B/C ya decidieron en su momento y esos textos quedan congelados;
lo unico que corre de nuevo es el codigo determinista (candidato estructural,
gate de seguridad, agregacion A/B/C) para responder "con el codigo de HOY,
llegaria esta unidad a confirmado?".

Limitacion conocida y documentada: el HTML crudo original nunca se persistio
(solo el texto extraido, acotado por ``eval/live_observability.py`` a
``content_limit_chars``). La reconstruccion de contenido en
``_reconstruct_content`` rellena las zonas no capturadas con un caracter de
relleno; los rangos de cita citados por A/B SI se preservan exactos (la
captura acotada los prioriza), asi que la verificacion de citas es fiel. El
campo ``content_complete`` marca cuando la pagina completa fue capturada
(pagina corta, no truncada) para que el lector sepa cuando el chequeo de
autoridad/identidad por contenido corrio sobre el texto real integro.

Por la misma ausencia de HTML crudo, la deteccion de antibot (``page.
is_antibot``, que depende de marcadores dentro del HTML) nunca puede
reproducirse por reconstruccion -- casos reales existen (p.ej. Quevedos)
donde el candidato verdadero quedo con ``evidence_state=incompleta_antibot``
en runtime y este replay, sin ese HTML, lo veria como "completa". Por eso
este modulo usa ``checkpoint.json`` (que SI persiste el candidato real
adjudicado en runtime -- ``layer.candidate.{authority,identity,
evidence_state}``) como VERDAD DE TERRENO para decidir el alcance: solo las
unidades cuyo bloqueador REAL en runtime era autoridad/identidad (no
evidence_state/antibot/error de acceso) entran en el analisis de Mision D.
Las unidades bloqueadas por antibot se reportan aparte, fuera de alcance.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from scripts.eval import medir_golden_set as golden_evaluator
from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2 import authority
from scripts.fase2_municipios.v2.agents.judge import JudgeOutcome
from scripts.fase2_municipios.v2.agents.orchestration import (
    ABCOrchestrator,
    DecisionProposal,
    ProposalValidationError,
    _FinalGateFailure,
)
from scripts.fase2_municipios.v2.eval.structural_evidence import structural_candidate
from scripts.fase2_municipios.v2.snapshot import EvidenceSource, build_snapshot


REPORT_COLUMNS = (
    "run_dir", "file", "municipio", "bucket", "attempt",
    "fetch_state", "a_state", "a_decision", "a_confidence",
    "b_state", "b_result", "c_state", "c_decision",
    "candidate_authority", "candidate_identity", "candidate_source_kind",
    "candidate_evidence_state", "candidate_accessible", "content_complete",
    "safety_blockers", "citation_status", "reason_code", "judge_invoked",
    "final_recomputed_status", "final_recomputed_decision",
    "replayable", "note",
    "truth_final", "truth_cause_kind", "truth_cause_code",
    "truth_authority", "truth_identity", "truth_evidence_state",
    "truth_in_scope", "reconstruction_matches_truth",
)


@dataclass
class UnitReplay:
    run_dir: str
    file: str
    municipio: str
    bucket: str
    attempt: int
    fetch_state: str = ""
    a_state: str = ""
    a_decision: str = ""
    a_confidence: str = ""
    b_state: str = ""
    b_result: str = ""
    c_state: str = ""
    c_decision: str = ""
    candidate_authority: str = ""
    candidate_identity: str = ""
    candidate_source_kind: str = ""
    candidate_evidence_state: str = ""
    candidate_accessible: bool | None = None
    content_complete: bool | None = None
    safety_blockers: str = ""
    citation_status: str = ""
    reason_code: str = ""
    judge_invoked: bool | None = None
    final_recomputed_status: str = ""
    final_recomputed_decision: str = ""
    replayable: bool = False
    note: str = ""
    truth_final: str = ""
    truth_cause_kind: str = ""
    truth_cause_code: str = ""
    truth_authority: str = ""
    truth_identity: str = ""
    truth_evidence_state: str = ""
    truth_in_scope: bool | None = None
    reconstruction_matches_truth: bool | None = None

    def as_row(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in REPORT_COLUMNS}


# Estados de evidencia bajo los que el gate SI puede confirmar (mismo set que
# ABCOrchestrator._safety_blockers). Cuando el candidato real en runtime
# quedo fuera de este set (tipicamente "incompleta_antibot"), el bloqueo real
# fue evidencia/antibot -- no autoridad -- y la unidad queda fuera del
# alcance de Mision D sin importar lo que este replay recomponga.
_CONFIRMABLE_EVIDENCE_STATES = frozenset({"completa", "renderizada"})


def _load_checkpoint_truth(run_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Real runtime candidate/cause per unit, straight from checkpoint.json.

    This is ground truth (computed once, at the actual run, with the real
    HTML this replay cannot reconstruct) -- used only to SCOPE the diagnosis
    and to sanity-check the replay's own reconstruction, never to recompute
    anything itself.
    """
    checkpoint_path = run_dir / "checkpoint.json"
    truth: dict[tuple[str, str], dict[str, Any]] = {}
    if not checkpoint_path.is_file():
        return truth
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return truth
    for record in (payload.get("units") or {}).values():
        if not isinstance(record, Mapping):
            continue
        municipio = str(record.get("municipio", ""))
        bucket = str(record.get("bucket", ""))
        result = record.get("result") or {}
        outcome = result.get("outcome") or {}
        cause = outcome.get("cause") or {}
        layer = outcome.get("layer") or {}
        candidate = layer.get("candidate") or {}
        key = (golden_evaluator.muni_key(municipio), bucket)
        truth[key] = {
            "final": result.get("final", ""),
            "cause_kind": cause.get("kind", ""),
            "cause_code": cause.get("code", ""),
            "authority": candidate.get("authority", ""),
            "identity": candidate.get("identity", ""),
            "evidence_state": candidate.get("evidence_state", ""),
        }
    return truth


def _not_replayable(
    *, run_dir: str, file: str, municipio: str, bucket: str, attempt: int,
    note: str, fetch_state: str = "", a_state: str = "", b_state: str = "",
    c_state: str = "",
) -> UnitReplay:
    return UnitReplay(
        run_dir=run_dir, file=file, municipio=municipio, bucket=bucket,
        attempt=attempt, fetch_state=fetch_state, a_state=a_state,
        b_state=b_state, c_state=c_state, replayable=False, note=note,
    )


def _reconstruct_content(source: Mapping[str, Any]) -> tuple[str, bool]:
    """Rebuild the best-effort original content string from a bounded
    observability capture (``eval.live_observability._bounded_snapshot``).

    Captured segments (citation ranges + head) are placed at their exact
    original offsets; gaps outside any captured segment are filled with a
    space so downstream offsets still line up. Citation verification only
    ever slices a captured range, so the filler is never read for that
    check. Returns ``(content, complete)`` where ``complete`` is True only
    when the capture was not truncated (the whole original page fits).
    """
    original_length = int(source.get("original_length") or 0)
    segments = source.get("content_segments") or ()
    buffer = [" "] * original_length
    for segment in segments:
        start = int(segment.get("original_start", 0))
        text = segment.get("text", "")
        for offset, ch in enumerate(text):
            index = start + offset
            if 0 <= index < original_length:
                buffer[index] = ch
    complete = not bool(source.get("content_truncated"))
    return "".join(buffer), complete


def _candidate_snapshot(fetch_raw: Mapping[str, Any], content: str) -> cascade.EvidenceSnapshot:
    status = fetch_raw.get("status")
    return cascade.EvidenceSnapshot(
        html="",
        text=content,
        title=str(fetch_raw.get("title", "")),
        final_url=str(fetch_raw.get("final_url", "")),
        requested_url=str(fetch_raw.get("requested_url", "")),
        status=status if isinstance(status, int) else None,
        source="orion_http",
        # "completa"/"renderizada" son equivalentes para _safety_blockers;
        # el estado real (no persistido en los artefactos) no cambia el
        # resultado del gate salvo por error_fetch, que ya cubre `status`.
        evidence_state="completa",
    )


def _build_candidate(
    municipio: str, bucket: str, fetch_raw: Mapping[str, Any], content: str,
) -> cascade.CandidateRecord:
    snapshot = _candidate_snapshot(fetch_raw, content)
    provenance = authority.redirect_provenance(
        str(fetch_raw.get("requested_url", "")),
        str(fetch_raw.get("final_url", "")),
        municipio,
    )
    return structural_candidate(
        requested_url=str(fetch_raw.get("requested_url", "")),
        source="orion_http",
        tier="live",
        municipio=municipio,
        bucket=bucket,
        evidence=snapshot,
        provenance=provenance,
    )


def _proposal_a_dict(
    a_raw: Mapping[str, Any], candidate: cascade.CandidateRecord,
) -> dict[str, Any]:
    # El candidate_id original se computo contra el snapshot COMPLETO en
    # vivo (con HTML/links que este replay no tiene persistidos); se
    # reemplaza por el id del candidato reconstruido para que el lookup de
    # ABCOrchestrator lo encuentre -- el resto de la propuesta de A (la
    # decision congelada, sus citas, su razon) no se toca.
    patched = dict(a_raw)
    patched["candidate_id"] = candidate.candidate_id
    return ABCOrchestrator._proposal_from_certifier(patched, (candidate,))


class _ReplayJudge:
    """Never calls a model: returns the frozen recorded decision, or raises
    if the orchestrator tries to invoke it when the original run did not."""

    def __init__(self, outcome: JudgeOutcome | None) -> None:
        self._outcome = outcome
        self.called = False

    def choose(self, **_kwargs: Any) -> JudgeOutcome:
        self.called = True
        if self._outcome is None:
            raise AssertionError(
                "replay judge invoked but the original run never recorded "
                "a judge decision for this unit (C state was skipped/"
                "not_started) -- consensus/aggregation deviates from replay"
            )
        return self._outcome


def _safety_blockers_text(candidate: cascade.CandidateRecord) -> str:
    return ";".join(ABCOrchestrator._safety_blockers(candidate))


def _citation_status(
    proposal_a: Mapping[str, Any], model_snapshot: Any,
) -> str:
    try:
        proposal = DecisionProposal.from_mapping(proposal_a)
    except ProposalValidationError as exc:
        return f"proposal_invalid:{exc}"
    if proposal.decision not in {
        "indice_oficial", "indice_oficial_combinado", "portal_externo_oficial",
    }:
        return "not_affirmative"
    try:
        citations = ABCOrchestrator._strict_citations(model_snapshot, proposal)
    except _FinalGateFailure as exc:
        return f"rejected:{exc.reason}"
    return "verified" if citations else "no_citations"


def _attach_truth(
    row: UnitReplay, truth: Mapping[tuple[str, str], dict[str, Any]],
) -> UnitReplay:
    """Fill the ``truth_*``/scope fields from checkpoint.json ground truth,
    and flag whether this replay's own (reconstructed) candidate agrees with
    what actually happened at runtime -- never changes the recomputed
    decision itself, purely diagnostic."""
    key = (golden_evaluator.muni_key(row.municipio), row.bucket)
    entry = truth.get(key)
    if entry is None:
        return row
    row.truth_final = str(entry.get("final", ""))
    row.truth_cause_kind = str(entry.get("cause_kind", ""))
    row.truth_cause_code = str(entry.get("cause_code", ""))
    row.truth_authority = str(entry.get("authority", ""))
    row.truth_identity = str(entry.get("identity", ""))
    row.truth_evidence_state = str(entry.get("evidence_state", ""))
    if row.truth_evidence_state:
        row.truth_in_scope = row.truth_evidence_state in _CONFIRMABLE_EVIDENCE_STATES
    if row.replayable and row.truth_authority and row.truth_identity:
        row.reconstruction_matches_truth = (
            row.candidate_authority == row.truth_authority
            and row.candidate_identity == row.truth_identity
        )
    return row


def replay_unit(
    path: Path, *, run_dir_label: str,
    truth: Mapping[tuple[str, str], dict[str, Any]] = None,  # type: ignore[assignment]
) -> UnitReplay:
    return _attach_truth(_replay_unit_core(path, run_dir_label=run_dir_label), truth or {})


def _replay_unit_core(path: Path, *, run_dir_label: str) -> UnitReplay:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio="", bucket="",
            attempt=0, note=f"unreadable_json:{exc}",
        )

    unit = payload.get("unit") or {}
    municipio = str(unit.get("municipio", ""))
    bucket = str(unit.get("bucket", ""))
    attempt = int(payload.get("attempt", 1))
    stages = payload.get("stages") or {}

    fetch = stages.get("fetch") or {}
    fetch_state = str(fetch.get("state", ""))
    if fetch_state != "raw_received" or not isinstance(fetch.get("raw"), Mapping):
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            note="fetch_not_available",
        )
    fetch_raw = fetch["raw"]

    a = stages.get("A") or {}
    a_state = str(a.get("state", ""))
    if a_state != "raw_received" or not isinstance(a.get("raw"), Mapping):
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            a_state=a_state, note="a_not_available",
        )
    a_raw = a["raw"]

    b = stages.get("B") or {}
    b_state = str(b.get("state", ""))
    b_raw = b.get("raw")
    if not isinstance(b_raw, Mapping):
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            a_state=a_state, b_state=b_state, note="b_not_available",
        )

    c = stages.get("C") or {}
    c_state = str(c.get("state", ""))
    c_raw = c.get("raw") if isinstance(c.get("raw"), Mapping) else {}
    if c_state == "raw_received" and c_raw.get("decision"):
        judge_outcome: JudgeOutcome | None = JudgeOutcome(
            decision=str(c_raw.get("decision")), reason=str(c_raw.get("reason", "")),
        )
    elif c_state in {"skipped", "not_started", ""}:
        judge_outcome = None
    else:
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            a_state=a_state, b_state=b_state, c_state=c_state,
            note=f"c_not_replayable:{c_state}",
        )

    sources_json = (payload.get("evidence_snapshot") or {}).get("sources") or ()
    content_by_id: dict[str, str] = {}
    content_complete = True
    for source in sources_json:
        source_id = str(source.get("source_id", ""))
        text, complete = _reconstruct_content(source)
        content_by_id[source_id] = text
        content_complete = content_complete and complete
    main_content = content_by_id.get("main", "")

    candidate = _build_candidate(municipio, bucket, fetch_raw, main_content)

    try:
        proposal_a = _proposal_a_dict(a_raw, candidate)
    except Exception as exc:  # noqa: BLE001 - diagnostic tool, never crash the batch
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            a_state=a_state, b_state=b_state, c_state=c_state,
            note=f"proposal_a_build_failed:{type(exc).__name__}:{exc}",
        )
    proposal_b = ABCOrchestrator._proposal_from_prosecutor(b_raw, proposal_a)
    prosecutor_result = str(b_raw.get("result", ""))

    try:
        model_snapshot = build_snapshot(
            EvidenceSource(
                source_id=str(source.get("source_id", "")),
                url=str(source.get("url", "")),
                retrieved_at=datetime.fromisoformat(str(source.get("retrieved_at"))),
                content=content_by_id[str(source.get("source_id", ""))],
            )
            for source in sources_json
        )
    except Exception as exc:  # noqa: BLE001
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            a_state=a_state, b_state=b_state, c_state=c_state,
            note=f"snapshot_build_failed:{type(exc).__name__}:{exc}",
        )

    judge = _ReplayJudge(judge_outcome)
    try:
        result = ABCOrchestrator(judge=judge).resolve(
            snapshot=model_snapshot,
            candidates=(candidate,),
            proposal_a=proposal_a,
            proposal_b=proposal_b,
            requested_bucket=bucket,
            prosecutor_result=prosecutor_result,
        )
    except AssertionError as exc:
        return _not_replayable(
            run_dir=run_dir_label, file=path.name, municipio=municipio,
            bucket=bucket, attempt=attempt, fetch_state=fetch_state,
            a_state=a_state, b_state=b_state, c_state=c_state,
            note=f"replay_judge_mismatch:{exc}",
        )

    return UnitReplay(
        run_dir=run_dir_label, file=path.name, municipio=municipio, bucket=bucket,
        attempt=attempt, fetch_state=fetch_state, a_state=a_state,
        a_decision=str(a_raw.get("decision", "")),
        a_confidence=str(a_raw.get("confidence", "")),
        b_state=b_state, b_result=prosecutor_result, c_state=c_state,
        c_decision=str(c_raw.get("decision", "")) if c_raw else "",
        candidate_authority=candidate.authority,
        candidate_identity=candidate.identity,
        candidate_source_kind=candidate.source_kind,
        candidate_evidence_state=candidate.evidence_state,
        candidate_accessible=candidate.accessible,
        content_complete=content_complete,
        safety_blockers=_safety_blockers_text(candidate),
        citation_status=_citation_status(proposal_a, model_snapshot),
        reason_code=result.reason_code,
        judge_invoked=result.judge_invoked,
        final_recomputed_status=result.final_decision.status,
        final_recomputed_decision=result.final_decision.decision,
        replayable=True,
        note="",
    )


def replay_run_dir(run_dir: Path, *, label: str) -> list[UnitReplay]:
    observability_dir = run_dir / "observability"
    if not observability_dir.is_dir():
        raise FileNotFoundError(f"no observability/ under {run_dir}")
    truth = _load_checkpoint_truth(run_dir)
    rows = []
    for path in sorted(observability_dir.glob("*.json")):
        rows.append(replay_unit(path, run_dir_label=label, truth=truth))
    return rows


def _load_progress_finals(progress_path: Path) -> dict[tuple[str, str], str]:
    """Keyed by (muni_key, bucket) -- progress.csv's own ``municipio``
    column is already muni_key-normalized (lowercase, no spaces/accents),
    same join key used against checkpoint.json truth and the observability
    JSON's title-case ``unit.municipio`` (normalized at lookup time)."""
    finals: dict[tuple[str, str], str] = {}
    if not progress_path.is_file():
        return finals
    with progress_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (
                golden_evaluator.muni_key(row.get("municipio", "")),
                row.get("bucket", ""),
            )
            finals[key] = row.get("final", "")
    return finals


def _pick_best(rows: list[UnitReplay], *, run_dir_order: list[str]) -> UnitReplay:
    """Prefer a replayable row; among replayable rows, prefer the run_dir
    passed later on the command line (typically the paid retry), then the
    highest attempt number."""
    def rank(row: UnitReplay) -> tuple[int, int, int]:
        replayable_rank = 1 if row.replayable else 0
        try:
            dir_rank = run_dir_order.index(row.run_dir)
        except ValueError:
            dir_rank = -1
        return (replayable_rank, dir_rank, row.attempt)

    return max(rows, key=rank)


def write_report(rows: list[UnitReplay], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_row())


def summarize(
    rows: list[UnitReplay], *, run_dir_order: list[str],
    progress_finals: dict[tuple[str, str], str],
) -> str:
    # Agrupar por (muni_key, bucket) -- la misma clave de join que
    # progress.csv/checkpoint.json usan (normalizada, sin acentos/espacios),
    # nunca por el municipio en title-case tal cual viene del JSON.
    by_unit: dict[tuple[str, str], list[UnitReplay]] = {}
    for row in rows:
        by_unit.setdefault(
            (golden_evaluator.muni_key(row.municipio), row.bucket), []
        ).append(row)

    flips: list[str] = []
    still_review_in_scope: list[str] = []
    out_of_scope: list[str] = []
    mismatched: list[str] = []
    not_replayed: list[str] = []
    for key, unit_rows in sorted(by_unit.items()):
        best = _pick_best(unit_rows, run_dir_order=run_dir_order)
        label = f"{best.municipio or key[0]}/{best.bucket or key[1]}"
        if not best.replayable:
            not_replayed.append(f"{label} ({best.note})")
            continue
        recorded = progress_finals.get(key, "")
        in_scope = best.truth_in_scope is not False  # True or unknown (no truth) both count
        if best.truth_in_scope is False:
            out_of_scope.append(
                f"{label}: truth_evidence_state={best.truth_evidence_state!r} "
                f"truth_cause={best.truth_cause_kind}/{best.truth_cause_code} "
                "(no es un gap de autoridad, fuera de Mision D)"
            )
        if best.reconstruction_matches_truth is False:
            mismatched.append(
                f"{label}: reconstruido authority={best.candidate_authority!r} "
                f"identity={best.candidate_identity!r} vs runtime real "
                f"authority={best.truth_authority!r} identity={best.truth_identity!r}"
            )
        if best.final_recomputed_status == "confirmado" and recorded == "revisar":
            flips.append(
                f"{label}: revisar -> {best.final_recomputed_decision} "
                f"(reason_code={best.reason_code}; in_scope={in_scope})"
            )
        elif best.final_recomputed_status != "confirmado" and in_scope:
            still_review_in_scope.append(
                f"{label}: blockers={best.safety_blockers!r} "
                f"citation={best.citation_status} reason_code={best.reason_code}"
            )

    lines = [
        f"unidades replayables: {sum(1 for r in rows if r.replayable)}/{len(rows)}",
        f"flips revisar->confirmada: {len(flips)}",
        *[f"  + {line}" for line in flips],
        f"siguen en revisar (EN ALCANCE, no antibot/evidencia): {len(still_review_in_scope)}",
        *[f"  - {line}" for line in still_review_in_scope],
        f"fuera de alcance (bloqueo real no fue autoridad, p.ej. antibot): {len(out_of_scope)}",
        *[f"  ~ {line}" for line in out_of_scope],
    ]
    if mismatched:
        lines.append(
            f"AVISO reconstruccion != runtime real (revisar antes de confiar): {len(mismatched)}"
        )
        lines.extend(f"  ! {line}" for line in mismatched)
    if not_replayed:
        lines.append(f"no replayables: {len(not_replayed)}")
        lines.extend(f"  ? {line}" for line in not_replayed)
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", action="append", required=True, dest="run_dirs",
        help="Directorio de corrida con observability/ y progress.csv adentro "
             "(repetible; el orden importa para desempate: el ultimo gana).",
    )
    parser.add_argument("--output", type=Path, default=None, help="CSV de salida (opcional).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_dirs = [Path(value) for value in args.run_dirs]
    all_rows: list[UnitReplay] = []
    progress_finals: dict[tuple[str, str], str] = {}
    for run_dir in run_dirs:
        label = run_dir.name
        all_rows.extend(replay_run_dir(run_dir, label=label))
        progress_finals.update(_load_progress_finals(run_dir / "progress.csv"))

    if args.output is not None:
        write_report(all_rows, args.output)

    run_dir_order = [path.name for path in run_dirs]
    print(summarize(all_rows, run_dir_order=run_dir_order, progress_finals=progress_finals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
