from __future__ import annotations

from typing import Any

import pytest

from pensae.domain.evidence import (
    EvidenceIntegrityError,
    construct_verified_evidence,
    create_transient_spans,
)
from pensae.domain.opportunity import ProblemSignal
from pensae.research.schemas import SignalSelection


def test_manifest_has_reusable_synthetic_provenance_and_complete_case_names(
    corpus: dict[str, Any],
) -> None:
    provenance = corpus["provenance"]

    assert corpus["schema_version"] == "pensae.offline-evaluation.v1"
    assert corpus["license"] == "CC0-1.0"
    assert provenance == {
        "creator": "Pensae project contributors",
        "creation_method": (
            "Original synthetic scenarios written solely for deterministic evaluation"
        ),
        "created_on": "2026-07-23",
        "contains_live_payloads": False,
        "contains_personal_data": False,
        "contains_model_output": False,
        "network_required": False,
    }
    assert all(source["url"].split("/", 3)[2].endswith(".example") for source in corpus["sources"])
    case_groups = (
        "signal_cases",
        "evidence_cases",
        "source_diversity_cases",
        "negative_evidence_cases",
        "identity_cases",
        "schema_repair_cases",
        "prompt_injection_cases",
    )
    names = [case["name"] for group in case_groups for case in corpus[group]]
    assert len(names) == len(set(names))
    assert all(case.get("expected_outcome") for group in case_groups for case in corpus[group])


@pytest.mark.parametrize("case_index", [0, 1])
def test_expected_signals_have_complete_fields_and_mechanical_evidence(
    case_index: int,
    corpus: dict[str, Any],
    sources: dict[str, dict[str, Any]],
) -> None:
    case = corpus["signal_cases"][case_index]
    source = sources[case["source_id"]]
    document = create_transient_spans(
        source_id=source["id"],
        extracted_text=source["text"],
        span_characters=case["span_characters"],
    )
    selected = tuple(case["selected_span_ids"])
    evidence = construct_verified_evidence(
        document,
        selected_span_ids=selected,
        supported_claim=case["expected_signal"]["business_consequence"],
    )
    selection = SignalSelection(
        **case["expected_signal"],
        source_id=source["id"],
        span_ids=selected,
    )
    signal = ProblemSignal(
        source_id=selection.source_id,
        evidence_ids=(f"evidence-{case_index}",),
        affected_user=selection.affected_user,
        recurring_workflow=selection.recurring_workflow,
        current_workaround=selection.current_workaround,
        business_consequence=selection.business_consequence,
        confidence=selection.confidence,
    )

    assert case["expected_outcome"] == "retained"
    assert case["expected_evidence_kind"] == "supporting"
    assert evidence.excerpt == document.normalized_text[: case["span_characters"]]
    assert signal.model_dump()["confidence"] == case["expected_signal"]["confidence"]
    assert set(case["expected_signal"]) == {
        "affected_user",
        "recurring_workflow",
        "current_workaround",
        "business_consequence",
        "confidence",
    }


@pytest.mark.parametrize("case_index", [0, 1, 2, 3])
def test_evidence_cases_enforce_exact_same_source_contiguous_spans(
    case_index: int,
    corpus: dict[str, Any],
    sources: dict[str, dict[str, Any]],
) -> None:
    case = corpus["evidence_cases"][case_index]
    source = sources[case["source_id"]]
    document = create_transient_spans(
        source_id=source["id"],
        extracted_text=source["text"],
        span_characters=case["span_characters"],
    )

    if case["expected_outcome"] == "accepted":
        evidence = construct_verified_evidence(
            document,
            selected_span_ids=tuple(case["selected_span_ids"]),
            supported_claim=case["supported_claim"],
        )
        selected = [span for span in document.spans if span.span_id in case["selected_span_ids"]]
        assert evidence.excerpt == document.normalized_text[selected[0].start : selected[-1].end]
        assert len(evidence.excerpt) <= 500
        return

    with pytest.raises(EvidenceIntegrityError, match=case["expected_error"]):
        construct_verified_evidence(
            document,
            selected_span_ids=tuple(case["selected_span_ids"]),
            supported_claim=case["supported_claim"],
        )
