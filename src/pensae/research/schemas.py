"""Schema-constrained outputs for the research workflow."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


SearchQuery = Annotated[str, Field(min_length=2, max_length=96)]


class QueryPlan(StrictOutput):
    queries: tuple[SearchQuery, ...] = Field(min_length=1, max_length=8)


class SignalSelection(StrictOutput):
    affected_user: str = Field(min_length=1, max_length=240)
    recurring_workflow: str = Field(min_length=1, max_length=500)
    current_workaround: str = Field(min_length=1, max_length=500)
    business_consequence: str = Field(min_length=1, max_length=500)
    source_id: str = Field(min_length=1, max_length=64)
    span_ids: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class FocusedEvidenceSelection(StrictOutput):
    source_id: str = Field(min_length=1, max_length=64)
    span_ids: tuple[str, ...] = Field(min_length=1)
    supported_claim: str = Field(min_length=1, max_length=500)
    evidence_kind: str = Field(pattern=r"^(supporting|negative|conflicting)$")


class PatternSynthesis(StrictOutput):
    pattern_summary: str = Field(min_length=1, max_length=1_000)
    affected_user: str = Field(min_length=1, max_length=240)
    recurring_workflow: str = Field(min_length=1, max_length=500)
    current_workaround: str = Field(min_length=1, max_length=500)
    desired_outcome: str = Field(min_length=1, max_length=500)


class SegmentAssessment(StrictOutput):
    target_segment: str = Field(min_length=1, max_length=240)
    affected_user: str = Field(min_length=1, max_length=240)
    likely_buyer: str = Field(min_length=1, max_length=240)
    recurring_workflow: str = Field(min_length=1, max_length=500)
    frequency_value_hypothesis: str = Field(min_length=1, max_length=1_000)
    constraints: tuple[str, ...] = Field(min_length=1, max_length=5)
    reachability: str = Field(min_length=1, max_length=1_000)


class SegmentMap(StrictOutput):
    segments: tuple[SegmentAssessment, ...] = Field(min_length=1, max_length=3)


class SegmentSolution(StrictOutput):
    target_segment: str = Field(min_length=1, max_length=240)
    likely_buyer: str = Field(min_length=1, max_length=240)
    desired_outcome: str = Field(min_length=1, max_length=500)
    proposed_solution: str = Field(min_length=1, max_length=1_000)
    delivery_model: str = Field(min_length=1, max_length=240)


class ProposedScores(StrictOutput):
    commercial_attractiveness: int = Field(ge=1, le=5)
    commercial_explanation: str = Field(min_length=1, max_length=1_000)
    evidence_strength: int = Field(ge=1, le=5)
    evidence_explanation: str = Field(min_length=1, max_length=1_000)
    pensae_feasibility: int = Field(ge=1, le=5)
    feasibility_explanation: str = Field(min_length=1, max_length=1_000)
    differentiation: int = Field(ge=1, le=5)
    differentiation_explanation: str = Field(min_length=1, max_length=1_000)


class ClaimAssessment(StrictOutput):
    statement: str = Field(min_length=1, max_length=1_000)
    label: Literal[
        "fact",
        "inference",
        "estimate",
        "assumption",
        "hypothesis",
        "conflict",
        "missing_evidence",
    ]
    evidence_ids: tuple[str, ...] = ()


class OpportunityAnalysis(StrictOutput):
    opportunity_name: str = Field(min_length=1, max_length=240)
    concise_summary: str = Field(min_length=1, max_length=1_000)
    primary_industry: str = Field(min_length=1, max_length=240)
    problem_pattern: str = Field(min_length=1, max_length=2_000)
    target_segment: str = Field(min_length=1, max_length=240)
    affected_user: str = Field(min_length=1, max_length=240)
    likely_buyer: str = Field(min_length=1, max_length=240)
    recurring_workflow: str = Field(min_length=1, max_length=1_000)
    current_workaround: str = Field(min_length=1, max_length=1_000)
    business_consequences: tuple[str, ...] = Field(min_length=1)
    proposed_solution: str = Field(min_length=1, max_length=2_000)
    delivery_model: str = Field(min_length=1, max_length=240)
    initial_market_reason: str = Field(min_length=1, max_length=1_000)
    frequency_value_hypothesis: str = Field(min_length=1, max_length=1_000)
    other_segments: tuple[str, ...]
    alternatives: tuple[str, ...] = Field(min_length=1)
    missing_capabilities: tuple[str, ...]
    reusable_core_capabilities: tuple[str, ...] = Field(min_length=1)
    pricing_or_spend_signals: tuple[str, ...] = Field(min_length=1)
    market_saturation: str = Field(min_length=1, max_length=1_000)
    incumbent_response_risk: str = Field(min_length=1, max_length=1_000)
    integration_customization_burden: str = Field(min_length=1, max_length=1_000)
    preferred_technology_fit: str = Field(min_length=1, max_length=1_000)
    trust_regulatory_constraints: tuple[str, ...]
    operational_burden: str = Field(min_length=1, max_length=1_000)
    commercial_analysis: str = Field(min_length=1, max_length=2_000)
    feasibility_analysis: str = Field(min_length=1, max_length=2_000)
    differentiation_analysis: str = Field(min_length=1, max_length=2_000)
    risks: tuple[str, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = Field(min_length=1)
    reasons_not_to_pursue: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...] = Field(min_length=1)
    negative_evidence_ids: tuple[str, ...]
    conflicting_evidence_ids: tuple[str, ...]
    next_research_questions: tuple[str, ...] = Field(min_length=1)
    claims: tuple[ClaimAssessment, ...] = Field(min_length=1)
    source_diversity_limitation: str | None = Field(default=None, max_length=1_000)
    conflict_limitation: str | None = Field(default=None, max_length=1_000)
    proposed_scores: ProposedScores
    strong_evidence: bool
    plausible_buyer: bool
    payment_or_value_path: bool
    critical_blocker: bool
    strong_negative_evidence: bool
    implausible_economics: bool
    excessive_customization_or_operations: bool
