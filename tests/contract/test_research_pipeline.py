"""Current production research workflow contract tests."""

from __future__ import annotations

import ast
import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel
from tests.fakes.models import FakeChatClient
from tests.fakes.opportunities import fake_opportunity_aggregate

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.infrastructure.models import ChatResponse, EmbeddingResponse
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchBatch, SearchContractError, SearchHit
from pensae.opportunities import EvidenceInput
from pensae.opportunities.writer import score_opportunity_report
from pensae.research.budget import BudgetLimitExceeded, WorkDimension
from pensae.research.operations import ResearchWorkflowOperations, _strongest_pattern_similarity
from pensae.research.roles import (
    FOCUSED_SELECTOR_HEADROOM_TOKENS,
    RoleExecutionResult,
    RoleName,
    RoleSpec,
    StructuredRoleExecutor,
    pack_untrusted_spans,
)
from pensae.research.schemas import (
    FocusedEvidenceSelection,
    PatternSynthesis,
    QueryPlan,
    SegmentAssessment,
    SegmentMap,
    SegmentSolution,
    SignalSelection,
)
from pensae.research.state import SelectedSource
from pensae.research.workflow import (
    WORKFLOW_SEQUENCE,
    FixedWorkflowGraph,
    NodeResult,
    WorkflowDependencyUnavailable,
    WorkflowNode,
    WorkflowStopRequested,
    WorkLimitExceeded,
)


class ToggleCancellation:
    def __init__(self) -> None:
        self.value = False

    async def requested(self, run_id: UUID) -> bool:
        del run_id
        return self.value


def _hit(name: str) -> SearchHit:
    return SearchHit(
        canonical_url=f"https://{name}.example.test/source",
        title=f"{name} source",
        snippet="bounded fixture",
        engines=("fixture",),
    )


def _selected_signal(index: int) -> SelectedSource:
    durable_source_id = UUID(int=10_000 + index)
    signal_source_id = f"signal_{index}"
    return SelectedSource(
        id=durable_source_id,
        hit=_hit(signal_source_id),
        retrieved=RetrievedSource(
            final_url=f"https://{signal_source_id}.example.test/source",
            media_type="text/plain",
            extracted_text=f"Independent evidence for related workflow group {index}",
            response_bytes=140,
        ),
        selection=SignalSelection(
            affected_user="Operations staff",
            recurring_workflow=f"group-{index // 2} recurring workflow",
            current_workaround="Manual coordination",
            business_consequence="Delayed follow-up",
            source_id=signal_source_id,
            span_ids=(f"{signal_source_id}:s0001",),
            confidence=0.8,
        ),
        evidence=(
            EvidenceInput(
                id=UUID(int=20_000 + index),
                source_id=durable_source_id,
                excerpt="Independent evidence",
                supported_claim="Related recurring workflow",
                evidence_kind="supporting",
            ),
        ),
    )


class FakeSearch:
    def __init__(
        self,
        cancellation: ToggleCancellation,
        *,
        cancel: bool = False,
        retries: int = 0,
        one_source: bool = False,
        focused_sources: int = 1,
    ) -> None:
        self.cancellation = cancellation
        self.cancel = cancel
        self.calls: list[str] = []
        self.retries = retries
        self.one_source = one_source
        self.focused_sources = focused_sources

    async def search(self, query: str, *, result_limit: int = 10) -> SearchBatch:
        self.calls.append(query)
        if self.cancel:
            self.cancellation.value = True
        results = (
            tuple(_hit(f"focused-{index}") for index in range(self.focused_sources))
            if "focused" in query
            else (_hit("one"), _hit("two"))
        )
        if self.one_source and "focused" not in query:
            results = results[:1]
        return SearchBatch(
            query=query,
            results=results[:result_limit],
            warnings=(),
            retries=self.retries,
        )


class UniqueSearch:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(self, query: str, *, result_limit: int = 10) -> SearchBatch:
        call_index = len(self.calls)
        self.calls.append(query)
        return SearchBatch(
            query=query,
            results=tuple(
                _hit(f"unique-{call_index}-{result_index}") for result_index in range(result_limit)
            ),
            warnings=(),
            retries=0,
        )


class FailedRetrySearch:
    async def search(self, query: str, *, result_limit: int = 10) -> SearchBatch:
        del query, result_limit
        raise SearchContractError("local SearXNG search failed after one retry", retries=1)


class NeverReturningSearch:
    async def search(self, query: str, *, result_limit: int = 10) -> SearchBatch:
        del query, result_limit
        await asyncio.Event().wait()
        raise AssertionError("bounded search timeout did not fire")


class FakeRetrieval:
    def __init__(
        self,
        cancellation: ToggleCancellation,
        *,
        cancel: bool = False,
        response_bytes: int = 140,
        text_characters: int | None = None,
    ) -> None:
        self.cancellation = cancellation
        self.cancel = cancel
        self.response_bytes = response_bytes
        self.text_characters = text_characters

    async def fetch(self, url: str) -> RetrievedSource:
        if self.cancel:
            self.cancellation.value = True
        return RetrievedSource(
            final_url=url,
            media_type="text/plain",
            extracted_text=(
                "x" * self.text_characters
                if self.text_characters is not None
                else (
                    "Operators repeatedly copy incoming requests into spreadsheets, delaying "
                    "assignment and follow-up while existing software remains costly."
                )
            ),
            response_bytes=self.response_bytes,
        )


class NeverReturningRetrieval:
    async def fetch(self, url: str) -> RetrievedSource:
        del url
        await asyncio.Event().wait()
        raise AssertionError("bounded retrieval timeout did not fire")


class FakeEmbeddings:
    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
        return EmbeddingResponse(
            vectors=tuple((1.0,) + (0.0,) * 1023 for _ in texts),
            input_tokens=11,
        )


class BroadRelatedEmbeddings:
    """Treat all signals as related while keeping synthesized pair patterns distinct."""

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
        if not texts:
            raise AssertionError("embedding fixture requires at least one text")
        if len(texts) > 1:
            vectors = tuple((1.0,) + (0.0,) * 1023 for _ in texts)
        else:
            match = re.search(r"signal_(\d+)", next(iter(texts)))
            group = int(match.group(1)) // 2 if match is not None else 0
            vectors = tuple(
                tuple(1.0 if index == group else 0.0 for index in range(1024)) for _ in texts
            )
        return EmbeddingResponse(vectors=vectors, input_tokens=11)


class CancelAfterEmbedding:
    def __init__(self, cancellation: ToggleCancellation) -> None:
        self._cancellation = cancellation

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
        del texts
        self._cancellation.value = True
        return EmbeddingResponse(vectors=((1.0,) + (0.0,) * 1023,), input_tokens=11)


class NeverReturningEmbeddings:
    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
        del texts
        await asyncio.Event().wait()
        raise AssertionError("bounded embedding timeout did not fire")


class NeverReturningRoles:
    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        await asyncio.Event().wait()
        raise AssertionError("bounded model timeout did not fire")


class FakeRoles:
    def __init__(
        self,
        cancellation: ToggleCancellation,
        *,
        cancel: bool = False,
        fail_final: bool = False,
        invalid_first_final: bool = False,
        invalid_focused_evidence: bool = False,
        invalid_focused_source: bool = False,
        focused_source_index: int = 0,
        focused_span_ordinal: int = 1,
        final_do_not_pursue: bool = False,
        two_segments: bool = False,
        distinct_patterns: bool = False,
    ) -> None:
        self.cancellation = cancellation
        self.cancel = cancel
        self.plans = 0
        self.fail_final = fail_final
        self.invalid_first_final = invalid_first_final
        self.invalid_focused_evidence = invalid_focused_evidence
        self.invalid_focused_source = invalid_focused_source
        self.focused_source_index = focused_source_index
        self.focused_span_ordinal = focused_span_ordinal
        self.final_do_not_pursue = final_do_not_pursue
        self.focused_calls = 0
        self.focused_allowed_source_ids: tuple[str, ...] = ()
        self.focused_untrusted: tuple[Mapping[str, object], ...] = ()
        self.two_segments = two_segments
        self.distinct_patterns = distinct_patterns
        self.final_calls = 0

    async def execute[OutputT: BaseModel](
        self,
        spec: RoleSpec[OutputT],
        *,
        trusted_input: Mapping[str, object],
        untrusted_retrieved_data: tuple[Mapping[str, object], ...] = (),
        seed: int,
        before_model_call: Callable[[], Awaitable[None]] | None = None,
        after_model_call: Callable[[], Awaitable[None]] | None = None,
        before_generation: Callable[[int, int, bool], Awaitable[None]] | None = None,
        after_generation: Callable[[Any, bool], Awaitable[None]] | None = None,
    ) -> RoleExecutionResult[OutputT]:
        del (
            seed,
            before_model_call,
            after_model_call,
            before_generation,
            after_generation,
        )
        if self.cancel:
            self.cancellation.value = True
        if spec.role is RoleName.OPPORTUNITY_ANALYST:
            self.final_calls += 1
            if self.fail_final or (self.invalid_first_final and self.final_calls == 1):
                return RoleExecutionResult(
                    value=None,
                    attempts=2,
                    prompt_tokens=14,
                    completion_tokens=10,
                )
        value: BaseModel
        if spec.output_type is QueryPlan:
            self.plans += 1
            value = QueryPlan(
                queries=("discovery problem workflow",)
                if self.plans == 1
                else ("focused demand alternatives",)
            )
        elif spec.output_type is SignalSelection:
            source_id = cast(str, trusted_input["source_id"])
            value = SignalSelection(
                affected_user="Property operations staff",
                recurring_workflow="Triage and assign incoming maintenance requests",
                current_workaround="Email and spreadsheets",
                business_consequence="Delayed assignment and follow-up",
                source_id=source_id,
                span_ids=(f"{source_id}:s0001",),
                confidence=0.84,
            )
        elif spec.output_type is FocusedEvidenceSelection:
            self.focused_calls += 1
            allowed_source_ids = cast(tuple[str, ...], trusted_input["allowed_source_ids"])
            self.focused_allowed_source_ids = allowed_source_ids
            self.focused_untrusted = untrusted_retrieved_data
            source_id = (
                "focused_unlisted"
                if self.invalid_focused_source
                else allowed_source_ids[self.focused_source_index]
            )
            value = FocusedEvidenceSelection(
                source_id=source_id,
                span_ids=(
                    (f"{source_id}:s0001", f"{source_id}:s0003")
                    if self.invalid_focused_evidence
                    else (f"{source_id}:s{self.focused_span_ordinal:04d}",)
                ),
                supported_claim="Repeated manual handoffs support focused demand.",
                evidence_kind="supporting",
            )
        elif spec.output_type is PatternSynthesis:
            signals = cast(tuple[Mapping[str, object], ...], trusted_input["signals"])
            source_ids = tuple(cast(str, signal["source_id"]) for signal in signals)
            value = PatternSynthesis(
                pattern_summary=(
                    f"Pattern supported by {' and '.join(source_ids)}"
                    if self.distinct_patterns
                    else "Fragmented maintenance request intake delays follow-up"
                ),
                affected_user="Property operations staff",
                recurring_workflow="Triage and assign incoming maintenance requests",
                current_workaround="Email and spreadsheets",
                desired_outcome="Faster maintenance triage",
            )
        elif spec.output_type is SegmentMap:
            segments = [
                SegmentAssessment(
                    target_segment="Small property managers",
                    affected_user="Property operations staff",
                    likely_buyer="Operations leader",
                    recurring_workflow="Triage and assign maintenance requests",
                    frequency_value_hypothesis="Daily coordination consumes staff time",
                    constraints=("Limited integration capacity",),
                    reachability="Reachable through property-management associations",
                )
            ]
            if self.two_segments:
                segments.append(
                    SegmentAssessment(
                        target_segment="Facilities service firms",
                        affected_user="Property operations staff",
                        likely_buyer="Operations leader",
                        recurring_workflow="Triage and assign maintenance requests",
                        frequency_value_hypothesis="Daily coordination consumes staff time",
                        constraints=("Limited integration capacity",),
                        reachability="Reachable through facilities associations",
                    )
                )
            value = SegmentMap(segments=tuple(segments))
        elif spec.role is RoleName.PRODUCT_STRATEGIST:
            value = SegmentSolution(
                target_segment=cast(str, trusted_input["target_segment"]),
                likely_buyer=cast(str, trusted_input["likely_buyer"]),
                desired_outcome="Faster maintenance triage",
                proposed_solution="A narrow intake and follow-up queue",
                delivery_model="Local web application",
            )
        else:
            allowed = cast(tuple[str, ...], trusted_input["allowed_evidence_ids"])
            strategy = cast(Mapping[str, object], trusted_input["strategy"])
            report = fake_opportunity_aggregate(UUID(int=900)).report
            claims = tuple(
                claim.model_copy(
                    update={"evidence_ids": (allowed[0],)}
                    if claim.label in {"fact", "conflict"}
                    else {}
                )
                for claim in report.claims
            )
            value = report.model_copy(
                update={
                    "supporting_evidence_ids": allowed,
                    "negative_evidence_ids": (),
                    "conflicting_evidence_ids": (),
                    "claims": claims,
                    "target_segment": cast(str, strategy["target_segment"]),
                    "likely_buyer": cast(str, strategy["likely_buyer"]),
                    "source_diversity_limitation": None,
                    "conflict_limitation": None,
                    "critical_blocker": self.final_do_not_pursue,
                }
            )
        return RoleExecutionResult(
            value=cast(OutputT, value),
            attempts=1,
            prompt_tokens=7,
            completion_tokens=5,
        )


class FakeStore:
    def __init__(self, cancellation: ToggleCancellation) -> None:
        self.cancellation = cancellation
        self.committed: list[Any] = []
        self.cancel_on_nearest = False
        self.cancel_after_commit = False
        self.exact_identity: UUID | None = None
        self.exact_pattern: UUID | None = None
        self.rediscoveries: list[UUID] = []
        self.similarity_matches: tuple[tuple[int, float], ...] = (
            (701, 0.95),
            (702, 0.86),
            (703, 0.81),
        )

    async def find_identity(self, fingerprint: str) -> UUID | None:
        del fingerprint
        return self.exact_identity

    async def record_rediscovery(
        self, *, run_id: UUID, opportunity_id: UUID, lifecycle_event_id: UUID
    ) -> None:
        del run_id, lifecycle_event_id
        self.rediscoveries.append(opportunity_id)

    async def nearest_problems(
        self,
        embedding: tuple[float, ...],
        *,
        limit: int,
        minimum_similarity: float,
    ) -> tuple[Any, ...]:
        del embedding, minimum_similarity
        if self.cancel_on_nearest:
            self.cancellation.value = True
        from pensae.opportunities import SimilarityMatch

        return tuple(
            SimilarityMatch(UUID(int=value), similarity)
            for value, similarity in self.similarity_matches[:limit]
        )

    async def find_pattern_fingerprint(self, fingerprint: str) -> UUID | None:
        del fingerprint
        return self.exact_pattern

    async def nearest_patterns(
        self,
        embedding: tuple[float, ...],
        *,
        limit: int,
        minimum_similarity: float,
    ) -> tuple[Any, ...]:
        del embedding, limit, minimum_similarity
        return ()

    async def commit(self, aggregate: Any) -> UUID:
        self.committed.append(aggregate)
        if self.cancel_after_commit:
            self.cancellation.value = True
        return aggregate.opportunity_id


def _operations(
    cancellation: ToggleCancellation,
    *,
    research_scope: str = "Property management",
    search_cancel: bool = False,
    retrieval_cancel: bool = False,
    model_cancel: bool = False,
    fail_final: bool = False,
    invalid_first_final: bool = False,
    invalid_focused_evidence: bool = False,
    invalid_focused_source: bool = False,
    focused_source_index: int = 0,
    focused_span_ordinal: int = 1,
    final_do_not_pursue: bool = False,
    one_source: bool = False,
    two_segments: bool = False,
    focused_sources: int = 1,
    distinct_patterns: bool = False,
    retrieval_text_characters: int | None = None,
) -> tuple[ResearchWorkflowOperations, FakeStore]:
    store = FakeStore(cancellation)
    operations = ResearchWorkflowOperations(
        run_id=UUID(int=600),
        industry=research_scope,
        protected=ProtectedConfig.load(),
        search=FakeSearch(
            cancellation,
            cancel=search_cancel,
            one_source=one_source,
            focused_sources=focused_sources,
        ),
        retrieval=FakeRetrieval(
            cancellation,
            cancel=retrieval_cancel,
            text_characters=retrieval_text_characters,
        ),
        roles=cast(
            Any,
            FakeRoles(
                cancellation,
                cancel=model_cancel,
                fail_final=fail_final,
                invalid_first_final=invalid_first_final,
                invalid_focused_evidence=invalid_focused_evidence,
                invalid_focused_source=invalid_focused_source,
                focused_source_index=focused_source_index,
                focused_span_ordinal=focused_span_ordinal,
                final_do_not_pursue=final_do_not_pursue,
                two_segments=two_segments,
                distinct_patterns=distinct_patterns,
            ),
        ),
        embeddings=FakeEmbeddings(),
        store=cast(Any, store),
        cancellation=cancellation,
    )
    return operations, store


def test_workflow_operations_compose_stable_run_capabilities() -> None:
    operations, _store = _operations(ToggleCancellation())

    assert ResearchWorkflowOperations.__bases__ == (object,)
    assert type(operations.context).__module__ == "pensae.research.context"
    assert type(operations.calls).__module__ == "pensae.research.calls"
    assert {handler.__self__.__class__.__module__ for handler in operations._handlers.values()} == {
        "pensae.research.stages.commit",
        "pensae.research.stages.discovery",
        "pensae.research.stages.synthesis",
        "pensae.research.stages.validation",
    }


async def _synthesize_broad_pool(
    indexes: tuple[int, ...],
    *,
    pattern_limit: int = 10,
    distinct_patterns: bool = True,
) -> tuple[ResearchWorkflowOperations, NodeResult]:
    cancellation = ToggleCancellation()
    operations, _store = _operations(
        cancellation,
        distinct_patterns=distinct_patterns,
    )
    operations.context.embeddings = BroadRelatedEmbeddings()
    operations.context.memory.selected = tuple(_selected_signal(index) for index in indexes)
    bounds = replace(ProtectedConfig.load().research.bounds, patterns=pattern_limit)
    result = await operations.run_node(WorkflowNode.PATTERN_SYNTHESIS, {}, bounds)
    return operations, result


@pytest.mark.anyio
async def test_broad_related_signal_pool_yields_multiple_independent_patterns() -> None:
    operations, result = await _synthesize_broad_pool(tuple(range(8)))

    patterns = operations.context.memory.patterns
    groups = tuple(
        tuple(source.selection.source_id for source in pattern.selected) for pattern in patterns
    )
    assert result.counters.patterns == 4
    assert groups == (
        ("signal_0", "signal_1"),
        ("signal_2", "signal_3"),
        ("signal_4", "signal_5"),
        ("signal_6", "signal_7"),
    )
    assert all(len(set(group)) == 2 for group in groups)
    assert len({source for group in groups for source in group}) == 8


@pytest.mark.anyio
async def test_signal_subclusters_have_stable_order_and_respect_pattern_cap() -> None:
    forward, _ = await _synthesize_broad_pool(tuple(range(8)), pattern_limit=3)
    reverse, _ = await _synthesize_broad_pool(tuple(reversed(range(8))), pattern_limit=3)

    def groups(operations: ResearchWorkflowOperations) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(source.selection.source_id for source in pattern.selected)
            for pattern in operations.context.memory.patterns
        )

    assert groups(forward) == groups(reverse)
    assert len(groups(forward)) == 3


@pytest.mark.anyio
async def test_unpaired_related_signal_does_not_invent_a_second_pattern() -> None:
    operations, result = await _synthesize_broad_pool((0, 1, 2))

    assert result.counters.patterns == 1
    assert tuple(
        source.selection.source_id for source in operations.context.memory.patterns[0].selected
    ) == ("signal_0", "signal_1")


@pytest.mark.anyio
async def test_subclustered_duplicate_patterns_remain_rejected() -> None:
    operations, _ = await _synthesize_broad_pool(tuple(range(6)), distinct_patterns=False)

    patterns = operations.context.memory.patterns
    assert len(patterns) == 3
    assert patterns[0].exact_duplicate is False
    assert all(pattern.exact_duplicate for pattern in patterns[1:])
    assert all(pattern.insufficient_distinction for pattern in patterns[1:])


@pytest.mark.anyio
async def test_complete_two_pass_pipeline_commits_one_candidate_and_accounts_shortfall() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(
        cancellation,
        research_scope="Investigate recurring coordination problems across local services",
    )
    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "completed_with_warnings"
    assert result.trace == tuple(node.value for node in WORKFLOW_SEQUENCE)
    assert result.target_count == 5
    assert result.admitted_count == 1
    assert result.evaluated_count == 1
    assert result.achieved_count == 0
    assert result.shortfall_code == "insufficient_evidence"
    assert result.non_counting_outcomes.unresolved_possible_rediscovery == 1
    assert result.committed_count == 1
    assert len(store.committed) == 1
    aggregate = store.committed[0]
    assert aggregate.primary_industry == "Property management"
    assert len(aggregate.sources) == 3
    assert len(aggregate.evidence) == 3
    assert len(aggregate.signals) == 2
    assert all(item.excerpt and len(item.excerpt) <= 500 for item in aggregate.evidence)
    durable_json = json.dumps(aggregate.model_dump(mode="json"))
    for prohibited in ("normalized_text", "raw_output", '"prompt":', "search_response"):
        assert prohibited not in durable_json
    assert [relation.opportunity_id for relation in aggregate.relations] == [
        UUID(int=701),
        UUID(int=702),
        UUID(int=703),
    ]
    assert aggregate.classification == "possible_rediscovery"
    assert result.counters.queries == 2
    assert result.counters.retrieved_pages == 3
    assert result.counters.signals == 2
    assert result.counters.opportunities == 1
    assert {item.role for item in result.model_usage} == {
        "research_planner",
        "problem_analyst",
        "product_strategist",
        "opportunity_analyst",
        "signal_clustering",
        "pattern_similarity",
        "similarity_embedding",
    }
    assert sum(item.input_tokens + item.output_tokens for item in result.model_usage) > 0
    assert {item.model for item in result.model_usage} == {
        ProtectedConfig.load().policy.chat_model_id,
        ProtectedConfig.load().policy.embedding_model_id,
    }
    assert all(item.attempts == 1 for item in result.model_usage)
    assert operations.context.memory.retrieved == ()
    assert operations.context.memory.selected == ()
    assert operations.context.memory.patterns == ()
    assert operations.context.memory.concepts == ()
    assert operations.context.memory.focused == ()
    assert operations.context.memory.aggregates == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("similarity_matches", "expected_classification"),
    [
        ((), "new"),
        (((701, 0.80),), "related"),
    ],
)
async def test_new_or_related_counts_toward_target_even_with_do_not_pursue_verdict(
    similarity_matches: tuple[tuple[int, float], ...],
    expected_classification: str,
) -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, final_do_not_pursue=True)
    store.similarity_matches = similarity_matches

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.committed_count == 1
    assert result.achieved_count == 1
    assert len(store.committed) == 1
    aggregate = store.committed[0]
    _scores, verdict = score_opportunity_report(aggregate.report)
    assert aggregate.classification == expected_classification
    assert verdict.value == "do_not_pursue"


@pytest.mark.anyio
async def test_weak_single_source_stops_before_solution_design_or_commit() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, one_source=True)

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "completed_with_warnings"
    assert result.survivor_count == 0
    assert result.counters.concepts == 0
    assert store.committed == []
    assert cast(FakeRoles, operations.context.roles).final_calls == 0


@pytest.mark.anyio
async def test_noncontiguous_focused_evidence_is_discarded_before_analysis_or_commit() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, invalid_focused_evidence=True)

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "completed_with_warnings"
    assert "evidence_integrity_failure" in result.warnings
    assert "focused_evidence_unavailable" in result.warnings
    assert store.committed == []
    assert cast(FakeRoles, operations.context.roles).final_calls == 0


@pytest.mark.anyio
async def test_focused_retrieval_selects_one_source_with_one_model_call() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(
        cancellation,
        focused_sources=3,
        focused_source_index=1,
    )

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    roles = cast(FakeRoles, operations.context.roles)
    assert roles.focused_calls == 1
    assert result.counters.retrieved_pages == 5
    assert result.counters.retrieved_bytes == 700
    assert len(store.committed) == 1
    assert store.committed[0].sources[-1].url == "https://focused-1.example.test/source"


def test_focused_span_pack_is_stable_fair_and_preserves_prompt_headroom() -> None:
    policy = ProtectedConfig.load().research

    def source(source_id: str) -> tuple[str, tuple[Mapping[str, object], ...]]:
        return (
            source_id,
            tuple(
                {
                    "source_id": source_id,
                    "span_id": f"{source_id}:s{ordinal:04d}",
                    "text": f"<{source_id}> evidence {ordinal} " + "x" * 220,
                }
                for ordinal in range(1, 151)
            ),
        )

    reverse_sources = tuple(source(source_id) for source_id in ("source_c", "source_b", "source_a"))
    forward_sources = tuple(reversed(reverse_sources))
    reverse_pack = pack_untrusted_spans(
        reverse_sources,
        prompt_input_max_tokens=policy.prompt_input_max_tokens,
    )
    forward_pack = pack_untrusted_spans(
        forward_sources,
        prompt_input_max_tokens=policy.prompt_input_max_tokens,
    )

    assert reverse_pack == forward_pack
    assert reverse_pack.truncated is True
    assert reverse_pack.byte_limit == (
        policy.prompt_input_max_tokens - FOCUSED_SELECTOR_HEADROOM_TOKENS
    )
    assert reverse_pack.serialized_bytes <= reverse_pack.byte_limit
    assert reverse_pack.source_ids == ("source_a", "source_b", "source_c")
    assert reverse_pack.omitted_source_ids == ()
    assert tuple(span["source_id"] for span in reverse_pack.spans[:3]) == (
        "source_a",
        "source_b",
        "source_c",
    )
    original_spans = {
        (span["source_id"], span["span_id"], span["text"])
        for _source_id, spans in forward_sources
        for span in spans
    }
    assert all(
        (span["source_id"], span["span_id"], span["text"]) in original_spans
        for span in reverse_pack.spans
    )


@pytest.mark.anyio
async def test_large_focused_pages_are_fairly_packed_before_one_selection_call() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(
        cancellation,
        focused_sources=3,
        focused_source_index=1,
        retrieval_text_characters=100_000,
    )

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    roles = cast(FakeRoles, operations.context.roles)
    packed_source_ids = tuple(
        dict.fromkeys(cast(str, span["source_id"]) for span in roles.focused_untrusted)
    )
    assert roles.focused_calls == 1
    assert "focused_evidence_prompt_truncated" in result.warnings
    assert roles.focused_allowed_source_ids == (
        "focused_1_1",
        "focused_1_2",
        "focused_1_3",
    )
    assert set(packed_source_ids) == set(roles.focused_allowed_source_ids)
    assert all(
        cast(str, span["span_id"]).startswith(f"{span['source_id']}:")
        and span["text"] == "x" * len(cast(str, span["text"]))
        for span in roles.focused_untrusted
    )
    assert result.counters.retrieved_pages == 5
    assert result.counters.retrieved_bytes == 700
    assert len(store.committed) == 1
    focused_source = store.committed[0].sources[-1]
    assert focused_source.url == "https://focused-1.example.test/source"
    assert store.committed[0].evidence[-1].excerpt == "x" * 240


@pytest.mark.anyio
async def test_focused_selector_cannot_reference_an_omitted_but_real_source_span() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(
        cancellation,
        focused_sources=3,
        retrieval_text_characters=100_000,
        focused_span_ordinal=400,
    )

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    roles = cast(FakeRoles, operations.context.roles)
    assert "focused_1_1:s0400" not in {span["span_id"] for span in roles.focused_untrusted}
    assert "focused_evidence_prompt_truncated" in result.warnings
    assert "evidence_integrity_failure" in result.warnings
    assert result.non_counting_outcomes.incomplete_candidate == 1
    assert store.committed == []


@pytest.mark.anyio
async def test_unlisted_focused_source_selection_is_discarded_mechanically() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(
        cancellation,
        focused_sources=3,
        invalid_focused_source=True,
    )

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert cast(FakeRoles, operations.context.roles).focused_calls == 1
    assert result.counters.retrieved_pages == 5
    assert "evidence_integrity_failure" in result.warnings
    assert result.non_counting_outcomes.incomplete_candidate == 1
    assert store.committed == []


@pytest.mark.anyio
async def test_focused_retrieval_page_limit_stops_before_selection_call() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, focused_sources=3)
    bounds = replace(ProtectedConfig.load().research.bounds, run_pages=3)

    result = await FixedWorkflowGraph(
        bounds=bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "stopped"
    assert result.counters.retrieved_pages == 3
    assert result.limit_code == "retrieved_pages"
    assert result.limit_stage == WorkflowNode.FOCUSED_RETRIEVAL.value
    assert cast(FakeRoles, operations.context.roles).focused_calls == 0
    assert store.committed == []


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["model", "search", "retrieval", "precommit"])
async def test_stop_is_observed_after_each_external_boundary(boundary: str) -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(
        cancellation,
        model_cancel=boundary == "model",
        search_cancel=boundary == "search",
        retrieval_cancel=boundary == "retrieval",
    )
    if boundary == "precommit":
        store.cancel_on_nearest = True
    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "stopped"
    assert result.trace[-1] == WorkflowNode.TERMINAL_CLEANUP.value
    assert store.committed == []
    if boundary == "model":
        assert result.counters.model_calls == 1
        assert result.counters.input_tokens == 7
        assert result.counters.output_tokens == 5
    if boundary == "search":
        assert result.counters.queries == 1
        assert result.counters.search_results == 2
    assert operations.context.memory.retrieved == ()
    assert operations.context.memory.selected == ()
    assert operations.context.memory.aggregates == ()
    if boundary == "search":
        assert result.counters.queries == 1
    if boundary == "retrieval":
        assert result.counters.retrieved_pages == 1
        assert result.counters.retrieved_bytes == 140


@pytest.mark.anyio
async def test_stop_after_an_independent_commit_preserves_the_earlier_commit() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation)
    first = fake_opportunity_aggregate(UUID(int=801))
    operations.context.memory.aggregates = (first,)
    store.cancel_after_commit = True

    result = await operations.run_node(
        WorkflowNode.SIMILARITY_COMMIT,
        {},
        ProtectedConfig.load().research.bounds,
    )

    assert result.committed_count == 1
    assert result.warnings == ("stop_requested",)
    assert [item.opportunity_id for item in store.committed] == [first.opportunity_id]


@pytest.mark.anyio
async def test_exact_fingerprint_records_rediscovery_without_duplicate_commit() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation)
    aggregate = fake_opportunity_aggregate(UUID(int=803))
    operations.context.memory.aggregates = (aggregate,)
    store.exact_identity = UUID(int=804)

    result = await operations.run_node(
        WorkflowNode.SIMILARITY_COMMIT,
        {},
        ProtectedConfig.load().research.bounds,
    )

    assert result.committed_count == 0
    assert result.warnings == ("exact_rediscovery",)
    assert store.committed == []
    assert store.rediscoveries == [UUID(int=804)]


@pytest.mark.anyio
async def test_retained_exact_pattern_is_rejected_before_solution_design() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation)
    store.exact_pattern = UUID(int=805)

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "completed_with_warnings"
    assert result.survivor_count == 0
    assert result.counters.concepts == 0
    assert store.committed == []


def test_run_cancellation_has_no_process_signal_or_supervisor_path() -> None:
    paths = (
        *Path("src/pensae/research").rglob("*.py"),
        Path("src/pensae/runs/control.py"),
        Path("src/pensae/runs/service.py"),
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called_attributes.isdisjoint({"kill", "terminate", "send_signal"})
        assert "model_runtime.supervisor" not in source


@pytest.mark.anyio
async def test_invalid_optional_final_candidate_is_discarded_and_cleanup_runs() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, fail_final=True)

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "completed_with_warnings"
    assert "final_candidates_unavailable" in result.warnings
    assert store.committed == []
    assert operations.context.memory.retrieved == ()
    assert operations.context.memory.selected == ()
    assert operations.context.memory.focused == ()
    assert operations.context.memory.aggregates == ()


@pytest.mark.anyio
async def test_invalid_optional_candidate_is_discarded_while_later_candidate_commits() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, invalid_first_final=True, two_segments=True)

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.status == "completed_with_warnings"
    assert "final_candidate_discarded" in result.warnings
    assert result.committed_count == 1
    assert len(store.committed) == 1


@pytest.mark.anyio
async def test_concepts_from_one_pattern_reuse_identical_discovery_support_records() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation, two_segments=True)

    result = await FixedWorkflowGraph(
        bounds=ProtectedConfig.load().research.bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=600))

    assert result.committed_count == 2
    first, second = store.committed
    assert first.sources[:2] == second.sources[:2]
    assert first.evidence[:2] == second.evidence[:2]
    assert first.sources[-1].id != second.sources[-1].id
    assert first.evidence[-1].id != second.evidence[-1].id


@pytest.mark.anyio
async def test_model_call_ceiling_is_checked_before_starting_another_role_call() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    operations.context.accounting.model_calls = ProtectedConfig.load().research.bounds.model_calls

    with pytest.raises(WorkLimitExceeded, match="model call"):
        await operations.run_node(
            WorkflowNode.DISCOVERY_PLAN,
            {},
            ProtectedConfig.load().research.bounds,
        )


@pytest.mark.anyio
async def test_search_retry_and_partial_work_are_accounted() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    search = FakeSearch(cancellation, retries=1)
    operations.context.search = search
    operations.context.memory.queries = ("retry this bounded query",)

    result = await operations.run_node(
        WorkflowNode.DISCOVERY_SEARCH,
        {},
        ProtectedConfig.load().research.bounds,
    )

    assert len(search.calls) == 1
    assert result.counters.search_retries == 1
    assert operations.accounting_snapshot().search_retries == 1
    assert operations.accounting_snapshot().queries == 1
    assert operations.accounting_snapshot().search_results == 2


@pytest.mark.anyio
async def test_discovery_search_stops_at_the_unique_url_ceiling_across_batches() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    search = UniqueSearch()
    operations.context.search = search
    operations.context.memory.queries = tuple(f"bounded query {index}" for index in range(8))
    bounds = ProtectedConfig.load().research.bounds

    result = await operations.run_node(WorkflowNode.DISCOVERY_SEARCH, {}, bounds)

    assert len(search.calls) == 6
    assert len(operations.context.memory.hits) == bounds.unique_urls
    assert result.counters.queries == 6
    assert result.counters.search_results == 60
    assert result.counters.unique_urls == bounds.unique_urls
    assert operations.accounting_snapshot().unique_urls == bounds.unique_urls


@pytest.mark.anyio
async def test_discovery_search_caps_unique_urls_mid_batch() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    search = UniqueSearch()
    operations.context.search = search
    operations.context.memory.queries = tuple(f"bounded query {index}" for index in range(8))
    bounds = replace(ProtectedConfig.load().research.bounds, unique_urls=25)

    result = await operations.run_node(WorkflowNode.DISCOVERY_SEARCH, {}, bounds)

    assert len(search.calls) == 3
    assert len(operations.context.memory.hits) == 25
    assert result.counters.queries == 3
    assert result.counters.search_results == 30
    assert result.counters.unique_urls == 25
    assert operations.accounting_snapshot().unique_urls == 25


@pytest.mark.anyio
async def test_completed_search_is_accounted_before_post_call_stop() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    operations.context.search = FakeSearch(cancellation, cancel=True, retries=1)

    with pytest.raises(WorkflowStopRequested):
        await operations.calls._search_call("bounded query", ProtectedConfig.load().research.bounds)

    assert operations.accounting_snapshot().queries == 1
    assert operations.accounting_snapshot().search_results == 2
    assert operations.accounting_snapshot().search_retries == 1


@pytest.mark.anyio
async def test_completed_embedding_is_accounted_before_post_call_stop() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    operations.context.embeddings = CancelAfterEmbedding(cancellation)

    with pytest.raises(WorkflowStopRequested):
        await operations.calls._embed(
            ("bounded text",),
            ProtectedConfig.load().research.bounds,
            role="test_embedding",
        )

    assert operations.accounting_snapshot().model_calls == 1
    assert operations.accounting_snapshot().input_tokens == 11
    assert operations.model_usage_snapshot()[0].role == "test_embedding"


@pytest.mark.anyio
async def test_failed_search_retry_is_accounted_before_dependency_failure() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    operations.context.search = FailedRetrySearch()

    with pytest.raises(RuntimeError, match="search dependency unavailable"):
        await operations.calls._search_call(
            "retry this bounded query",
            ProtectedConfig.load().research.bounds,
        )

    assert operations.accounting_snapshot().queries == 1
    assert operations.accounting_snapshot().search_retries == 1
    assert operations.accounting_snapshot().search_results == 0


@pytest.mark.anyio
@pytest.mark.parametrize("dependency", ["search", "retrieval", "model", "embedding"])
async def test_each_research_external_timeout_is_bounded_and_mapped(dependency: str) -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    if dependency == "search":
        operations.context.search = NeverReturningSearch()
        bounds = replace(bounds, search_timeout_seconds=0)
        call = operations.calls._search_call("bounded query", bounds)
    elif dependency == "retrieval":
        operations.context.retrieval = NeverReturningRetrieval()
        bounds = replace(bounds, search_timeout_seconds=0)
        call = operations.calls._retrieval_call("https://one.example.test/source", bounds)
    elif dependency == "model":
        operations.context.roles = cast(Any, NeverReturningRoles())
        bounds = replace(bounds, model_timeout_seconds=0)
        call = operations.run_node(WorkflowNode.DISCOVERY_PLAN, {}, bounds)
    else:
        operations.context.embeddings = NeverReturningEmbeddings()
        bounds = replace(bounds, embedding_timeout_seconds=0)
        call = operations.calls._embed(("bounded text",), bounds, role="test_embedding")

    with pytest.raises(WorkflowDependencyUnavailable, match="dependency unavailable"):
        await call


def test_current_run_similarity_is_not_hidden_by_weaker_retained_match() -> None:
    assert _strongest_pattern_similarity(0.99, (0.71, 0.75)) == 0.99


@pytest.mark.anyio
async def test_source_and_run_byte_bounds_stop_after_accounting_completed_fetch() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    operations.context.retrieval = FakeRetrieval(
        cancellation, response_bytes=bounds.source_bytes + 1
    )

    with pytest.raises(WorkLimitExceeded, match="source byte"):
        await operations.calls._retrieval_call("https://one.example.test/source", bounds)

    assert operations.accounting_snapshot().retrieved_pages == 1
    assert operations.accounting_snapshot().retrieved_bytes == bounds.source_bytes


@pytest.mark.anyio
async def test_next_chat_call_is_blocked_when_its_output_cap_could_exceed_run_tokens() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    fake_chat = FakeChatClient(
        (
            ChatResponse(
                content='{"queries":["must not run"]}', prompt_tokens=10, completion_tokens=2
            ),
        ),
        token_counts=(10,),
    )
    operations.context.roles = StructuredRoleExecutor(chat=fake_chat)
    operations.context.accounting.total_tokens = (
        bounds.total_run_tokens - ProtectedConfig.load().research.planner_output_max_tokens - 9
    )

    with pytest.raises(WorkLimitExceeded, match="would be exceeded"):
        await operations.run_node(WorkflowNode.DISCOVERY_PLAN, {}, bounds)

    assert fake_chat.requests == []


@pytest.mark.anyio
async def test_run_repair_ceiling_blocks_repair_and_keeps_actual_call_accounting() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    fake_chat = FakeChatClient(
        (
            ChatResponse(content='{"queries":[]}', prompt_tokens=10, completion_tokens=2),
            ChatResponse(
                content='{"queries":["repair must not run"]}',
                prompt_tokens=14,
                completion_tokens=4,
            ),
        ),
        token_counts=(10, 14),
    )
    operations.context.roles = StructuredRoleExecutor(chat=fake_chat)
    operations.context.accounting.repairs = bounds.repairs

    with pytest.raises(WorkLimitExceeded, match="repair ceiling"):
        await operations.run_node(WorkflowNode.DISCOVERY_PLAN, {}, bounds)

    assert len(fake_chat.requests) == 1
    assert operations.accounting_snapshot().model_calls == 1
    assert operations.accounting_snapshot().input_tokens == 10
    assert operations.accounting_snapshot().output_tokens == 2
    assert [(item.repair, item.attempts) for item in operations.model_usage_snapshot()] == [
        (False, 1)
    ]


@pytest.mark.anyio
async def test_late_pool_candidate_uses_its_slot_without_reserving_nonexistent_candidates() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    operations.context.accounting.counters = replace(
        operations.context.accounting.counters,
        queries=26,
    )

    admitted = operations.calls.begin_finalization(
        achieved_count=0,
        admitted_count=8,
        evaluated_count=7,
        stage=WorkflowNode.FOCUSED_PLAN.value,
    )

    assert admitted.remaining.queries == 14
    assert admitted.reserved.queries == 0
    assert admitted.available.queries == 14
    for index in range(3):
        await operations.calls._search_call(f"focused final candidate {index}", bounds)
    assert operations.accounting_snapshot().queries == 29

    finished = operations.calls.finish_finalization(
        achieved_count=0,
        admitted_count=8,
        evaluated_count=8,
    )
    assert finished.reserved.queries == 0


@pytest.mark.anyio
async def test_current_pool_slot_cannot_spend_any_real_future_candidate_reserve() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    operations.context.accounting.counters = replace(
        operations.context.accounting.counters,
        queries=25,
    )

    admitted = operations.calls.begin_finalization(
        achieved_count=0,
        admitted_count=8,
        evaluated_count=3,
        stage=WorkflowNode.FOCUSED_PLAN.value,
    )

    assert admitted.available.queries == 3
    assert admitted.reserved.queries == 12
    for index in range(3):
        await operations.calls._search_call(f"focused bounded candidate {index}", bounds)
    with pytest.raises(BudgetLimitExceeded) as raised:
        await operations.calls._search_call("focused optional overspend", bounds)

    assert raised.value.dimension is WorkDimension.QUERIES
    assert raised.value.available == 0
    assert raised.value.reserved == 12
    assert operations.accounting_snapshot().queries == 28


@pytest.mark.anyio
async def test_eight_noncounting_candidates_exhaust_pool_without_false_work_limit() -> None:
    cancellation = ToggleCancellation()
    operations, store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds

    async def discovery_with_full_query_allowance(
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        for index in range(bounds.discovery_queries):
            await operations.calls._search_call(f"discovery bounded query {index}", bounds)
        return NodeResult()

    async def eight_survivors(bounds: ProtectedWorkflowBounds) -> NodeResult:
        del bounds
        return NodeResult(survivor_count=8)

    async def admit_eight(bounds: ProtectedWorkflowBounds) -> NodeResult:
        del bounds
        operations.context.memory.concepts = cast(Any, tuple(range(8)))
        return NodeResult(admitted_count=8)

    async def candidate_with_full_query_allowance(
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        index = operations.context.memory.evaluated_count
        for query_index in range(bounds.focused_queries_per_concept):
            await operations.calls._search_call(
                f"focused candidate {index} query {query_index}", bounds
            )
        return NodeResult()

    async def no_work(bounds: ProtectedWorkflowBounds) -> NodeResult:
        del bounds
        return NodeResult()

    async def discard_candidate(bounds: ProtectedWorkflowBounds) -> NodeResult:
        del bounds
        operations.context.memory.evaluated_count += 1
        return NodeResult(
            warnings=("final_candidate_discarded",),
            evaluated_count=operations.context.memory.evaluated_count,
            candidate_ready=False,
            candidate_outcome="invalid_candidate",
        )

    operations._handlers[WorkflowNode.DISCOVERY_PLAN] = discovery_with_full_query_allowance
    operations._handlers[WorkflowNode.PRELIMINARY_GATE] = eight_survivors
    operations._handlers[WorkflowNode.CONCEPT_DESIGN] = admit_eight
    operations._handlers[WorkflowNode.FOCUSED_PLAN] = candidate_with_full_query_allowance
    operations._handlers[WorkflowNode.FOCUSED_SEARCH] = no_work
    operations._handlers[WorkflowNode.FOCUSED_RETRIEVAL] = no_work
    operations._handlers[WorkflowNode.FINAL_ANALYSIS] = discard_candidate

    result = await FixedWorkflowGraph(
        bounds=bounds,
        operations=operations,
        cancellation=cancellation,
    ).run(UUID(int=601))

    assert result.status == "completed_with_warnings"
    assert result.evaluated_count == 8
    assert result.achieved_count == 0
    assert result.committed_count == 0
    assert result.counters.queries == 32
    assert result.non_counting_outcomes.invalid_candidate == 8
    assert result.shortfall_code == "insufficient_evidence"
    assert result.limit_code is None
    assert "work_limit_exceeded" not in result.warnings
    assert store.committed == []


@pytest.mark.anyio
async def test_second_slot_repair_cannot_consume_later_candidate_reserve() -> None:
    cancellation = ToggleCancellation()
    operations, _store = _operations(cancellation)
    bounds = ProtectedConfig.load().research.bounds
    fake_chat = FakeChatClient(
        (
            ChatResponse(content='{"queries":[]}', prompt_tokens=10, completion_tokens=2),
            ChatResponse(
                content='{"queries":["first repair succeeds"]}',
                prompt_tokens=14,
                completion_tokens=4,
            ),
            ChatResponse(content='{"queries":[]}', prompt_tokens=10, completion_tokens=2),
            ChatResponse(
                content='{"queries":["reserved repair must not run"]}',
                prompt_tokens=14,
                completion_tokens=4,
            ),
        ),
        token_counts=(10, 14, 10, 14),
    )
    operations.context.roles = StructuredRoleExecutor(chat=fake_chat)
    operations.context.accounting.repairs = 3
    operations.context.accounting.counters = replace(
        operations.context.accounting.counters,
        repairs=3,
    )
    operations.calls.begin_finalization(
        achieved_count=0,
        admitted_count=5,
        evaluated_count=0,
        stage=WorkflowNode.FOCUSED_PLAN.value,
    )
    planner = cast(RoleSpec[QueryPlan], operations.context.specs[RoleName.RESEARCH_PLANNER])

    first = await operations.calls._model(
        planner,
        trusted_input={"maximum_queries": 3},
        seed=1,
        timeout_seconds=bounds.model_timeout_seconds,
    )
    assert first.attempts == 2
    operations.calls.set_budget_stage(WorkflowNode.FOCUSED_RETRIEVAL.value)
    with pytest.raises(BudgetLimitExceeded) as raised:
        await operations.calls._model(
            planner,
            trusted_input={"maximum_queries": 3},
            seed=2,
            timeout_seconds=bounds.model_timeout_seconds,
        )

    assert raised.value.dimension is WorkDimension.REPAIRS
    assert raised.value.limit == "repairs"
    assert raised.value.stage == WorkflowNode.FOCUSED_RETRIEVAL.value
    assert raised.value.available == 0
    assert raised.value.reserved == 4
    assert len(fake_chat.requests) == 3
    assert operations.accounting_snapshot().repairs == 4
    assert operations.accounting_snapshot().model_calls == 3
    assert [(item.repair, item.attempts) for item in operations.model_usage_snapshot()] == [
        (False, 1),
        (True, 1),
        (False, 1),
    ]


@pytest.mark.anyio
async def test_embedding_input_cap_rejects_nonclustering_work_before_the_call() -> None:
    operations, _store = _operations(ToggleCancellation())
    bounds = ProtectedConfig.load().research.bounds

    class RecordingEmbeddings:
        def __init__(self) -> None:
            self.requests: list[tuple[str, ...]] = []

        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
            self.requests.append(texts)
            return EmbeddingResponse(vectors=((1.0,),), input_tokens=1)

    embeddings = RecordingEmbeddings()
    operations.context.embeddings = embeddings

    with pytest.raises(WorkLimitExceeded, match="embedding input token limit"):
        await operations.calls._embed(("x" * 8_193,), bounds, role="similarity_embedding")

    assert embeddings.requests == []


@pytest.mark.anyio
async def test_signal_clustering_batch_is_fairly_truncated_under_the_embedding_cap() -> None:
    operations, _store = _operations(ToggleCancellation())
    bounds = ProtectedConfig.load().research.bounds

    class RecordingEmbeddings:
        def __init__(self) -> None:
            self.requests: list[tuple[str, ...]] = []

        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
            self.requests.append(texts)
            return EmbeddingResponse(
                vectors=tuple((1.0,) for _text in texts),
                input_tokens=2_048,
            )

    embeddings = RecordingEmbeddings()
    operations.context.embeddings = embeddings
    texts = tuple(f"signal-{index}-" + "x" * 1_000 for index in range(30))

    result = await operations.calls._embed(texts, bounds, role="signal_clustering")

    assert len(result.vectors) == 30
    assert len(embeddings.requests) == 1
    request = embeddings.requests[0]
    assert len(request) == 30
    assert all(request)
    assert sum(len(text.encode("utf-8")) for text in request) <= 8_192
