from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from tests.fakes.opportunities import fake_opportunity_aggregate

from pensae.config.protected import ProtectedConfig
from pensae.domain.opportunity import (
    OpportunityIdentity,
    PreliminaryCandidate,
    ProblemSignal,
    ScoreCard,
    VerdictContext,
    assign_verdict,
    classify_initial_candidate,
    evaluate_preliminary_gate,
    identity_fingerprint,
)


def _signal(source_id: str) -> ProblemSignal:
    return ProblemSignal(
        source_id=source_id,
        evidence_ids=(f"evidence-{source_id}",),
        affected_user="synthetic operations coordinator",
        recurring_workflow="triage and assign incoming requests",
        current_workaround="copy status between messages and a spreadsheet",
        business_consequence="assignment can be delayed",
        confidence=0.8,
    )


@pytest.mark.parametrize("case_index", [0, 1, 2])
def test_preliminary_source_diversity_cases_match_policy(
    case_index: int, corpus: dict[str, Any]
) -> None:
    case = corpus["source_diversity_cases"][case_index]
    candidate = PreliminaryCandidate(
        pattern_summary="Fragmented maintenance intake delays assignment.",
        target_segment="Small property managers",
        likely_buyer="Operations director",
        signals=tuple(_signal(source_id) for source_id in case["signal_source_ids"]),
        exceptional_primary_source=case["exceptional_primary_source"],
        concentration_limitation=case["concentration_limitation"],
    )

    result = evaluate_preliminary_gate(candidate)

    assert result.passed is (case["expected_outcome"] == "accepted")
    if not result.passed:
        assert case["expected_failure"] in {failure.value for failure in result.failures}


@pytest.mark.parametrize("case_index", [3, 4])
def test_durable_source_concentration_requires_an_explicit_limitation(
    case_index: int,
    corpus: dict[str, Any],
    sources: dict[str, dict[str, Any]],
) -> None:
    case = corpus["source_diversity_cases"][case_index]
    aggregate = fake_opportunity_aggregate(UUID(int=5_002))
    raw = aggregate.model_dump(mode="python")
    selected = [sources[source_id] for source_id in case["durable_source_ids"]]
    for index, source in enumerate(raw["sources"]):
        selected_source = selected[index % len(selected)]
        source["url"] = selected_source["url"]
    raw["report"]["source_diversity_limitation"] = case["source_diversity_limitation"]

    if case["expected_outcome"] == "accepted":
        validated = type(aggregate).model_validate(raw)
        assert len({source.url.split("/", 3)[2] for source in validated.sources}) == 3
        return

    with pytest.raises(ValidationError, match=case["expected_error"]):
        type(aggregate).model_validate(raw)


@pytest.mark.parametrize("case_index", [0, 1])
def test_negative_evidence_is_present_when_required_and_controls_verdict(
    case_index: int, corpus: dict[str, Any]
) -> None:
    case = corpus["negative_evidence_cases"][case_index]
    negative_sources = set(case["negative_source_ids"])

    if case.get("requires_negative_evidence") and not negative_sources:
        assert case["expected_outcome"] == "rejected"
        assert case["expected_error"] == "required negative evidence is missing"
        return

    assert negative_sources <= set(case["evidence_source_ids"])
    scores = ScoreCard(
        commercial_attractiveness=case["scores"][0],
        evidence_strength=case["scores"][1],
        pensae_feasibility=case["scores"][2],
        differentiation=case["scores"][3],
    )
    verdict = assign_verdict(scores, VerdictContext.model_validate(case["verdict_context"]))
    assert verdict.value == case["expected_outcome"]


@pytest.mark.parametrize("case_index", [0, 1, 2, 3, 4, 5])
def test_duplicate_related_and_distinct_labels_match_calibrated_boundaries(
    case_index: int, corpus: dict[str, Any]
) -> None:
    case = corpus["identity_cases"][case_index]
    policy = ProtectedConfig.load().research
    if case["exact_identity_match"]:
        base = OpportunityIdentity.model_validate(case["base_identity"])
        candidate = OpportunityIdentity.model_validate(case["candidate_identity"])
        base_fingerprint = identity_fingerprint(base, version=policy.fingerprint_version)
        candidate_fingerprint = identity_fingerprint(candidate, version=policy.fingerprint_version)
        assert base_fingerprint == candidate_fingerprint

    result = classify_initial_candidate(
        exact_identity_match=case["exact_identity_match"],
        strongest_similarity=case["similarity"],
        related_threshold=policy.related_similarity_threshold,
        possible_rediscovery_threshold=policy.possible_rediscovery_similarity_threshold,
    )

    assert result.value == case["expected_outcome"]
