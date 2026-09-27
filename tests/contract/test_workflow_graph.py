from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast
from uuid import UUID

import pytest

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.research.workflow import (
    WORKFLOW_SEQUENCE,
    CandidateOutcome,
    CounterDelta,
    FixedWorkflowGraph,
    NodeResult,
    WorkCounters,
    WorkflowContractFailure,
    WorkflowNode,
    WorkflowPartialCommitFailure,
    WorkLimitExceeded,
)


class ScriptedOperations:
    def __init__(self, results: Mapping[WorkflowNode, NodeResult] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[WorkflowNode] = []

    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        del state, bounds
        self.calls.append(node)
        return self.results.get(node, NodeResult())


class FailingOperations(ScriptedOperations):
    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        if node is WorkflowNode.PATTERN_SYNTHESIS:
            raise WorkflowContractFailure("pattern contract invalid")
        return await super().run_node(node, state, bounds)


class TimeoutOperations(ScriptedOperations):
    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        self.calls.append(node)
        if node is WorkflowNode.DISCOVERY_SEARCH:
            raise TimeoutError
        del state, bounds
        return NodeResult()


class UnexpectedFailureOperations(ScriptedOperations):
    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        self.calls.append(node)
        if node is WorkflowNode.FINAL_ANALYSIS:
            raise LookupError("database invariant")
        del state, bounds
        return NodeResult(
            survivor_count=1 if node is WorkflowNode.PRELIMINARY_GATE else None,
            admitted_count=1 if node is WorkflowNode.CONCEPT_DESIGN else None,
        )


class PartialCommitOperations(ScriptedOperations):
    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        self.calls.append(node)
        del state, bounds
        if node is WorkflowNode.SIMILARITY_COMMIT:
            raise WorkflowPartialCommitFailure(1)
        if node is WorkflowNode.PRELIMINARY_GATE:
            return NodeResult(survivor_count=2)
        if node is WorkflowNode.CONCEPT_DESIGN:
            return NodeResult(admitted_count=2)
        if node is WorkflowNode.FINAL_ANALYSIS:
            return NodeResult(evaluated_count=1, candidate_ready=True)
        return NodeResult()


class FiveCommitOperations(ScriptedOperations):
    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        del bounds
        self.calls.append(node)
        if node is WorkflowNode.PRELIMINARY_GATE:
            return NodeResult(survivor_count=8)
        if node is WorkflowNode.CONCEPT_DESIGN:
            return NodeResult(admitted_count=8, counters=CounterDelta(concepts=8))
        if node is WorkflowNode.FINAL_ANALYSIS:
            return NodeResult(
                evaluated_count=cast(int, state["evaluated_count"]) + 1,
                candidate_ready=True,
            )
        if node is WorkflowNode.SIMILARITY_COMMIT:
            return NodeResult(
                achieved_count=cast(int, state["achieved_count"]) + 1,
                committed_count=cast(int, state["committed_count"]) + 1,
                candidate_outcome="new",
                counters=CounterDelta(opportunities=1),
            )
        return NodeResult()


class MixedOutcomeOperations(ScriptedOperations):
    outcomes: tuple[CandidateOutcome, ...] = (
        "automatic_exact_rediscovery",
        "updated_version",
        "unresolved_possible_rediscovery",
        "invalid_candidate",
        "incomplete_candidate",
        "new",
        "related",
        "new",
    )

    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        del bounds
        self.calls.append(node)
        if node is WorkflowNode.PRELIMINARY_GATE:
            return NodeResult(survivor_count=8)
        if node is WorkflowNode.CONCEPT_DESIGN:
            return NodeResult(admitted_count=8)
        index = cast(int, state["evaluated_count"])
        if node is WorkflowNode.FINAL_ANALYSIS:
            outcome = self.outcomes[index]
            invalid = outcome in {"invalid_candidate", "incomplete_candidate"}
            return NodeResult(
                evaluated_count=index + 1,
                candidate_ready=not invalid,
                candidate_outcome=outcome if invalid else None,
            )
        if node is WorkflowNode.SIMILARITY_COMMIT:
            outcome = self.outcomes[index - 1]
            counted = outcome in {"new", "related"}
            committed = outcome != "automatic_exact_rediscovery"
            return NodeResult(
                achieved_count=cast(int, state["achieved_count"]) + int(counted),
                committed_count=cast(int, state["committed_count"]) + int(committed),
                candidate_outcome=outcome,
                counters=CounterDelta(opportunities=int(committed)),
            )
        return NodeResult()


class CancelAfterChecks:
    def __init__(self, cancel_on_check: int) -> None:
        self.cancel_on_check = cancel_on_check
        self.checks = 0

    async def requested(self, run_id: UUID) -> bool:
        del run_id
        self.checks += 1
        return self.checks >= self.cancel_on_check


def _bounds() -> ProtectedWorkflowBounds:
    return ProtectedConfig.load().research.bounds


@pytest.mark.anyio
async def test_fixed_graph_commits_five_candidates_sequentially_without_persistence() -> None:
    operations = FiveCommitOperations()
    graph = FixedWorkflowGraph(bounds=_bounds(), operations=operations)

    result = await graph.run(UUID(int=1))

    assert result.status == "completed"
    candidate_route = WORKFLOW_SEQUENCE[8:13]
    expected = (*WORKFLOW_SEQUENCE[:8], *(candidate_route * 5), WorkflowNode.TERMINAL_CLEANUP)
    assert result.trace == tuple(node.value for node in expected)
    assert operations.calls == list(expected)
    assert result.admitted_count == 8
    assert result.evaluated_count == 5
    assert result.achieved_count == 5
    assert result.committed_count == 5
    assert graph.compiled.checkpointer is None
    assert graph.compiled.store is None
    topology = graph.compiled.get_graph()
    assert set(node.value for node in WORKFLOW_SEQUENCE) <= set(topology.nodes)


@pytest.mark.anyio
async def test_gate_with_no_survivor_skips_all_solution_and_validation_nodes() -> None:
    operations = ScriptedOperations(
        {
            WorkflowNode.PRELIMINARY_GATE: NodeResult(
                warnings=("no_gate_survivor",), survivor_count=0
            )
        }
    )
    graph = FixedWorkflowGraph(bounds=_bounds(), operations=operations)

    result = await graph.run(UUID(int=2))

    assert result.status == "completed_with_warnings"
    assert result.trace[-1] == WorkflowNode.TERMINAL_CLEANUP.value
    assert WorkflowNode.CONCEPT_DESIGN not in operations.calls
    assert WorkflowNode.FINAL_ANALYSIS not in operations.calls
    assert WorkflowNode.SIMILARITY_COMMIT not in operations.calls


@pytest.mark.anyio
async def test_non_counting_outcomes_exhaust_the_bounded_pool_without_filler() -> None:
    result = await FixedWorkflowGraph(bounds=_bounds(), operations=MixedOutcomeOperations()).run(
        UUID(int=11)
    )

    assert result.status == "completed_with_warnings"
    assert result.evaluated_count == 8
    assert result.committed_count == 5
    assert result.achieved_count == 3
    assert result.shortfall_code == "insufficient_evidence"
    assert result.non_counting_outcomes.automatic_exact_rediscovery == 1
    assert result.non_counting_outcomes.updated_version == 1
    assert result.non_counting_outcomes.unresolved_possible_rediscovery == 1
    assert result.non_counting_outcomes.invalid_candidate == 1
    assert result.non_counting_outcomes.incomplete_candidate == 1
    assert result.counters.opportunities == 5


@pytest.mark.anyio
async def test_no_final_candidate_skips_similarity_and_commit() -> None:
    operations = ScriptedOperations(
        {
            WorkflowNode.PRELIMINARY_GATE: NodeResult(survivor_count=1),
            WorkflowNode.CONCEPT_DESIGN: NodeResult(admitted_count=1),
            WorkflowNode.FINAL_ANALYSIS: NodeResult(
                warnings=("final_candidate_discarded",),
                evaluated_count=1,
                candidate_ready=False,
                candidate_outcome="invalid_candidate",
            ),
        }
    )
    graph = FixedWorkflowGraph(bounds=_bounds(), operations=operations)

    result = await graph.run(UUID(int=3))

    assert result.status == "completed_with_warnings"
    assert WorkflowNode.SIMILARITY_COMMIT not in operations.calls
    assert operations.calls[-1] is WorkflowNode.TERMINAL_CLEANUP


@pytest.mark.anyio
async def test_work_ceiling_stops_before_following_node_and_runs_cleanup() -> None:
    operations = ScriptedOperations(
        {
            WorkflowNode.DISCOVERY_PLAN: NodeResult(
                counters=CounterDelta(queries=_bounds().run_queries + 1)
            )
        }
    )
    graph = FixedWorkflowGraph(bounds=_bounds(), operations=operations)

    result = await graph.run(UUID(int=4))

    assert result.status == "stopped"
    assert operations.calls == [
        WorkflowNode.DISCOVERY_PLAN,
        WorkflowNode.TERMINAL_CLEANUP,
    ]
    assert result.counters.queries == 0
    assert result.warnings == ("work_limit_exceeded",)
    assert result.limit_code == "queries"
    assert result.limit_stage == WorkflowNode.DISCOVERY_PLAN.value


@pytest.mark.anyio
async def test_total_token_ceiling_is_enforced_independently_of_call_count() -> None:
    bounds = _bounds()
    operations = ScriptedOperations(
        {
            WorkflowNode.DISCOVERY_PLAN: NodeResult(
                counters=CounterDelta(
                    model_calls=1,
                    input_tokens=bounds.total_run_tokens,
                    output_tokens=1,
                )
            )
        }
    )

    result = await FixedWorkflowGraph(bounds=bounds, operations=operations).run(UUID(int=5))

    assert result.status == "stopped"
    assert result.warnings == ("work_limit_exceeded",)
    assert result.limit_code == "total_tokens"
    assert result.limit_stage == WorkflowNode.DISCOVERY_PLAN.value


@pytest.mark.anyio
async def test_cancellation_is_checked_before_and_after_nodes() -> None:
    operations = ScriptedOperations()
    cancellation = CancelAfterChecks(cancel_on_check=4)
    graph = FixedWorkflowGraph(bounds=_bounds(), operations=operations, cancellation=cancellation)

    result = await graph.run(UUID(int=6))

    assert result.status == "stopped"
    assert operations.calls == [
        WorkflowNode.DISCOVERY_PLAN,
        WorkflowNode.DISCOVERY_SEARCH,
        WorkflowNode.TERMINAL_CLEANUP,
    ]
    assert cancellation.checks == 4


@pytest.mark.anyio
async def test_required_contract_failure_routes_directly_to_failed_cleanup() -> None:
    operations = FailingOperations()
    result = await FixedWorkflowGraph(bounds=_bounds(), operations=operations).run(UUID(int=7))

    assert result.status == "failed"
    assert operations.calls[-1] is WorkflowNode.TERMINAL_CLEANUP
    assert WorkflowNode.SEGMENT_MAPPING not in operations.calls
    assert result.warnings == ("workflow_contract_failure",)


def test_counter_delta_rejects_negative_work() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        CounterDelta(queries=-1)


@pytest.mark.parametrize(
    ("counter", "limit"),
    [
        ("queries", lambda bounds: bounds.run_queries),
        ("search_results", lambda bounds: bounds.run_queries * bounds.results_per_query),
        ("unique_urls", lambda bounds: bounds.unique_urls),
        ("retrieved_pages", lambda bounds: bounds.run_pages),
        ("retrieved_bytes", lambda bounds: bounds.run_bytes),
        ("signals", lambda bounds: bounds.signals),
        ("patterns", lambda bounds: bounds.patterns),
        ("segments", lambda bounds: bounds.patterns * bounds.segments_per_pattern),
        ("preliminary_survivors", lambda bounds: bounds.preliminary_survivors),
        ("concepts", lambda bounds: bounds.concepts),
        ("opportunities", lambda bounds: bounds.concepts),
        ("search_retries", lambda bounds: bounds.run_queries * bounds.search_retries),
        ("model_calls", lambda bounds: bounds.model_calls),
        ("repairs", lambda bounds: bounds.repairs),
    ],
)
def test_every_work_counter_ceiling_is_enforced(
    counter: str, limit: Callable[[ProtectedWorkflowBounds], int]
) -> None:
    bounds = _bounds()
    ceiling = limit(bounds)

    with pytest.raises(WorkLimitExceeded, match=f"protected {counter} ceiling"):
        WorkCounters().increment(CounterDelta(**{counter: ceiling + 1}), bounds)


@pytest.mark.anyio
async def test_dependency_timeout_stops_and_always_runs_cleanup() -> None:
    operations = TimeoutOperations()
    result = await FixedWorkflowGraph(bounds=_bounds(), operations=operations).run(UUID(int=8))

    assert result.status == "stopped"
    assert result.warnings == ("dependency_unavailable",)
    assert operations.calls[-1] is WorkflowNode.TERMINAL_CLEANUP


@pytest.mark.anyio
async def test_unexpected_failure_fails_and_always_runs_cleanup() -> None:
    operations = UnexpectedFailureOperations()
    result = await FixedWorkflowGraph(bounds=_bounds(), operations=operations).run(UUID(int=9))

    assert result.status == "failed"
    assert result.warnings == ("workflow_execution_failed",)
    assert operations.calls[-1] is WorkflowNode.TERMINAL_CLEANUP


def test_node_warnings_are_identifiers_only() -> None:
    with pytest.raises(ValueError, match="identifiers"):
        NodeResult(warnings=("a retrieved title or arbitrary message",))


@pytest.mark.anyio
async def test_later_commit_failure_preserves_earlier_committed_count() -> None:
    operations = PartialCommitOperations()
    result = await FixedWorkflowGraph(bounds=_bounds(), operations=operations).run(UUID(int=10))

    assert result.status == "failed"
    assert result.committed_count == 1
    assert result.warnings == ("opportunity_commit_failed",)
    assert operations.calls[-1] is WorkflowNode.TERMINAL_CLEANUP
