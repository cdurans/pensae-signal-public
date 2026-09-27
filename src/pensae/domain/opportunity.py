"""Deterministic opportunity gate, scoring, identity, and classification policy."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProblemSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=64)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    affected_user: str = Field(min_length=1, max_length=240)
    recurring_workflow: str = Field(min_length=1, max_length=500)
    current_workaround: str = Field(min_length=1, max_length=500)
    business_consequence: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class PreliminaryCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern_summary: str = Field(min_length=1, max_length=1_000)
    target_segment: str = Field(min_length=1, max_length=240)
    likely_buyer: str = Field(min_length=1, max_length=240)
    signals: tuple[ProblemSignal, ...] = Field(min_length=1)
    exceptional_primary_source: bool = False
    concentration_limitation: str | None = Field(default=None, max_length=500)
    exact_duplicate: bool = False
    prohibited_retrieval: bool = False
    evidence_integrity_failure: bool = False
    insufficient_distinction: bool = False


class GateFailure(StrEnum):
    INSUFFICIENT_INDEPENDENT_SIGNALS = "insufficient_independent_signals"
    MISSING_CONCENTRATION_LIMITATION = "missing_concentration_limitation"
    EXACT_DUPLICATE = "exact_duplicate"
    PROHIBITED_RETRIEVAL = "prohibited_retrieval"
    EVIDENCE_INTEGRITY_FAILURE = "evidence_integrity_failure"
    INSUFFICIENT_DISTINCTION = "insufficient_distinction"


class PreliminaryGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    failures: tuple[GateFailure, ...]


def evaluate_preliminary_gate(candidate: PreliminaryCandidate) -> PreliminaryGateResult:
    failures: list[GateFailure] = []
    independent_sources = {signal.source_id for signal in candidate.signals}
    if len(independent_sources) < 2:
        if not candidate.exceptional_primary_source:
            failures.append(GateFailure.INSUFFICIENT_INDEPENDENT_SIGNALS)
        elif (
            not candidate.concentration_limitation or not candidate.concentration_limitation.strip()
        ):
            failures.append(GateFailure.MISSING_CONCENTRATION_LIMITATION)
    if candidate.exact_duplicate:
        failures.append(GateFailure.EXACT_DUPLICATE)
    if candidate.prohibited_retrieval:
        failures.append(GateFailure.PROHIBITED_RETRIEVAL)
    if candidate.evidence_integrity_failure:
        failures.append(GateFailure.EVIDENCE_INTEGRITY_FAILURE)
    if candidate.insufficient_distinction:
        failures.append(GateFailure.INSUFFICIENT_DISTINCTION)
    return PreliminaryGateResult(passed=not failures, failures=tuple(failures))


class ScoreCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commercial_attractiveness: int = Field(ge=1, le=5)
    evidence_strength: int = Field(ge=1, le=5)
    pensae_feasibility: int = Field(ge=1, le=5)
    differentiation: int = Field(ge=1, le=5)

    def weighted_total(self) -> Decimal:
        weighted = (
            self.commercial_attractiveness * 35
            + self.evidence_strength * 30
            + self.pensae_feasibility * 25
            + self.differentiation * 10
        )
        return Decimal(weighted) / Decimal(100)


class Verdict(StrEnum):
    PROMISING = "promising"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"
    DO_NOT_PURSUE = "do_not_pursue"


class VerdictContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strong_evidence: bool
    plausible_buyer: bool
    payment_or_value_path: bool
    critical_blocker: bool = False
    strong_negative_evidence: bool = False
    implausible_economics: bool = False
    excessive_customization_or_operations: bool = False


def assign_verdict(scores: ScoreCard, context: VerdictContext) -> Verdict:
    total = scores.weighted_total()
    if (
        context.critical_blocker
        or context.strong_negative_evidence
        or context.implausible_economics
        or context.excessive_customization_or_operations
        or total < Decimal("2.5")
    ):
        return Verdict.DO_NOT_PURSUE
    if (
        total >= Decimal("3.5")
        and context.strong_evidence
        and context.plausible_buyer
        and context.payment_or_value_path
    ):
        return Verdict.PROMISING
    return Verdict.NEEDS_MORE_EVIDENCE


class OpportunityIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    problem_pattern: str = Field(min_length=1)
    proposed_solution: str = Field(min_length=1)
    target_segment: str = Field(min_length=1)
    likely_buyer: str = Field(min_length=1)
    recurring_workflow: str = Field(min_length=1)


class ProblemSimilarityInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    problem: str = Field(min_length=1)
    affected_user: str = Field(min_length=1)
    current_workaround: str = Field(min_length=1)
    desired_outcome: str = Field(min_length=1)


def _normalize_identity_value(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip().casefold()


def identity_fingerprint(identity: OpportunityIdentity, *, version: str) -> str:
    if not version.strip():
        raise ValueError("fingerprint version cannot be empty")
    payload = {
        "version": version,
        **{
            key: _normalize_identity_value(value)
            for key, value in identity.model_dump(mode="python").items()
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def problem_similarity_text(value: ProblemSimilarityInput) -> str:
    fields = value.model_dump(mode="python")
    return "\n".join(
        f"{key}: {re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip()}"
        for key, text in fields.items()
    )


class InitialClassification(StrEnum):
    NEW = "new"
    RELATED = "related"
    POSSIBLE_REDISCOVERY = "possible_rediscovery"
    REDISCOVERED = "rediscovered"


def classify_initial_candidate(
    *,
    exact_identity_match: bool,
    strongest_similarity: float | None,
    related_threshold: float,
    possible_rediscovery_threshold: float,
) -> InitialClassification:
    if not 0 <= related_threshold <= possible_rediscovery_threshold <= 1:
        raise ValueError("similarity thresholds must be ordered within zero and one")
    if strongest_similarity is not None and not 0 <= strongest_similarity <= 1:
        raise ValueError("strongest similarity must be within zero and one")
    if exact_identity_match:
        return InitialClassification.REDISCOVERED
    if strongest_similarity is None or strongest_similarity < related_threshold:
        return InitialClassification.NEW
    if strongest_similarity >= possible_rediscovery_threshold:
        return InitialClassification.POSSIBLE_REDISCOVERY
    return InitialClassification.RELATED
