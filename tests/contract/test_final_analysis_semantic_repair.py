from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from tests.fakes.models import FakeChatClient
from tests.fakes.opportunities import fake_opportunity_aggregate

from pensae.config.protected import ProtectedConfig
from pensae.domain.opportunity import PreliminaryCandidate
from pensae.infrastructure.models import ChatResponse, EmbeddingResponse
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchHit
from pensae.research.assembly import OpportunityAssembler
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.roles import (
    RoleExecutionResult,
    StructuredRoleExecutor,
    build_role_specs,
)
from pensae.research.schemas import (
    OpportunityAnalysis,
    PatternSynthesis,
    SegmentAssessment,
    SegmentSolution,
    SignalSelection,
)
from pensae.research.state import (
    ConceptCandidate,
    FocusedSource,
    PatternCandidate,
    SelectedSource,
)


@dataclass(slots=True)
class _ExecutorCalls:
    executor: StructuredRoleExecutor
    events: list[tuple[str, int]]
    result: RoleExecutionResult[OpportunityAnalysis] | None = None

    async def _model(
        self,
        spec: Any,
        *,
        trusted_input: Any,
        untrusted: Any,
        seed: int,
        timeout_seconds: int,
    ) -> RoleExecutionResult[OpportunityAnalysis]:
        del timeout_seconds
        result = await self.executor.execute(
            spec,
            trusted_input=trusted_input,
            untrusted_retrieved_data=untrusted,
            seed=seed,
        )
        self.result = result
        self.events.append(("model", result.attempts))
        return result

    async def _embed(self, texts: Any, bounds: Any, *, role: str) -> EmbeddingResponse:
        del texts, bounds
        assert self.result is not None and self.result.value is not None
        self.events.append((role, self.result.attempts))
        return EmbeddingResponse(
            vectors=((1.0,) + (0.0,) * 1_023,),
            input_tokens=7,
        )


def _selected_source(
    aggregate: Any,
    source_index: int,
    evidence_indexes: tuple[int, ...],
) -> SelectedSource:
    source = aggregate.sources[source_index]
    url = source.url
    return SelectedSource(
        id=source.id,
        hit=SearchHit(
            canonical_url=url,
            title=source.title,
            snippet="Synthetic focused-repair fixture.",
            engines=("fixture",),
        ),
        retrieved=RetrievedSource(
            final_url=url,
            media_type="text/html",
            extracted_text=f"Synthetic source {source_index} text.",
            response_bytes=100,
        ),
        selection=SignalSelection(
            affected_user="Property operations staff",
            recurring_workflow="Triage and assign maintenance requests",
            current_workaround="Email and spreadsheets",
            business_consequence="Assignments and follow-up are delayed",
            source_id=f"discovery_{source_index + 1}",
            span_ids=(f"discovery_{source_index + 1}:s0001",),
            confidence=0.8,
        ),
        evidence=tuple(aggregate.evidence[index] for index in evidence_indexes),
    )


def _candidate_fixture() -> tuple[ConceptCandidate, FocusedSource, OpportunityAnalysis]:
    aggregate = fake_opportunity_aggregate(UUID(int=700))
    selected = (
        _selected_source(aggregate, 0, (0, 1)),
        _selected_source(aggregate, 1, (2,)),
    )
    focused_selected = _selected_source(aggregate, 2, (3,))
    focused = FocusedSource(
        id=focused_selected.id,
        hit=focused_selected.hit,
        retrieved=focused_selected.retrieved,
        evidence=focused_selected.evidence,
        untrusted_spans=(),
    )
    synthesis = PatternSynthesis(
        pattern_summary=aggregate.report.problem_pattern,
        affected_user=aggregate.report.affected_user,
        recurring_workflow=aggregate.report.recurring_workflow,
        current_workaround=aggregate.report.current_workaround,
        desired_outcome="Faster auditable maintenance triage",
    )
    segment = SegmentAssessment(
        target_segment=aggregate.report.target_segment,
        affected_user=aggregate.report.affected_user,
        likely_buyer=aggregate.report.likely_buyer,
        recurring_workflow=aggregate.report.recurring_workflow,
        frequency_value_hypothesis=aggregate.report.frequency_value_hypothesis,
        constraints=("Limited integration capacity",),
        reachability="Reachable through property-management associations.",
    )
    candidate = PreliminaryCandidate(
        pattern_summary=aggregate.report.problem_pattern,
        target_segment=aggregate.report.target_segment,
        likely_buyer=aggregate.report.likely_buyer,
        signals=tuple(OpportunityAssembler._problem_signal(source) for source in selected),
    )
    pattern = PatternCandidate(
        fingerprint=aggregate.pattern.fingerprint,
        candidate=candidate,
        selected=selected,
        synthesis=synthesis,
        segment=segment,
        embedding=aggregate.pattern.embedding,
    )
    strategy = SegmentSolution(
        target_segment=aggregate.report.target_segment,
        likely_buyer=aggregate.report.likely_buyer,
        desired_outcome=synthesis.desired_outcome,
        proposed_solution=aggregate.report.proposed_solution,
        delivery_model=aggregate.report.delivery_model,
    )
    return ConceptCandidate(pattern=pattern, strategy=strategy), focused, aggregate.report


def _assembler(
    responses: tuple[ChatResponse, ...],
) -> tuple[OpportunityAssembler, _ExecutorCalls, FakeChatClient]:
    protected = ProtectedConfig.load()
    chat = FakeChatClient(responses, token_counts=(100, 200))
    executor = StructuredRoleExecutor(
        chat=chat,
        prompt_input_max_tokens=protected.research.prompt_input_max_tokens,
        context_safety_tokens=protected.research.context_safety_tokens,
    )
    calls = _ExecutorCalls(executor=executor, events=[])
    identifiers = count(900)
    context = SimpleNamespace(
        run_id=UUID(int=701),
        specs=build_role_specs(protected.research),
        protected=protected,
        id_factory=lambda: UUID(int=next(identifiers)),
        now=lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )
    return (
        OpportunityAssembler(cast(ResearchRunContext, context), cast(BoundedExternalCalls, calls)),
        calls,
        chat,
    )


@pytest.mark.anyio
async def test_semantic_invalid_final_report_repairs_before_similarity_embedding() -> None:
    concept, focused, valid = _candidate_fixture()
    invalid_claims = tuple(
        claim.model_copy(update={"evidence_ids": ()}) if claim.label == "fact" else claim
        for claim in valid.claims
    )
    invalid = valid.model_copy(
        update={"source_diversity_limitation": None, "claims": invalid_claims}
    )
    assembler, calls, chat = _assembler(
        (
            ChatResponse(
                content=invalid.model_dump_json(), prompt_tokens=100, completion_tokens=50
            ),
            ChatResponse(content=valid.model_dump_json(), prompt_tokens=200, completion_tokens=60),
        )
    )

    aggregate, result, embedding_tokens = await assembler._build_aggregate(
        concept,
        focused,
        seed=600,
        bounds=ProtectedConfig.load().research.bounds,
    )

    assert aggregate is not None and aggregate.report == valid
    assert result.attempts == 2
    assert [call.repair for call in result.calls] == [False, True]
    assert embedding_tokens == 7
    assert calls.events == [("model", 2), ("similarity_embedding", 2)]
    repair_control = chat.requests[1].messages[-1].content
    assert "final_source_diversity_limitation_missing" in repair_control
    assert "final_fact_evidence_missing" in repair_control
    assert "provide a non-empty source_diversity_limitation" in repair_control
    assert valid.opportunity_name not in repair_control


@pytest.mark.anyio
async def test_exhausted_semantic_repair_discards_without_embedding_or_raw_retention() -> None:
    concept, focused, valid = _candidate_fixture()
    invalid = valid.model_copy(update={"source_diversity_limitation": None})
    raw = invalid.model_dump_json()
    assembler, calls, chat = _assembler(
        (
            ChatResponse(content=raw, prompt_tokens=100, completion_tokens=50),
            ChatResponse(content=raw, prompt_tokens=200, completion_tokens=60),
        )
    )

    aggregate, result, embedding_tokens = await assembler._build_aggregate(
        concept,
        focused,
        seed=600,
        bounds=ProtectedConfig.load().research.bounds,
    )

    assert aggregate is None
    assert result.value is None
    assert result.attempts == 2
    assert [call.repair for call in result.calls] == [False, True]
    assert embedding_tokens == 0
    assert calls.events == [("model", 2)]
    assert valid.opportunity_name not in repr(result)
    assert "final_source_diversity_limitation_missing" in chat.requests[1].messages[-1].content
