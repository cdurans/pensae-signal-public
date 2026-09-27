"""Validated opportunity assembly for the research workflow."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.domain.opportunity import (
    OpportunityIdentity,
    ProblemSignal,
    ProblemSimilarityInput,
    identity_fingerprint,
    problem_similarity_text,
)
from pensae.opportunities import (
    OpportunityAggregate,
    PatternInput,
    ProvenanceInput,
    SignalInput,
    SourceInput,
)
from pensae.opportunities.writer import score_opportunity_report
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.roles import (
    REQUIRED_CLAIM_LABELS,
    RoleExecutionResult,
    RoleName,
    RoleSpec,
    RoleValueValidationCode,
)
from pensae.research.schemas import OpportunityAnalysis
from pensae.research.state import ConceptCandidate, FocusedSource, SelectedSource


class OpportunityAssembler:
    """Build a complete aggregate only after gate and focused validation succeed."""

    def __init__(self, context: ResearchRunContext, calls: BoundedExternalCalls) -> None:
        self.context = context
        self.calls = calls
        self._source_inputs: dict[UUID, SourceInput] = {}

    @property
    def _run_id(self) -> UUID:
        return self.context.run_id

    @property
    def _specs(self) -> Mapping[RoleName, RoleSpec[BaseModel]]:
        return self.context.specs

    async def _build_aggregate(
        self,
        concept: ConceptCandidate,
        focused: FocusedSource,
        *,
        seed: int,
        bounds: ProtectedWorkflowBounds,
    ) -> tuple[OpportunityAggregate | None, RoleExecutionResult[OpportunityAnalysis], int]:
        spec = cast(RoleSpec[OpportunityAnalysis], self._specs[RoleName.OPPORTUNITY_ANALYST])
        evidence_ids = tuple(
            str(item.id)
            for group in (*concept.pattern.selected, focused)
            for item in group.evidence
        )
        evidence_contract = tuple(
            {"id": str(item.id), "kind": item.evidence_kind}
            for group in (*concept.pattern.selected, focused)
            for item in group.evidence
        )
        retained_sources = (*concept.pattern.selected, focused)
        source_origin_count = len(
            {urlsplit(source.retrieved.final_url).netloc.casefold() for source in retained_sources}
        )
        has_conflicting_evidence = any(
            item.evidence_kind == "conflicting"
            for source in retained_sources
            for item in source.evidence
        )

        def validate_report(report: OpportunityAnalysis) -> tuple[RoleValueValidationCode, ...]:
            return self._validate_final_report(
                report,
                expected_target_segment=concept.strategy.target_segment,
                expected_likely_buyer=concept.strategy.likely_buyer,
                evidence_contract=evidence_contract,
                source_origin_count=source_origin_count,
                has_conflicting_evidence=has_conflicting_evidence,
            )

        spec = replace(spec, value_validator=validate_report)
        result = await self.calls._model(
            spec,
            trusted_input={
                "candidate": concept.pattern.candidate.model_dump(mode="json"),
                "strategy": concept.strategy.model_dump(mode="json"),
                "allowed_evidence_ids": evidence_ids,
                "evidence_contract": evidence_contract,
                "analysis_contract": {
                    "source_origin_count": source_origin_count,
                    "has_conflicting_evidence": has_conflicting_evidence,
                },
                "required_claim_labels": (
                    "fact",
                    "inference",
                    "estimate",
                    "assumption",
                    "hypothesis",
                    "conflict",
                    "missing_evidence",
                ),
            },
            untrusted=focused.untrusted_spans,
            seed=seed,
            timeout_seconds=bounds.model_timeout_seconds,
        )
        report = result.value
        if report is None:
            return None, result, 0
        if (
            report.target_segment != concept.strategy.target_segment
            or report.likely_buyer != concept.strategy.likely_buyer
        ):
            raise ValueError("final report changed the gate-approved segment or buyer")
        # Validate the protected deterministic score/verdict before any similarity
        # embedding or lifecycle classification. The writer recomputes the same pure
        # result immediately before its short transaction.
        score_opportunity_report(report)
        similarity_text = problem_similarity_text(
            ProblemSimilarityInput(
                problem=report.problem_pattern,
                affected_user=report.affected_user,
                current_workaround=report.current_workaround,
                desired_outcome=concept.strategy.desired_outcome,
            )
        )
        embedded = await self.calls._embed((similarity_text,), bounds, role="similarity_embedding")
        opportunity_vector = embedded.vectors[0]
        signal_ids = tuple(self.context.id_factory() for _ in concept.pattern.selected)
        policy = self.context.protected.research
        identity = OpportunityIdentity(
            problem_pattern=report.problem_pattern,
            proposed_solution=report.proposed_solution,
            target_segment=report.target_segment,
            likely_buyer=report.likely_buyer,
            recurring_workflow=report.recurring_workflow,
        )
        return (
            OpportunityAggregate(
                opportunity_id=self.context.id_factory(),
                version_id=self.context.id_factory(),
                lifecycle_event_id=self.context.id_factory(),
                run_id=self._run_id,
                identity_fingerprint=identity_fingerprint(
                    identity, version=policy.fingerprint_version
                ),
                primary_industry=report.primary_industry,
                report=report,
                sources=(
                    *tuple(self._source(item) for item in concept.pattern.selected),
                    self._source(focused),
                ),
                evidence=tuple(
                    item
                    for group in (*concept.pattern.selected, focused)
                    for item in group.evidence
                ),
                signals=tuple(
                    SignalInput(
                        id=signal_id,
                        evidence_ids=(
                            tuple(item.id for item in source.evidence)
                            + (
                                tuple(item.id for item in focused.evidence)
                                if signal_id == signal_ids[0]
                                else ()
                            )
                        ),
                        affected_user=source.selection.affected_user,
                        recurring_workflow=source.selection.recurring_workflow,
                        current_workaround=source.selection.current_workaround,
                        business_consequence=source.selection.business_consequence,
                        confidence=Decimal(str(source.selection.confidence)),
                    )
                    for signal_id, source in zip(signal_ids, concept.pattern.selected, strict=True)
                ),
                pattern=PatternInput(
                    id=self.context.id_factory(),
                    signal_ids=signal_ids,
                    summary=report.problem_pattern,
                    fingerprint=concept.pattern.fingerprint,
                    embedding=concept.pattern.embedding,
                    embedding_model_id=self.context.protected.policy.embedding_model_id,
                    fingerprint_version=policy.fingerprint_version,
                ),
                opportunity_embedding=opportunity_vector,
                provenance=ProvenanceInput(
                    chat_model_id=self.context.protected.policy.chat_model_id,
                    embedding_model_id=self.context.protected.policy.embedding_model_id,
                    workflow_version=policy.workflow_version,
                    prompt_versions=self._prompt_versions(),
                    schema_version=policy.schema_version,
                    fingerprint_version=policy.fingerprint_version,
                    threshold_version=policy.similarity_threshold_version,
                    model_parameter_version=policy.model_parameter_version,
                ),
            ),
            result,
            embedded.input_tokens,
        )

    @staticmethod
    def _validate_final_report(
        report: OpportunityAnalysis,
        *,
        expected_target_segment: str,
        expected_likely_buyer: str,
        evidence_contract: tuple[dict[str, str], ...],
        source_origin_count: int,
        has_conflicting_evidence: bool,
    ) -> tuple[RoleValueValidationCode, ...]:
        """Validate closed deterministic report invariants before similarity embedding."""

        failures: list[RoleValueValidationCode] = []
        if report.target_segment != expected_target_segment:
            failures.append(RoleValueValidationCode.FINAL_SEGMENT_MISMATCH)
        if report.likely_buyer != expected_likely_buyer:
            failures.append(RoleValueValidationCode.FINAL_BUYER_MISMATCH)

        expected_by_kind = {
            kind: {item["id"] for item in evidence_contract if item["kind"] == kind}
            for kind in ("supporting", "negative", "conflicting")
        }
        reported_lists = {
            "supporting": report.supporting_evidence_ids,
            "negative": report.negative_evidence_ids,
            "conflicting": report.conflicting_evidence_ids,
        }
        reported_flat = tuple(
            evidence_id for evidence_ids in reported_lists.values() for evidence_id in evidence_ids
        )
        reported_ids = set(reported_flat)
        expected_ids = {item["id"] for item in evidence_contract}
        if len(reported_flat) != len(reported_ids):
            failures.append(RoleValueValidationCode.FINAL_EVIDENCE_CATEGORY_OVERLAP)
        if reported_ids != expected_ids:
            failures.append(RoleValueValidationCode.FINAL_EVIDENCE_SET_MISMATCH)
        if any(set(reported_lists[kind]) != expected_by_kind[kind] for kind in expected_by_kind):
            failures.append(RoleValueValidationCode.FINAL_EVIDENCE_KIND_MISMATCH)

        if {claim.label for claim in report.claims} != set(REQUIRED_CLAIM_LABELS):
            failures.append(RoleValueValidationCode.FINAL_CLAIM_LABELS_INCOMPLETE)
        if any(set(claim.evidence_ids) - expected_ids for claim in report.claims):
            failures.append(RoleValueValidationCode.FINAL_CLAIM_EVIDENCE_UNKNOWN)
        if any(claim.label == "fact" and not claim.evidence_ids for claim in report.claims):
            failures.append(RoleValueValidationCode.FINAL_FACT_EVIDENCE_MISSING)
        if any(claim.label == "conflict" and not claim.evidence_ids for claim in report.claims):
            failures.append(RoleValueValidationCode.FINAL_CONFLICT_EVIDENCE_MISSING)
        if has_conflicting_evidence and (
            not report.conflict_limitation or not report.conflict_limitation.strip()
        ):
            failures.append(RoleValueValidationCode.FINAL_CONFLICT_LIMITATION_MISSING)
        if source_origin_count < 3 and (
            not report.source_diversity_limitation or not report.source_diversity_limitation.strip()
        ):
            failures.append(RoleValueValidationCode.FINAL_SOURCE_DIVERSITY_LIMITATION_MISSING)
        try:
            score_opportunity_report(report)
        except ValueError:
            failures.append(RoleValueValidationCode.FINAL_SCORE_INVALID)
        return tuple(failures)

    @staticmethod
    def _problem_signal(source: SelectedSource) -> ProblemSignal:
        return ProblemSignal(
            source_id=source.selection.source_id,
            evidence_ids=tuple(str(item.id) for item in source.evidence),
            affected_user=source.selection.affected_user,
            recurring_workflow=source.selection.recurring_workflow,
            current_workaround=source.selection.current_workaround,
            business_consequence=source.selection.business_consequence,
            confidence=source.selection.confidence,
        )

    def _source(self, item: SelectedSource | FocusedSource) -> SourceInput:
        retained = self._source_inputs.get(item.id)
        candidate = SourceInput(
            id=item.id,
            url=item.retrieved.final_url,
            title=item.hit.title,
            retrieved_at=(retained.retrieved_at if retained is not None else self.context.now()),
            credibility_note="Retrieved through the protected public-source client.",
            limitation=(
                "Focused evidence includes a material conflict; interpretation is limited."
                if any(evidence.evidence_kind == "conflicting" for evidence in item.evidence)
                else None
            ),
            content_fingerprint=hashlib.sha256(item.retrieved.extracted_text.encode()).hexdigest(),
        )
        if retained is not None and retained != candidate:
            raise ValueError("source identifier was reused with different immutable content")
        self._source_inputs[item.id] = candidate
        return candidate

    def _prompt_versions(self) -> dict[str, str]:
        policy = self.context.protected.research
        return {
            "planner": policy.planner_prompt_version,
            "problem_analyst": policy.problem_analyst_prompt_version,
            "product_strategist": policy.product_strategist_prompt_version,
            "opportunity_analyst": policy.opportunity_analyst_prompt_version,
        }
