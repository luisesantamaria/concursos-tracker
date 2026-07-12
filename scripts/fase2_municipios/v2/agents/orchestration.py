"""Deterministic A/B/C arbitration over the existing final-decision chain."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2.agents.certifier import (
    AFFIRMATIVE_CERTIFIER_DECISIONS,
)
from scripts.fase2_municipios.v2.agents.judge import ConflictJudge
from scripts.fase2_municipios.v2.snapshot import (
    Citation,
    CitationVerificationError,
    EvidenceSnapshot,
    anchor_citation,
    verify_all,
)


PROPOSAL_DECISIONS = frozenset({
    *AFFIRMATIVE_CERTIFIER_DECISIONS,
    "detalle_individual_rechazado",
    "licitacao_rechazada",
    "concurso_cultural_rechazado",
    "nao_encontrado",
    "revisar",
})


class ProposalValidationError(ValueError):
    """Untrusted A/B proposal does not satisfy the orchestration adapter."""


@dataclass(frozen=True)
class DecisionProposal:
    decision: str
    bucket: str
    candidate_id: str
    resource_url: str
    citations: tuple[Mapping[str, Any], ...]
    reason: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DecisionProposal":
        if not isinstance(raw, Mapping):
            raise ProposalValidationError("proposal_not_object")
        required = (
            "decision", "bucket", "candidate_id", "resource_url", "citations", "reason"
        )
        if any(name not in raw for name in required):
            raise ProposalValidationError("proposal_missing_field")
        decision = raw["decision"]
        bucket = raw["bucket"]
        candidate_id = raw["candidate_id"]
        resource_url = raw["resource_url"]
        reason = raw["reason"]
        citations = raw["citations"]
        if not isinstance(decision, str) or decision not in PROPOSAL_DECISIONS:
            raise ProposalValidationError("proposal_invalid_decision")
        canonical_bucket = cascade._canonical_bucket(bucket) if isinstance(bucket, str) else ""
        if not canonical_bucket:
            raise ProposalValidationError("proposal_invalid_bucket")
        if not all(isinstance(value, str) for value in (candidate_id, resource_url, reason)):
            raise ProposalValidationError("proposal_invalid_string")
        if not isinstance(citations, (list, tuple)):
            raise ProposalValidationError("proposal_invalid_citations")
        detached: list[Mapping[str, Any]] = []
        for item in citations:
            if not isinstance(item, Mapping):
                raise ProposalValidationError("proposal_invalid_citation_item")
            detached.append(dict(item))
        return cls(
            decision=decision,
            bucket=canonical_bucket,
            candidate_id=candidate_id,
            resource_url=resource_url,
            citations=tuple(detached),
            reason=reason,
        )

    @property
    def semantic_key(self) -> tuple[str, str, str]:
        return (
            self.decision,
            self.bucket,
            cascade._normalized_candidate_url(self.resource_url),
        )

    def as_untrusted_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "bucket": self.bucket,
            "candidate_id": self.candidate_id,
            "resource_url": self.resource_url,
            "citations": [dict(item) for item in self.citations],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OrchestrationResult:
    final_decision: cascade.FinalDecision
    reason_code: str
    judge_invoked: bool
    capture_report: Any | None = None


@dataclass(frozen=True)
class CaptureBoundaryReport:
    captured: bool
    error_code: str | None = None


class CaptureSink(Protocol):
    """Write-only hook pre-bound to one already-structured learning candidate."""

    def capture(self) -> Any: ...


def _serialize_final_decision(final: cascade.FinalDecision) -> bytes:
    """Materialize a stable decision payload before any optional side effect."""
    return json.dumps(
        asdict(final),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class _FinalGateFailure(ValueError):
    def __init__(self, reason: str, *, invalid_citation: bool = False) -> None:
        self.reason = reason
        self.invalid_citation = invalid_citation
        super().__init__(reason)


class ABCOrchestrator:
    """Choose only A/B and always close through deterministic existing gates."""

    def __init__(self, *, judge: ConflictJudge) -> None:
        self.judge = judge

    @staticmethod
    def _proposal_from_certifier(
        output: Mapping[str, Any],
        candidates: tuple[cascade.CandidateRecord, ...],
    ) -> dict[str, Any]:
        candidate_id = output.get("candidate_id")
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        candidate = by_id.get(candidate_id)
        return {
            "decision": output.get("decision"),
            "bucket": output.get("bucket"),
            "candidate_id": candidate_id,
            "resource_url": candidate.final_url if candidate else "",
            "citations": output.get("citations"),
            "reason": output.get("reason"),
        }

    @staticmethod
    def _proposal_from_prosecutor(
        output: Mapping[str, Any], proposal_a: Mapping[str, Any]
    ) -> dict[str, Any]:
        if output.get("result") == "sustain":
            return dict(proposal_a)
        citations = list(output.get("citations", ()))
        for accusation in output.get("accusations", ()):
            if isinstance(accusation, Mapping):
                citations.extend(accusation.get("citations", ()))
        return {
            "decision": "revisar",
            "bucket": proposal_a.get("bucket"),
            "candidate_id": proposal_a.get("candidate_id"),
            "resource_url": proposal_a.get("resource_url"),
            "citations": citations,
            "reason": output.get("reason"),
        }

    def run(
        self,
        *,
        snapshot: EvidenceSnapshot,
        candidates: Iterable[cascade.CandidateRecord],
        task: str,
        certifier: Any,
        prosecutor: Any,
        capture_sink: CaptureSink | None = None,
        requested_bucket: str | None = None,
    ) -> OrchestrationResult:
        """Execute A, then B only for an affirmative A; C stays conditional."""
        candidate_tuple = tuple(candidates)
        certified = certifier.certify(snapshot=snapshot, task=task)
        certified_output = getattr(certified, "output", certified)
        if not isinstance(certified_output, Mapping):
            return OrchestrationResult(
                self._review(requested_bucket or "combinado", "proposal_invalid"),
                "proposal_invalid",
                False,
            )
        proposal_a = self._proposal_from_certifier(
            certified_output, candidate_tuple
        )
        if certified_output.get("decision") not in AFFIRMATIVE_CERTIFIER_DECISIONS:
            return self.resolve(
                snapshot=snapshot,
                candidates=candidate_tuple,
                proposal_a=proposal_a,
                proposal_b=proposal_a,
                capture_sink=capture_sink,
                requested_bucket=requested_bucket,
            )
        prosecuted = prosecutor.audit(
            snapshot=snapshot,
            certifier_output=self._normalize_combined_bucket(
                proposal_a, requested_bucket
            ),
        )
        prosecuted_output = getattr(prosecuted, "output", prosecuted)
        if not isinstance(prosecuted_output, Mapping):
            return OrchestrationResult(
                self._review(requested_bucket or "combinado", "proposal_invalid"),
                "proposal_invalid",
                False,
            )
        proposal_b = self._proposal_from_prosecutor(prosecuted_output, proposal_a)
        return self.resolve(
            snapshot=snapshot,
            candidates=candidate_tuple,
            proposal_a=proposal_a,
            proposal_b=proposal_b,
            capture_sink=capture_sink,
            requested_bucket=requested_bucket,
            prosecutor_result=str(prosecuted_output.get("result", "")),
        )

    @staticmethod
    def _normalize_combined_bucket(
        proposal: Mapping[str, Any], requested_bucket: str | None
    ) -> dict[str, Any]:
        normalized = dict(proposal)
        canonical_requested = (
            cascade._canonical_bucket(requested_bucket)
            if isinstance(requested_bucket, str) else ""
        )
        if (
            canonical_requested in {"concurso_publico", "processo_seletivo"}
            and normalized.get("decision") == "indice_oficial_combinado"
            and cascade._canonical_bucket(str(normalized.get("bucket", "")))
            == "combinado"
        ):
            normalized["bucket"] = canonical_requested
        elif (
            canonical_requested in {"concurso_publico", "processo_seletivo"}
            and normalized.get("decision") == "revisar"
            and normalized.get("bucket") == "desconocido"
        ):
            normalized["bucket"] = canonical_requested
        return normalized

    @staticmethod
    def _review(bucket: str, code: str, candidate_id: str = "") -> cascade.FinalDecision:
        return cascade._review_final(
            bucket,
            code,
            decision="revisar",
            candidate_id=candidate_id,
        )

    @staticmethod
    def _agree(a: DecisionProposal, b: DecisionProposal) -> bool:
        if a.decision == b.decision == "revisar":
            return True
        return a.semantic_key == b.semantic_key

    @staticmethod
    def _strict_citations(
        snapshot: EvidenceSnapshot, proposal: DecisionProposal
    ) -> tuple[Citation, ...]:
        try:
            citations = tuple(
                anchor_citation(snapshot, item, require_offsets=True)
                for item in proposal.citations
            )
            verify_all(snapshot, citations)
        except CitationVerificationError as exc:
            raise _FinalGateFailure(
                "citation_rejected", invalid_citation=True
            ) from exc
        return citations

    def _final_gate(
        self,
        *,
        snapshot: EvidenceSnapshot,
        candidates: tuple[cascade.CandidateRecord, ...],
        proposal: DecisionProposal,
    ) -> cascade.FinalDecision:
        if proposal.decision == "revisar":
            return self._review(proposal.bucket, "agreement_review", proposal.candidate_id)
        if proposal.decision not in AFFIRMATIVE_CERTIFIER_DECISIONS:
            raise _FinalGateFailure("proposal_not_affirmative")
        citations = self._strict_citations(snapshot, proposal)
        if not citations:
            raise _FinalGateFailure("affirmative_without_citations", invalid_citation=True)

        # Independencia total V1/V2 (directiva 12-jul): la autoridad SEMANTICA
        # es exclusiva de los agentes A/B/C. El codigo solo verifica hechos
        # objetivos: candidato existente, URL final identica, seguridad
        # estructural (autoridad/identidad/accesibilidad/estado de evidencia,
        # computadas deterministicamente por evidencia, jamas por regex de
        # contenido) y el pin de bucket fijado aguas arriba. La clasificacion
        # V1 (record.decision/page_role) NO se lee ni participa.
        record = self._proposal_record(candidates, proposal)
        if record is None:
            raise _FinalGateFailure("candidate_not_found")
        if (
            cascade._normalized_candidate_url(record.final_url)
            != cascade._normalized_candidate_url(proposal.resource_url)
        ):
            raise _FinalGateFailure("proposal_resource_mismatch")
        blockers = self._safety_blockers(record)
        if blockers:
            raise _FinalGateFailure(
                "structural_safety_rejected:" + ";".join(blockers)
            )
        canonical = cascade._canonical_bucket(proposal.bucket)
        if canonical not in {"concurso_publico", "processo_seletivo"}:
            raise _FinalGateFailure("bucket_invalid")
        return cascade.FinalDecision(
            bucket=canonical,
            status="confirmado",
            decision=proposal.decision,
            url=record.final_url,
            candidate_id=record.candidate_id,
            reason=(
                "v2_semantic_authority: consenso A/B con citas literales "
                f"verificadas; authority={record.authority}; "
                f"identity={record.identity}; "
                f"evidence_state={record.evidence_state}; "
                f"candidate_id={record.candidate_id}; "
                f"final_url={record.final_url}; snapshot preservado sin refetch"
            ),
        )

    @staticmethod
    def _proposal_record(
        candidates: tuple[cascade.CandidateRecord, ...],
        proposal: DecisionProposal,
    ) -> cascade.CandidateRecord | None:
        """Record de la propuesta por candidate_id, SIN el filtro de
        elegibilidad del selector (que exige decision V1 afirmativa: cuando la
        clasificacion semantica V1 esta equivocada, ese filtro vaciaria el
        pool y ocultaria el desacuerdo que la cola debe capturar)."""
        for record in candidates:
            if record.candidate_id == proposal.candidate_id:
                return record
        return None

    @staticmethod
    def _safety_blockers(record: cascade.CandidateRecord) -> list[str]:
        """Bloqueadores ESTRUCTURALES de derive_final_decision, sin el veto
        semantico (record.decision). Espejo exacto de cascade: autoridad,
        identidad y accesibilidad/estado de evidencia."""
        blockers: list[str] = []
        if record.authority != "confirmada":
            blockers.append(f"authority={record.authority}")
        if record.identity != "confirmada":
            blockers.append(f"identity={record.identity}")
        if not record.accessible or record.evidence_state not in {
            "completa", "renderizada",
        }:
            blockers.append(f"evidence_state={record.evidence_state}")
        return blockers

    def resolve(
        self,
        *,
        snapshot: EvidenceSnapshot,
        candidates: Iterable[cascade.CandidateRecord],
        proposal_a: Mapping[str, Any],
        proposal_b: Mapping[str, Any],
        capture_sink: CaptureSink | None = None,
        requested_bucket: str | None = None,
        prosecutor_result: str | None = None,
    ) -> OrchestrationResult:
        result = self._resolve_without_capture(
            snapshot=snapshot,
            candidates=candidates,
            proposal_a=proposal_a,
            proposal_b=proposal_b,
            requested_bucket=requested_bucket,
            prosecutor_result=prosecutor_result,
        )
        if capture_sink is None:
            return result
        try:
            _serialize_final_decision(result.final_decision)
            report = capture_sink.capture()
        except (OSError, TypeError, ValueError, UnicodeError):
            report = CaptureBoundaryReport(
                captured=False,
                error_code="capture_error",
            )
        return replace(result, capture_report=report)

    def _resolve_without_capture(
        self,
        *,
        snapshot: EvidenceSnapshot,
        candidates: Iterable[cascade.CandidateRecord],
        proposal_a: Mapping[str, Any],
        proposal_b: Mapping[str, Any],
        requested_bucket: str | None = None,
        prosecutor_result: str | None = None,
    ) -> OrchestrationResult:
        candidate_tuple = tuple(candidates)
        canonical_requested = (
            cascade._canonical_bucket(requested_bucket)
            if isinstance(requested_bucket, str) else ""
        )
        if requested_bucket is not None and canonical_requested not in {
            "concurso_publico", "processo_seletivo"
        }:
            return OrchestrationResult(
                self._review("combinado", "input_invalid"),
                "input_invalid",
                False,
            )
        if (
            not isinstance(snapshot, EvidenceSnapshot)
            or any(
                not isinstance(candidate, cascade.CandidateRecord)
                for candidate in candidate_tuple
            )
        ):
            return OrchestrationResult(
                self._review("combinado", "input_invalid"),
                "input_invalid",
                False,
            )
        try:
            a = DecisionProposal.from_mapping(
                self._normalize_combined_bucket(proposal_a, requested_bucket)
            )
            b = DecisionProposal.from_mapping(
                self._normalize_combined_bucket(proposal_b, requested_bucket)
            )
        except ProposalValidationError:
            return OrchestrationResult(
                self._review("combinado", "proposal_invalid"),
                "proposal_invalid",
                False,
            )

        review_bucket = canonical_requested or a.bucket
        if (
            canonical_requested
            and a.decision in AFFIRMATIVE_CERTIFIER_DECISIONS
            and a.bucket != canonical_requested
        ):
            return OrchestrationResult(
                self._review(review_bucket, "bucket_mismatch", a.candidate_id),
                "bucket_mismatch",
                False,
            )
        if a.decision not in AFFIRMATIVE_CERTIFIER_DECISIONS:
            code = (
                "agreement_review"
                if a.decision == "revisar" else "proposal_a_not_affirmative"
            )
            return OrchestrationResult(
                self._review(
                    review_bucket, code, a.candidate_id
                ),
                code,
                False,
            )
        if prosecutor_result not in {None, "sustain", "block", "review"}:
            return OrchestrationResult(
                self._review(review_bucket, "prosecutor_invalid", a.candidate_id),
                "prosecutor_invalid",
                False,
            )
        if prosecutor_result == "review":
            return OrchestrationResult(
                self._review(review_bucket, "prosecutor_review", a.candidate_id),
                "prosecutor_review",
                False,
            )
        if prosecutor_result == "sustain" and not self._agree(a, b):
            return OrchestrationResult(
                self._review(review_bucket, "prosecutor_invalid", a.candidate_id),
                "prosecutor_invalid",
                False,
            )
        if prosecutor_result == "block" and b.decision != "revisar":
            return OrchestrationResult(
                self._review(review_bucket, "prosecutor_invalid", a.candidate_id),
                "prosecutor_invalid",
                False,
            )

        if self._agree(a, b):
            try:
                final = self._final_gate(
                    snapshot=snapshot, candidates=candidate_tuple, proposal=a
                )
            except _FinalGateFailure:
                return OrchestrationResult(
                    self._review(a.bucket, "consensus_failed_final_gate", a.candidate_id),
                    "consensus_failed_final_gate",
                    False,
                )
            code = "agreement_review" if final.status == "revisar" else "consensus"
            return OrchestrationResult(final, code, False)

        # With an explicit direct-mode result, only a proved block reaches C.
        if prosecutor_result is not None and prosecutor_result != "block":
            return OrchestrationResult(
                self._review(review_bucket, "prosecutor_review", a.candidate_id),
                "prosecutor_review",
                False,
            )

        judged = self.judge.choose(
            snapshot=snapshot,
            # Higiene de independencia (directiva 12-jul): decision/bucket/
            # reason son marcadores tecnicos V1 (no_adjudicado_v1 /
            # structural_evidence_only_v2) o narrativa sin autoridad semantica.
            # Solo hechos objetivos de evidencia cruzan hacia C.
            candidates=(
                {
                    "candidate_id": candidate.candidate_id,
                    "final_url": candidate.final_url,
                    "authority": candidate.authority,
                    "identity": candidate.identity,
                    "evidence_state": candidate.evidence_state,
                }
                for candidate in candidate_tuple
            ),
            proposal_a=a.as_untrusted_payload(),
            proposal_b=b.as_untrusted_payload(),
        )
        if judged.error_code:
            return OrchestrationResult(
                self._review(a.bucket, judged.error_code),
                judged.error_code,
                True,
            )
        if judged.decision == "revisar":
            return OrchestrationResult(
                self._review(a.bucket, "judge_ambiguous"),
                "judge_ambiguous",
                True,
            )
        chosen = a if judged.decision == "aceptar_A" else b
        try:
            final = self._final_gate(
                snapshot=snapshot, candidates=candidate_tuple, proposal=chosen
            )
        except _FinalGateFailure as exc:
            code = (
                "judge_invalid_citation"
                if exc.invalid_citation
                else "judge_failed_final_gate"
            )
            return OrchestrationResult(
                self._review(chosen.bucket, code, chosen.candidate_id),
                code,
                True,
            )
        return OrchestrationResult(final, "judge_resolved", True)
