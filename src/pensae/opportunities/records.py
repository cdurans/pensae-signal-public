"""Validated durable opportunity and run records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pensae.research.schemas import OpportunityAnalysis
from pensae.research.workflow import LimitCode, ModelCallUsage, ShortfallCode

FORBIDDEN_DURABLE_KEYS = frozenset(
    {
        "page",
        "page_body",
        "full_page",
        "extracted_text",
        "normalized_text",
        "prompt",
        "raw_output",
        "repair_payload",
        "retry_payload",
        "search_response",
    }
)


class DurableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunSnapshot(DurableModel):
    id: UUID
    effective_config: dict[str, Any]
    workflow_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_processing_payloads(self) -> RunSnapshot:
        reject_forbidden_keys(self.effective_config)
        return self


class SourceInput(DurableModel):
    id: UUID
    url: str = Field(pattern=r"^https?://")
    title: str = Field(min_length=1)
    publisher: str | None = None
    publication_date: date | None = None
    retrieved_at: datetime
    credibility_note: str = Field(min_length=1)
    limitation: str | None = None
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceInput(DurableModel):
    id: UUID
    source_id: UUID
    excerpt: str = Field(min_length=1, max_length=500)
    supported_claim: str = Field(min_length=1)
    evidence_kind: Literal["supporting", "negative", "conflicting"]
    material_conflict: str | None = None


class SignalInput(DurableModel):
    id: UUID
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)
    affected_user: str = Field(min_length=1)
    recurring_workflow: str = Field(min_length=1)
    current_workaround: str = Field(min_length=1)
    business_consequence: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)


class PatternInput(DurableModel):
    id: UUID
    signal_ids: tuple[UUID, ...] = Field(min_length=1)
    summary: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding: tuple[float, ...]
    embedding_model_id: str = Field(min_length=1)
    fingerprint_version: str = Field(min_length=1)


class ProvenanceInput(DurableModel):
    chat_model_id: str = Field(min_length=1)
    embedding_model_id: str = Field(min_length=1)
    workflow_version: str = Field(min_length=1)
    prompt_versions: dict[str, str]
    schema_version: str = Field(min_length=1)
    fingerprint_version: str = Field(min_length=1)
    threshold_version: str = Field(min_length=1)
    model_parameter_version: str = Field(min_length=1)


class SimilarityRelationInput(DurableModel):
    id: UUID
    opportunity_id: UUID
    similarity: Decimal = Field(ge=0, le=1)
    relation_kind: Literal["related", "possible_rediscovery"]


class OpportunityAggregate(DurableModel):
    opportunity_id: UUID
    version_id: UUID
    lifecycle_event_id: UUID
    run_id: UUID
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    primary_industry: str = Field(min_length=1)
    report: OpportunityAnalysis
    sources: tuple[SourceInput, ...] = Field(min_length=1)
    evidence: tuple[EvidenceInput, ...] = Field(min_length=1)
    signals: tuple[SignalInput, ...] = Field(min_length=1)
    pattern: PatternInput
    opportunity_embedding: tuple[float, ...]
    provenance: ProvenanceInput
    relation_id: UUID | None = None
    related_opportunity_id: UUID | None = None
    relation_similarity: Decimal | None = Field(default=None, ge=0, le=1)
    relations: tuple[SimilarityRelationInput, ...] = Field(default=(), max_length=3)
    classification: Literal["new", "related", "possible_rediscovery"] = "new"

    @model_validator(mode="after")
    def validate_complete_chain(self) -> OpportunityAggregate:
        source_ids = {item.id for item in self.sources}
        evidence_ids = {item.id for item in self.evidence}
        signal_ids = {item.id for item in self.signals}
        if len(source_ids) != len(self.sources):
            raise ValueError("source IDs must be unique")
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("evidence IDs must be unique")
        if len(signal_ids) != len(self.signals):
            raise ValueError("signal IDs must be unique")
        if any(item.source_id not in source_ids for item in self.evidence):
            raise ValueError("every evidence item must reference an aggregate source")
        if any(set(item.evidence_ids) - evidence_ids for item in self.signals):
            raise ValueError("every signal must reference aggregate evidence")
        if set(self.pattern.signal_ids) - signal_ids:
            raise ValueError("the pattern must reference aggregate signals")
        report_ids_by_kind = {
            "supporting": set(self.report.supporting_evidence_ids),
            "negative": set(self.report.negative_evidence_ids),
            "conflicting": set(self.report.conflicting_evidence_ids),
        }
        report_ids = set().union(*report_ids_by_kind.values())
        if report_ids != {str(item.id) for item in self.evidence}:
            raise ValueError("the report evidence set must exactly match durable evidence")
        if sum(len(ids) for ids in report_ids_by_kind.values()) != len(report_ids):
            raise ValueError("report evidence categories must not overlap")
        evidence_ids_by_kind = {
            kind: {str(item.id) for item in self.evidence if item.evidence_kind == kind}
            for kind in ("supporting", "negative", "conflicting")
        }
        if report_ids_by_kind != evidence_ids_by_kind:
            raise ValueError("report evidence categories must match durable evidence kinds")
        if {claim.label for claim in self.report.claims} != {
            "fact",
            "inference",
            "estimate",
            "assumption",
            "hypothesis",
            "conflict",
            "missing_evidence",
        }:
            raise ValueError(
                "reports must distinguish facts, inferences, estimates, assumptions, "
                "hypotheses, conflicts, and missing evidence"
            )
        if any(set(claim.evidence_ids) - report_ids for claim in self.report.claims):
            raise ValueError("claim evidence IDs must reference durable report evidence")
        if any(claim.label == "fact" and not claim.evidence_ids for claim in self.report.claims):
            raise ValueError("facts require mechanically verified evidence IDs")
        if any(
            claim.label == "conflict" and not claim.evidence_ids for claim in self.report.claims
        ):
            raise ValueError("conflicts require mechanically verified evidence IDs")
        conflicting = tuple(item for item in self.evidence if item.evidence_kind == "conflicting")
        if conflicting and (
            not self.report.conflict_limitation or not self.report.conflict_limitation.strip()
        ):
            raise ValueError("conflicting evidence requires an explicit report limitation")
        if any(not item.material_conflict for item in conflicting):
            raise ValueError("conflicting evidence requires a material conflict description")
        source_origins = {urlsplit(item.url).netloc.casefold() for item in self.sources}
        if len(source_origins) < 3 and (
            not self.report.source_diversity_limitation
            or not self.report.source_diversity_limitation.strip()
        ):
            raise ValueError("concentrated sources require a source-diversity limitation")
        if len(self.pattern.embedding) != 1024 or len(self.opportunity_embedding) != 1024:
            raise ValueError("opportunity embeddings must contain exactly 1024 dimensions")
        relation_values = (
            self.relation_id,
            self.related_opportunity_id,
            self.relation_similarity,
        )
        legacy_relation = any(item is not None for item in relation_values)
        if legacy_relation and not all(item is not None for item in relation_values):
            raise ValueError("a legacy semantic relation must be complete")
        if legacy_relation and self.relations:
            raise ValueError("use either the legacy relation fields or relations, not both")
        if self.classification == "new" and (legacy_relation or self.relations):
            raise ValueError("a New opportunity cannot carry a semantic relation")
        if self.classification != "new" and not (legacy_relation or self.relations):
            raise ValueError("a semantic classification requires a complete relation")
        if self.relations:
            if len({item.opportunity_id for item in self.relations}) != len(self.relations):
                raise ValueError("semantic relation targets must be unique")
            strongest = max(self.relations, key=lambda item: item.similarity)
            if strongest.relation_kind != self.classification:
                raise ValueError("classification must match the strongest semantic relation")
        reject_forbidden_keys(self.report.model_dump(mode="json"))
        return self


class EvidenceDetail(DurableModel):
    id: UUID
    excerpt: str
    supported_claim: str
    evidence_kind: str
    material_conflict: str | None = None
    source_url: str
    source_title: str
    publisher: str | None
    retrieved_at: datetime


class ProblemSignalDetail(DurableModel):
    id: UUID
    affected_user: str
    recurring_workflow: str
    current_workaround: str
    business_consequence: str
    confidence: Decimal


class ProblemPatternDetail(DurableModel):
    id: UUID
    summary: str


class RelatedOpportunityDetail(DurableModel):
    opportunity_id: UUID
    opportunity_name: str
    primary_industry: str
    similarity: Decimal
    relation_kind: Literal["related", "possible_rediscovery"]
    revision: int = Field(ge=1)
    lifecycle_status: Literal["active", "merged"]


class OpportunityVersionSummary(DurableModel):
    id: UUID
    version_number: int
    verdict: str
    weighted_score: Decimal
    evidence_score: int
    created_at: datetime
    is_current: bool


class LifecycleEventDetail(DurableModel):
    id: UUID
    event_type: str
    event_data: dict[str, Any]
    version_id: UUID
    created_at: datetime


class OpportunityDetail(DurableModel):
    id: UUID
    version_id: UUID
    version_number: int
    primary_industry: str
    revision: int = Field(ge=1)
    lifecycle_status: Literal["active", "merged"] = "active"
    classification: Literal["new", "related", "possible_rediscovery", "rediscovered", "updated"] = (
        "new"
    )
    merge_target_id: UUID | None = None
    rediscovery_target_id: UUID | None = None
    report: OpportunityAnalysis
    scores: dict[str, Decimal]
    verdict: str
    evidence: tuple[EvidenceDetail, ...]
    provenance: dict[str, Any]
    favorite: bool = False
    note: str | None = None
    origin_pattern: ProblemPatternDetail | None = None
    origin_signals: tuple[ProblemSignalDetail, ...] = ()
    related: tuple[RelatedOpportunityDetail, ...] = Field(default=(), max_length=3)
    versions: tuple[OpportunityVersionSummary, ...] = ()
    lifecycle_events: tuple[LifecycleEventDetail, ...] = ()


class RunNonCountingOutcomes(DurableModel):
    automatic_exact_rediscovery: int = Field(default=0, ge=0)
    updated_version: int = Field(default=0, ge=0)
    unresolved_possible_rediscovery: int = Field(default=0, ge=0)
    invalid_candidate: int = Field(default=0, ge=0)
    incomplete_candidate: int = Field(default=0, ge=0)


class RunResourceCapacity(DurableModel):
    queries: int = Field(default=0, ge=0)
    pages: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class RunCapacity(DurableModel):
    available: RunResourceCapacity
    reserved: RunResourceCapacity
    consumed: RunResourceCapacity
    remaining: RunResourceCapacity


_RUN_PROGRESS_COUNTERS = {
    "target_count": "p7_target_count",
    "admitted_count": "p7_admitted_count",
    "evaluated_count": "p7_evaluated_count",
    "achieved_count": "p7_achieved_count",
    "automatic_exact_rediscovery": "p7_automatic_exact_rediscovery",
    "updated_version": "p7_updated_version",
    "unresolved_possible_rediscovery": "p7_unresolved_possible_rediscovery",
    "invalid_candidate": "p7_invalid_candidate",
    "incomplete_candidate": "p7_incomplete_candidate",
}
_SHORTFALL_PREFIX = "p7_shortfall_"
_LIMIT_PREFIX = "p7_limit_"
_LIMIT_STAGE_PREFIX = "p7_limit_stage_"
_SHORTFALL_DETAILS: dict[ShortfallCode, str] = {
    "bounded_pool_exhausted": (
        "The bounded candidate pool ended before the five-opportunity target was reached. "
        "Pensae Signal did not add filler."
    ),
    "insufficient_evidence": (
        "Available verified evidence could not support five complete opportunities. "
        "Pensae Signal did not weaken an evidence gate or add filler."
    ),
}


class RunDetail(DurableModel):
    id: UUID
    state: str
    opportunity_id: UUID | None
    committed_opportunity_ids: tuple[UUID, ...] = Field(default=(), max_length=8)
    effective_config: dict[str, Any]
    workflow_version: str
    schema_version: str
    created_at: datetime
    current_stage: str = "created"
    work_counters: dict[str, int] = Field(default_factory=dict)
    warning_codes: tuple[str, ...] = ()
    model_usage: tuple[ModelCallUsage, ...] = Field(default=(), max_length=256)
    target_count: int = Field(default=0, ge=0)
    admitted_count: int = Field(default=0, ge=0)
    evaluated_count: int = Field(default=0, ge=0)
    achieved_count: int = Field(default=0, ge=0)
    committed_count: int = Field(default=0, ge=0)
    non_counting_outcomes: RunNonCountingOutcomes = Field(default_factory=RunNonCountingOutcomes)
    capacity: RunCapacity | None = None
    limit_code: LimitCode | None = None
    limit_stage: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")
    shortfall_code: ShortfallCode | None = None
    shortfall_detail: str | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def expose_run_progress_contract(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        exposed = dict(value)
        counters = dict(exposed.get("work_counters") or {})
        progress = {
            name: counters.pop(storage_key)
            for name, storage_key in _RUN_PROGRESS_COUNTERS.items()
            if storage_key in counters
        }
        exposed["work_counters"] = counters
        for name in ("target_count", "admitted_count", "evaluated_count", "achieved_count"):
            if name in progress:
                exposed[name] = progress[name]
        outcome_names = (
            "automatic_exact_rediscovery",
            "updated_version",
            "unresolved_possible_rediscovery",
            "invalid_candidate",
            "incomplete_candidate",
        )
        if any(name in progress for name in outcome_names):
            exposed["non_counting_outcomes"] = {
                name: progress.get(name, 0) for name in outcome_names
            }
        if "target_count" not in progress and not exposed.get("target_count"):
            effective_config = exposed.get("effective_config")
            if isinstance(effective_config, Mapping):
                bounds = effective_config.get("bounds")
                if isinstance(bounds, Mapping) and isinstance(bounds.get("opportunities"), int):
                    exposed["target_count"] = bounds["opportunities"]

        public_warnings: list[str] = []
        for warning in exposed.get("warning_codes") or ():
            if warning.startswith(_SHORTFALL_PREFIX):
                exposed["shortfall_code"] = warning.removeprefix(_SHORTFALL_PREFIX)
            elif warning.startswith(_LIMIT_STAGE_PREFIX):
                exposed["limit_stage"] = warning.removeprefix(_LIMIT_STAGE_PREFIX)
            elif warning.startswith(_LIMIT_PREFIX):
                exposed["limit_code"] = warning.removeprefix(_LIMIT_PREFIX)
            else:
                public_warnings.append(warning)
        exposed["warning_codes"] = tuple(public_warnings)
        if exposed.get("limit_code") and not exposed.get("limit_stage"):
            exposed["limit_stage"] = exposed.get("current_stage")
        shortfall_code = exposed.get("shortfall_code")
        exposed["shortfall_detail"] = (
            _SHORTFALL_DETAILS[cast(ShortfallCode, shortfall_code)]
            if shortfall_code in _SHORTFALL_DETAILS
            else None
        )
        committed_ids = exposed.get("committed_opportunity_ids") or ()
        if committed_ids and not exposed.get("opportunity_id"):
            exposed["opportunity_id"] = committed_ids[-1]
        return exposed


def persisted_run_counters(
    counters: Mapping[str, int],
    *,
    target_count: int,
    admitted_count: int,
    evaluated_count: int,
    achieved_count: int,
    non_counting_outcomes: Mapping[str, int],
) -> dict[str, int]:
    """Encode scalar diagnostics in the existing bounded run JSONB column."""

    encoded = dict(counters)
    values = {
        "target_count": target_count,
        "admitted_count": admitted_count,
        "evaluated_count": evaluated_count,
        "achieved_count": achieved_count,
        **non_counting_outcomes,
    }
    if any(not isinstance(value, int) or value < 0 for value in values.values()):
        raise ValueError("run progress diagnostics must be nonnegative integers")
    encoded.update({_RUN_PROGRESS_COUNTERS[name]: value for name, value in values.items()})
    return encoded


def persisted_run_warnings(
    warning_codes: tuple[str, ...],
    *,
    shortfall_code: ShortfallCode | None,
    limit_code: LimitCode | None,
    limit_stage: str | None,
) -> tuple[str, ...]:
    """Encode closed terminal diagnostics without changing the accepted durable schema."""

    markers = (
        *((f"{_SHORTFALL_PREFIX}{shortfall_code}",) if shortfall_code else ()),
        *((f"{_LIMIT_PREFIX}{limit_code}",) if limit_code else ()),
        *((f"{_LIMIT_STAGE_PREFIX}{limit_stage}",) if limit_stage else ()),
    )
    return tuple(dict.fromkeys((*warning_codes, *markers)))


@dataclass(frozen=True, slots=True)
class SimilarityMatch:
    opportunity_id: UUID
    similarity: float


@dataclass(frozen=True, slots=True)
class PatternSimilarityMatch:
    pattern_id: UUID
    similarity: float


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    stopped_run_ids: tuple[UUID, ...]
    deleted_run_ids: tuple[UUID, ...]


def reject_forbidden_keys(value: Mapping[str, Any]) -> None:
    for key, nested in value.items():
        if key.casefold() in FORBIDDEN_DURABLE_KEYS:
            raise ValueError(f"durable payload contains prohibited field: {key}")
        if isinstance(nested, Mapping):
            reject_forbidden_keys(nested)
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, Mapping):
                    reject_forbidden_keys(item)
