"""Deterministic A/B/C arbitration over the existing final-decision chain."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

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
    ) -> OrchestrationResult:
        """Execute A then B; C remains conditional inside ``resolve``."""
        candidate_tuple = tuple(candidates)
        certified = certifier.certify(snapshot=snapshot, task=task)
        prosecuted = prosecutor.audit(
            snapshot=snapshot,
            certifier_output=certified.output,
        )
        proposal_a = self._proposal_from_certifier(certified.output, candidate_tuple)
        proposal_b = self._proposal_from_prosecutor(prosecuted.output, proposal_a)
        return self.resolve(
            snapshot=snapshot,
            candidates=candidate_tuple,
            proposal_a=proposal_a,
            proposal_b=proposal_b,
        )

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
        selected = cascade.resolve_selector_pick(
            list(candidates), proposal.bucket, proposal.candidate_id
        )
        if not isinstance(selected, cascade.SelectedResource):
            raise _FinalGateFailure("selector_rejected_proposal")
        record = selected.candidate
        if record.decision != proposal.decision:
            raise _FinalGateFailure("proposal_decision_mismatch")
        if (
            cascade._normalized_candidate_url(record.final_url)
            != cascade._normalized_candidate_url(proposal.resource_url)
        ):
            raise _FinalGateFailure("proposal_resource_mismatch")
        final = cascade.derive_final_decision(selected)
        if final.status != "confirmado":
            raise _FinalGateFailure("existing_final_gate_rejected")
        return final

    def resolve(
        self,
        *,
        snapshot: EvidenceSnapshot,
        candidates: Iterable[cascade.CandidateRecord],
        proposal_a: Mapping[str, Any],
        proposal_b: Mapping[str, Any],
    ) -> OrchestrationResult:
        candidate_tuple = tuple(candidates)
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
            a = DecisionProposal.from_mapping(proposal_a)
            b = DecisionProposal.from_mapping(proposal_b)
        except ProposalValidationError:
            return OrchestrationResult(
                self._review("combinado", "proposal_invalid"),
                "proposal_invalid",
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

        judged = self.judge.choose(
            snapshot=snapshot,
            candidates=(
                {
                    "candidate_id": candidate.candidate_id,
                    "final_url": candidate.final_url,
                    "decision": candidate.decision,
                    "bucket": candidate.bucket,
                    "authority": candidate.authority,
                    "identity": candidate.identity,
                    "evidence_state": candidate.evidence_state,
                    "reason": candidate.reason,
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
