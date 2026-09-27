"""Reusable opportunity and run fixtures."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pensae.opportunities import (
    EvidenceInput,
    OpportunityAggregate,
    PatternInput,
    ProvenanceInput,
    RunSnapshot,
    SignalInput,
    SourceInput,
)
from pensae.research.schemas import ClaimAssessment, OpportunityAnalysis, ProposedScores


def fake_run_snapshot(run_id: UUID | None = None) -> RunSnapshot:
    return RunSnapshot(
        id=run_id or uuid4(),
        effective_config={
            "focus": {"industries": ["property management"]},
            "bounds": {"max_sources": 3},
            "versions": {"threshold": "similarity.v1"},
        },
        workflow_version="phase1.vertical-slice.v1",
        schema_version="phase1.opportunity.v1",
    )


def fake_opportunity_aggregate(run_id: UUID) -> OpportunityAggregate:
    source_ids = tuple(uuid4() for _ in range(3))
    evidence_ids = tuple(uuid4() for _ in range(4))
    signal_ids = tuple(uuid4() for _ in range(2))
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    sources = tuple(
        SourceInput(
            id=source_id,
            url=f"https://example.test/property-maintenance-{index}",
            title=f"Synthetic property maintenance source {index}",
            publisher=f"Synthetic publisher {index}",
            retrieved_at=now,
            credibility_note="Legally reusable deterministic offline fixture.",
            limitation="Synthetic evidence is used only for automated verification.",
            content_fingerprint=hashlib.sha256(f"source-{index}".encode()).hexdigest(),
        )
        for index, source_id in enumerate(source_ids, start=1)
    )
    evidence = (
        EvidenceInput(
            id=evidence_ids[0],
            source_id=source_ids[0],
            excerpt="Maintenance requests arrive through email and require manual triage.",
            supported_claim="Requests arrive through fragmented channels.",
            evidence_kind="supporting",
        ),
        EvidenceInput(
            id=evidence_ids[1],
            source_id=source_ids[0],
            excerpt="Staff copy request status into a shared spreadsheet for follow-up.",
            supported_claim="Staff use a spreadsheet workaround.",
            evidence_kind="supporting",
        ),
        EvidenceInput(
            id=evidence_ids[2],
            source_id=source_ids[1],
            excerpt="Text messages from tenants can remain unassigned until the next business day.",
            supported_claim="Fragmentation delays assignment.",
            evidence_kind="supporting",
        ),
        EvidenceInput(
            id=evidence_ids[3],
            source_id=source_ids[2],
            excerpt="A small team reported that existing suites can require costly implementation.",
            supported_claim="Existing alternatives may be too operationally heavy.",
            evidence_kind="negative",
        ),
    )
    signals = (
        SignalInput(
            id=signal_ids[0],
            evidence_ids=evidence_ids[:2],
            affected_user="Property manager",
            recurring_workflow="Receive, triage, assign, and follow up on maintenance requests",
            current_workaround="Email inbox plus shared spreadsheet",
            business_consequence="Delayed assignments and inconsistent tenant updates",
            confidence=Decimal("0.86"),
        ),
        SignalInput(
            id=signal_ids[1],
            evidence_ids=evidence_ids[2:],
            affected_user="Maintenance coordinator",
            recurring_workflow="Reconcile texted requests with open work orders",
            current_workaround="Manual message review and status copying",
            business_consequence="Requests are missed and staff time is spent reconciling records",
            confidence=Decimal("0.78"),
        ),
    )
    report = OpportunityAnalysis(
        opportunity_name="Maintenance Request Triage Assistant",
        concise_summary="A focused intake and follow-up layer for small property managers.",
        primary_industry="Property management",
        problem_pattern="Fragmented request intake causes delayed triage and follow-up.",
        target_segment="Small and midsize residential property-management firms",
        affected_user="Property managers and maintenance coordinators",
        likely_buyer="Head of property operations",
        recurring_workflow="Receive, triage, assign, and follow up on maintenance requests",
        current_workaround="Email, text messages, and shared spreadsheets",
        business_consequences=("Delayed repairs", "Inconsistent tenant communication"),
        proposed_solution="A narrow intake and triage assistant with an auditable follow-up queue.",
        delivery_model="Locally operated web application",
        initial_market_reason=(
            "Recurring coordination burden is visible across independent sources."
        ),
        frequency_value_hypothesis=(
            "The workflow recurs daily and delay consumes staff time while harming tenant trust."
        ),
        other_segments=("Small facilities-management teams",),
        alternatives=("Property-management suites", "Shared inbox and spreadsheet"),
        missing_capabilities=("Direct work-order-system integration",),
        reusable_core_capabilities=("Channel intake", "Triage queue", "Follow-up audit trail"),
        pricing_or_spend_signals=(
            "Teams already pay for property software and spend coordinator time on reconciliation.",
        ),
        market_saturation="Broad suites are common, but the narrow coordination gap persists.",
        incumbent_response_risk="Property suites could add a similar focused workflow.",
        integration_customization_burden=(
            "The first version avoids deep integrations; optional connectors remain a burden risk."
        ),
        preferred_technology_fit=(
            "A local TypeScript/FastAPI application fits the auditable workflow and operator model."
        ),
        trust_regulatory_constraints=(
            "Tenant messages may contain personal information and require careful handling.",
        ),
        operational_burden=(
            "Source review and integration support must remain bounded for a small team."
        ),
        commercial_analysis=(
            "The buyer can connect faster triage to staff time and tenant retention."
        ),
        feasibility_analysis="The first product can remain narrow and integration-light.",
        differentiation_analysis=(
            "Evidence-linked recommendations distinguish it from generic inboxes."
        ),
        risks=("Incumbent suites could add similar intake automation.",),
        unknowns=("Willingness to pay requires direct validation.",),
        reasons_not_to_pursue=(),
        supporting_evidence_ids=tuple(str(item) for item in evidence_ids[:3]),
        negative_evidence_ids=(str(evidence_ids[3]),),
        conflicting_evidence_ids=(),
        next_research_questions=("Which request volume creates budget urgency?",),
        claims=(
            ClaimAssessment(
                statement="Fragmented channels delay maintenance triage.",
                label="fact",
                evidence_ids=(str(evidence_ids[0]),),
            ),
            ClaimAssessment(
                statement="A focused intake layer may reduce coordination delay.",
                label="inference",
            ),
            ClaimAssessment(
                statement="A first deployment could save several staff hours each week.",
                label="estimate",
            ),
            ClaimAssessment(statement="Buyers may pay for saved time.", label="assumption"),
            ClaimAssessment(
                statement="A narrow queue is the smallest useful product.", label="hypothesis"
            ),
            ClaimAssessment(
                statement="Existing suites may reduce the need for a separate queue.",
                label="conflict",
                evidence_ids=(str(evidence_ids[3]),),
            ),
            ClaimAssessment(
                statement="Validated willingness to pay is missing.", label="missing_evidence"
            ),
        ),
        source_diversity_limitation="The vertical slice uses a small source set.",
        conflict_limitation=None,
        proposed_scores=ProposedScores(
            commercial_attractiveness=4,
            commercial_explanation="A plausible operations buyer and measurable consequence exist.",
            evidence_strength=4,
            evidence_explanation="Multiple independent sources support the core workflow problem.",
            pensae_feasibility=4,
            feasibility_explanation="A narrow initial workflow avoids deep integration.",
            differentiation=3,
            differentiation_explanation="Positioning is distinct but incumbents remain a risk.",
        ),
        strong_evidence=True,
        plausible_buyer=True,
        payment_or_value_path=True,
        critical_blocker=False,
        strong_negative_evidence=False,
        implausible_economics=False,
        excessive_customization_or_operations=False,
    )
    return OpportunityAggregate(
        opportunity_id=uuid4(),
        version_id=uuid4(),
        lifecycle_event_id=uuid4(),
        run_id=run_id,
        identity_fingerprint=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        primary_industry="Property management",
        report=report,
        sources=sources,
        evidence=evidence,
        signals=signals,
        pattern=PatternInput(
            id=uuid4(),
            signal_ids=signal_ids,
            summary="Fragmented maintenance intake delays triage and tenant follow-up.",
            fingerprint=hashlib.sha256(b"fragmented-maintenance-intake").hexdigest(),
            embedding=(1.0,) + (0.0,) * 1023,
            embedding_model_id="Qwen3-Embedding-0.6B",
            fingerprint_version="sha256-canonical-json-v1",
        ),
        opportunity_embedding=(1.0,) + (0.0,) * 1023,
        provenance=ProvenanceInput(
            chat_model_id="Qwen3.6-35B-A3B-UD-IQ4_XS",
            embedding_model_id="Qwen3-Embedding-0.6B",
            workflow_version="phase1.vertical-slice.v1",
            prompt_versions={
                "planner": "research-planner.v1",
                "problem_analyst": "problem-analyst.v1",
                "product_strategist": "product-strategist.v1",
                "opportunity_analyst": "opportunity-analyst.v1",
            },
            schema_version="phase1.opportunity.v1",
            fingerprint_version="sha256-canonical-json-v1",
            threshold_version="similarity.v1",
            model_parameter_version="llama-chat-nonthinking-v1",
        ),
    )
