from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from pensae.domain.opportunity import (
    GateFailure,
    InitialClassification,
    OpportunityIdentity,
    PreliminaryCandidate,
    ProblemSignal,
    ProblemSimilarityInput,
    ScoreCard,
    Verdict,
    VerdictContext,
    assign_verdict,
    classify_initial_candidate,
    evaluate_preliminary_gate,
    identity_fingerprint,
    problem_similarity_text,
)


def _signal(source_id: str) -> ProblemSignal:
    return ProblemSignal(
        source_id=source_id,
        evidence_ids=(f"evidence-{source_id}",),
        affected_user="property coordinator",
        recurring_workflow="triage incoming maintenance requests",
        current_workaround="copy messages into a spreadsheet",
        business_consequence="repairs and tenant follow-up are delayed",
        confidence=0.9,
    )


def _candidate(**updates: object) -> PreliminaryCandidate:
    values: dict[str, object] = {
        "pattern_summary": "Maintenance requests are fragmented across channels.",
        "target_segment": "small and midsize property-management firms",
        "likely_buyer": "property operations manager",
        "signals": (_signal("source-a"), _signal("source-b")),
    }
    values.update(updates)
    return PreliminaryCandidate.model_validate(values)


def test_preliminary_gate_accepts_two_independent_supported_signals() -> None:
    result = evaluate_preliminary_gate(_candidate())

    assert result.passed
    assert result.failures == ()


def test_preliminary_gate_allows_one_exceptional_source_only_with_limitation() -> None:
    missing = evaluate_preliminary_gate(
        _candidate(signals=(_signal("source-a"),), exceptional_primary_source=True)
    )
    accepted = evaluate_preliminary_gate(
        _candidate(
            signals=(_signal("source-a"),),
            exceptional_primary_source=True,
            concentration_limitation="Only one exceptional primary source was available.",
        )
    )

    assert missing.failures == (GateFailure.MISSING_CONCENTRATION_LIMITATION,)
    assert accepted.passed


def test_preliminary_gate_accumulates_policy_failures() -> None:
    result = evaluate_preliminary_gate(
        _candidate(
            signals=(_signal("source-a"),),
            exact_duplicate=True,
            prohibited_retrieval=True,
            evidence_integrity_failure=True,
        )
    )

    assert result.failures == (
        GateFailure.INSUFFICIENT_INDEPENDENT_SIGNALS,
        GateFailure.EXACT_DUPLICATE,
        GateFailure.PROHIBITED_RETRIEVAL,
        GateFailure.EVIDENCE_INTEGRITY_FAILURE,
    )


def test_score_weights_are_fixed_application_arithmetic() -> None:
    scores = ScoreCard(
        commercial_attractiveness=4,
        evidence_strength=3,
        pensae_feasibility=5,
        differentiation=2,
    )

    assert scores.weighted_total() == Decimal("3.75")
    with pytest.raises(ValidationError):
        ScoreCard(
            commercial_attractiveness=6,
            evidence_strength=3,
            pensae_feasibility=5,
            differentiation=2,
        )


@pytest.mark.parametrize(
    ("scores", "context", "expected"),
    [
        (
            ScoreCard(
                commercial_attractiveness=4,
                evidence_strength=4,
                pensae_feasibility=4,
                differentiation=4,
            ),
            VerdictContext(
                strong_evidence=True,
                plausible_buyer=True,
                payment_or_value_path=True,
            ),
            Verdict.PROMISING,
        ),
        (
            ScoreCard(
                commercial_attractiveness=5,
                evidence_strength=5,
                pensae_feasibility=5,
                differentiation=5,
            ),
            VerdictContext(
                strong_evidence=True,
                plausible_buyer=True,
                payment_or_value_path=True,
                critical_blocker=True,
            ),
            Verdict.DO_NOT_PURSUE,
        ),
        (
            ScoreCard(
                commercial_attractiveness=3,
                evidence_strength=3,
                pensae_feasibility=3,
                differentiation=3,
            ),
            VerdictContext(
                strong_evidence=False,
                plausible_buyer=True,
                payment_or_value_path=False,
            ),
            Verdict.NEEDS_MORE_EVIDENCE,
        ),
    ],
)
def test_verdict_policy_cannot_be_overridden_by_score(
    scores: ScoreCard, context: VerdictContext, expected: Verdict
) -> None:
    assert assign_verdict(scores, context) is expected


def test_identity_fingerprint_is_normalized_versioned_and_solution_sensitive() -> None:
    first = OpportunityIdentity(
        problem_pattern=" Fragmented  maintenance requests ",
        proposed_solution="Unified intake",
        target_segment="Property Managers",
        likely_buyer="Operations Manager",
        recurring_workflow="Maintenance triage",
    )
    equivalent = first.model_copy(update={"problem_pattern": "fragmented maintenance requests"})
    different_solution = first.model_copy(update={"proposed_solution": "Managed service"})

    assert identity_fingerprint(first, version="v1") == identity_fingerprint(
        equivalent, version="v1"
    )
    assert identity_fingerprint(first, version="v1") != identity_fingerprint(
        different_solution, version="v1"
    )
    assert identity_fingerprint(first, version="v1") != identity_fingerprint(first, version="v2")


def test_problem_similarity_text_excludes_solution_scores_and_evidence_wording() -> None:
    value = ProblemSimilarityInput(
        problem="Requests are fragmented",
        affected_user="Property coordinator",
        current_workaround="Spreadsheet",
        desired_outcome="Reliable triage",
    )

    text = problem_similarity_text(value)

    assert text == (
        "problem: Requests are fragmented\n"
        "affected_user: Property coordinator\n"
        "current_workaround: Spreadsheet\n"
        "desired_outcome: Reliable triage"
    )
    assert "solution" not in text
    assert "score" not in text
    assert "evidence" not in text


@pytest.mark.parametrize(
    ("exact", "similarity", "expected"),
    [
        (True, None, InitialClassification.REDISCOVERED),
        (False, None, InitialClassification.NEW),
        (False, 0.69, InitialClassification.NEW),
        (False, 0.70, InitialClassification.RELATED),
        (False, 0.89, InitialClassification.RELATED),
        (False, 0.90, InitialClassification.POSSIBLE_REDISCOVERY),
    ],
)
def test_initial_classification_uses_injected_calibrated_thresholds(
    exact: bool, similarity: float | None, expected: InitialClassification
) -> None:
    assert (
        classify_initial_candidate(
            exact_identity_match=exact,
            strongest_similarity=similarity,
            related_threshold=0.70,
            possible_rediscovery_threshold=0.90,
        )
        is expected
    )


def test_similarity_thresholds_are_validated_without_shipping_guessed_defaults() -> None:
    with pytest.raises(ValueError, match="ordered"):
        classify_initial_candidate(
            exact_identity_match=False,
            strongest_similarity=0.8,
            related_threshold=0.9,
            possible_rediscovery_threshold=0.7,
        )
