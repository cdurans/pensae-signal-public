"""Fixed, checkpoint-free LangGraph topology and work accounting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from functools import partial
from typing import Literal, Protocol, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from pensae.config.protected import ProtectedWorkflowBounds

WorkflowStatus = Literal[
    "running",
    "stopping",
    "completed",
    "completed_with_warnings",
    "stopped",
    "failed",
]


class WorkflowNode(StrEnum):
    DISCOVERY_PLAN = "discovery_plan"
    DISCOVERY_SEARCH = "discovery_search"
    DISCOVERY_RETRIEVAL = "discovery_retrieval"
    SIGNAL_EXTRACTION = "signal_extraction"
    PATTERN_SYNTHESIS = "pattern_synthesis"
    SEGMENT_MAPPING = "segment_mapping"
    PRELIMINARY_GATE = "preliminary_gate"
    CONCEPT_DESIGN = "concept_design"
    FOCUSED_PLAN = "focused_plan"
    FOCUSED_SEARCH = "focused_search"
    FOCUSED_RETRIEVAL = "focused_retrieval"
    FINAL_ANALYSIS = "final_analysis"
    SIMILARITY_COMMIT = "similarity_commit"
    TERMINAL_CLEANUP = "terminal_cleanup"


WORKFLOW_SEQUENCE = (
    WorkflowNode.DISCOVERY_PLAN,
    WorkflowNode.DISCOVERY_SEARCH,
    WorkflowNode.DISCOVERY_RETRIEVAL,
    WorkflowNode.SIGNAL_EXTRACTION,
    WorkflowNode.PATTERN_SYNTHESIS,
    WorkflowNode.SEGMENT_MAPPING,
    WorkflowNode.PRELIMINARY_GATE,
    WorkflowNode.CONCEPT_DESIGN,
    WorkflowNode.FOCUSED_PLAN,
    WorkflowNode.FOCUSED_SEARCH,
    WorkflowNode.FOCUSED_RETRIEVAL,
    WorkflowNode.FINAL_ANALYSIS,
    WorkflowNode.SIMILARITY_COMMIT,
    WorkflowNode.TERMINAL_CLEANUP,
)


LimitCode = Literal[
    "queries",
    "search_results",
    "unique_urls",
    "retrieved_pages",
    "retrieved_bytes",
    "source_bytes",
    "signals",
    "patterns",
    "segments",
    "preliminary_survivors",
    "concepts",
    "opportunities",
    "search_retries",
    "model_calls",
    "repairs",
    "total_tokens",
    "role_tokens",
]
CandidateOutcome = Literal[
    "new",
    "related",
    "automatic_exact_rediscovery",
    "updated_version",
    "unresolved_possible_rediscovery",
    "invalid_candidate",
    "incomplete_candidate",
]
ShortfallCode = Literal["bounded_pool_exhausted", "insufficient_evidence"]


class WorkLimitExceeded(RuntimeError):
    """A protected run ceiling would be exceeded, with machine-readable identity."""

    def __init__(
        self,
        message: str,
        *,
        limit: LimitCode | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.limit = limit or _limit_code(message)
        self.stage = stage

    def observed_at(self, stage: str) -> WorkLimitExceeded:
        self.stage = stage
        return self


class WorkflowContractFailure(RuntimeError):
    """Validated configuration, graph, or required-node behavior is invalid."""


class WorkflowStopRequested(RuntimeError):
    """Cooperative stop observed at a protected work boundary."""


class WorkflowDependencyUnavailable(RuntimeError):
    """A required local runtime dependency failed or timed out."""


class WorkflowPartialCommitFailure(RuntimeError):
    """A later commit failed after earlier independent opportunity commits succeeded."""

    def __init__(self, committed_count: int) -> None:
        super().__init__("workflow failed after an earlier opportunity commit")
        if committed_count <= 0:
            raise ValueError("partial commit failure requires an earlier commit")
        self.committed_count = committed_count


@dataclass(frozen=True, slots=True)
class WorkCounters:
    queries: int = 0
    search_results: int = 0
    unique_urls: int = 0
    retrieved_pages: int = 0
    retrieved_bytes: int = 0
    signals: int = 0
    patterns: int = 0
    segments: int = 0
    preliminary_survivors: int = 0
    concepts: int = 0
    opportunities: int = 0
    search_retries: int = 0
    model_calls: int = 0
    repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def increment(self, delta: CounterDelta, bounds: ProtectedWorkflowBounds) -> WorkCounters:
        values = {
            field.name: getattr(self, field.name) + getattr(delta, field.name)
            for field in fields(self)
        }
        if any(value < 0 for value in values.values()):
            raise WorkflowContractFailure("work counters cannot decrease")
        updated = WorkCounters(**values)
        limits = {
            "queries": bounds.run_queries,
            "search_results": bounds.run_queries * bounds.results_per_query,
            "unique_urls": bounds.unique_urls,
            "retrieved_pages": bounds.run_pages,
            "retrieved_bytes": bounds.run_bytes,
            "signals": bounds.signals,
            "patterns": bounds.patterns,
            "segments": bounds.patterns * bounds.segments_per_pattern,
            "preliminary_survivors": bounds.preliminary_survivors,
            "concepts": bounds.concepts,
            # Non-counting Possible rediscoveries remain durable records, so the
            # bounded candidate pool, not the qualifying target, is the ceiling.
            "opportunities": bounds.concepts,
            "search_retries": bounds.run_queries * bounds.search_retries,
            "model_calls": bounds.model_calls,
            "repairs": bounds.repairs,
        }
        for name, limit in limits.items():
            if getattr(updated, name) > limit:
                raise WorkLimitExceeded(f"protected {name} ceiling exceeded")
        if updated.total_tokens > bounds.total_run_tokens:
            raise WorkLimitExceeded("protected total run token ceiling exceeded")
        return updated


@dataclass(frozen=True, slots=True)
class CounterDelta:
    queries: int = 0
    search_results: int = 0
    unique_urls: int = 0
    retrieved_pages: int = 0
    retrieved_bytes: int = 0
    signals: int = 0
    patterns: int = 0
    segments: int = 0
    preliminary_survivors: int = 0
    concepts: int = 0
    opportunities: int = 0
    search_retries: int = 0
    model_calls: int = 0
    repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if any(getattr(self, field.name) < 0 for field in fields(self)):
            raise ValueError("counter increments must be nonnegative")


@dataclass(frozen=True, slots=True)
class NonCountingOutcomes:
    automatic_exact_rediscovery: int = 0
    updated_version: int = 0
    unresolved_possible_rediscovery: int = 0
    invalid_candidate: int = 0
    incomplete_candidate: int = 0

    def __post_init__(self) -> None:
        if any(getattr(self, item.name) < 0 for item in fields(self)):
            raise ValueError("non-counting outcome counts must be nonnegative")

    def add(self, outcome: CandidateOutcome) -> NonCountingOutcomes:
        if outcome in {"new", "related"}:
            return self
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        values[outcome] += 1
        return NonCountingOutcomes(**values)


class ModelCallUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=240)
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    call_index: int = Field(ge=1)
    attempts: Literal[1] = 1
    repair: bool = False
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class NodeResult:
    counters: CounterDelta = CounterDelta()
    warnings: tuple[str, ...] = ()
    survivor_count: int | None = None
    admitted_count: int | None = None
    evaluated_count: int | None = None
    achieved_count: int | None = None
    committed_count: int | None = None
    candidate_ready: bool | None = None
    candidate_outcome: CandidateOutcome | None = None
    shortfall_code: ShortfallCode | None = None
    limit_code: LimitCode | None = None
    limit_stage: str | None = None
    model_usage: tuple[ModelCallUsage, ...] = ()

    def __post_init__(self) -> None:
        if any(not _is_warning_code(code) for code in self.warnings):
            raise ValueError("workflow warnings must be bounded identifiers")


class WorkflowState(TypedDict):
    run_id: UUID
    status: WorkflowStatus
    stage: str
    counters: WorkCounters
    warnings: tuple[str, ...]
    survivor_count: int
    target_count: int
    admitted_count: int
    evaluated_count: int
    achieved_count: int
    committed_count: int
    candidate_ready: bool
    non_counting_outcomes: NonCountingOutcomes
    shortfall_code: ShortfallCode | None
    limit_code: LimitCode | None
    limit_stage: str | None
    trace: tuple[str, ...]
    model_usage: tuple[ModelCallUsage, ...]


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    run_id: UUID
    status: WorkflowStatus
    counters: WorkCounters
    warnings: tuple[str, ...]
    survivor_count: int
    target_count: int
    admitted_count: int
    evaluated_count: int
    achieved_count: int
    committed_count: int
    non_counting_outcomes: NonCountingOutcomes
    shortfall_code: ShortfallCode | None
    limit_code: LimitCode | None
    limit_stage: str | None
    trace: tuple[str, ...]
    model_usage: tuple[ModelCallUsage, ...] = ()


class WorkflowOperations(Protocol):
    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult: ...


class CancellationProbe(Protocol):
    async def requested(self, run_id: UUID) -> bool: ...


class WorkflowObserver(Protocol):
    async def stage_started(self, state: Mapping[str, object], node: WorkflowNode) -> None: ...

    async def stage_completed(
        self, state: Mapping[str, object], node: WorkflowNode, result: NodeResult
    ) -> None: ...


class NeverCancelled:
    async def requested(self, run_id: UUID) -> bool:
        del run_id
        return False


class NullWorkflowObserver:
    async def stage_started(self, state: Mapping[str, object], node: WorkflowNode) -> None:
        del state, node

    async def stage_completed(
        self, state: Mapping[str, object], node: WorkflowNode, result: NodeResult
    ) -> None:
        del state, node, result


class FixedWorkflowGraph:
    """One checkpoint-free graph with a fixed, bounded sequential candidate cycle."""

    def __init__(
        self,
        *,
        bounds: ProtectedWorkflowBounds,
        operations: WorkflowOperations,
        cancellation: CancellationProbe | None = None,
        observer: WorkflowObserver | None = None,
    ) -> None:
        self._bounds = bounds
        self._operations = operations
        self._cancellation = cancellation or NeverCancelled()
        self._observer = observer or NullWorkflowObserver()
        builder = StateGraph(WorkflowState)
        for node in WORKFLOW_SEQUENCE:
            builder.add_node(node.value, partial(self._execute_node, node))
        builder.add_edge(START, WorkflowNode.DISCOVERY_PLAN.value)
        upstream = WORKFLOW_SEQUENCE[: WORKFLOW_SEQUENCE.index(WorkflowNode.PRELIMINARY_GATE)]
        for index, node in enumerate(upstream):
            builder.add_conditional_edges(
                node.value,
                self._route_running,
                {
                    "continue": WORKFLOW_SEQUENCE[index + 1].value,
                    "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
                },
            )
        builder.add_conditional_edges(
            WorkflowNode.PRELIMINARY_GATE.value,
            self._route_after_preliminary_gate,
            {
                "concept": WorkflowNode.CONCEPT_DESIGN.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_conditional_edges(
            WorkflowNode.CONCEPT_DESIGN.value,
            self._route_after_concept_design,
            {
                "candidate": WorkflowNode.FOCUSED_PLAN.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_conditional_edges(
            WorkflowNode.FOCUSED_PLAN.value,
            self._route_running,
            {
                "continue": WorkflowNode.FOCUSED_SEARCH.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_conditional_edges(
            WorkflowNode.FOCUSED_SEARCH.value,
            self._route_running,
            {
                "continue": WorkflowNode.FOCUSED_RETRIEVAL.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_conditional_edges(
            WorkflowNode.FOCUSED_RETRIEVAL.value,
            self._route_running,
            {
                "continue": WorkflowNode.FINAL_ANALYSIS.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_conditional_edges(
            WorkflowNode.FINAL_ANALYSIS.value,
            self._route_after_final_analysis,
            {
                "commit": WorkflowNode.SIMILARITY_COMMIT.value,
                "candidate": WorkflowNode.FOCUSED_PLAN.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_conditional_edges(
            WorkflowNode.SIMILARITY_COMMIT.value,
            self._route_after_commit,
            {
                "candidate": WorkflowNode.FOCUSED_PLAN.value,
                "cleanup": WorkflowNode.TERMINAL_CLEANUP.value,
            },
        )
        builder.add_edge(WorkflowNode.TERMINAL_CLEANUP.value, END)
        self.compiled = builder.compile(name="pensae-fixed-research-workflow")

    async def run(self, run_id: UUID) -> WorkflowResult:
        initial = WorkflowState(
            run_id=run_id,
            status="running",
            stage="created",
            counters=WorkCounters(),
            warnings=(),
            survivor_count=0,
            target_count=self._bounds.opportunities,
            admitted_count=0,
            evaluated_count=0,
            achieved_count=0,
            committed_count=0,
            candidate_ready=False,
            non_counting_outcomes=NonCountingOutcomes(),
            shortfall_code=None,
            limit_code=None,
            limit_stage=None,
            trace=(),
            model_usage=(),
        )
        raw = await self.compiled.ainvoke(initial)
        state = cast(WorkflowState, raw)
        return WorkflowResult(
            run_id=state["run_id"],
            status=state["status"],
            counters=state["counters"],
            warnings=state["warnings"],
            survivor_count=state["survivor_count"],
            target_count=state["target_count"],
            admitted_count=state["admitted_count"],
            evaluated_count=state["evaluated_count"],
            achieved_count=state["achieved_count"],
            committed_count=state["committed_count"],
            non_counting_outcomes=state["non_counting_outcomes"],
            shortfall_code=state["shortfall_code"],
            limit_code=state["limit_code"],
            limit_stage=state["limit_stage"],
            trace=state["trace"],
            model_usage=state["model_usage"],
        )

    async def _execute_node(self, node: WorkflowNode, state: WorkflowState) -> dict[str, object]:
        trace = (*state["trace"], node.value)
        operation_counters_before = self._operation_counters()
        operation_usage_before = self._operation_usage()
        if node is not WorkflowNode.TERMINAL_CLEANUP and (
            state["status"] != "running" or await self._cancellation.requested(state["run_id"])
        ):
            return {"status": "stopping", "stage": node.value, "trace": trace}
        try:
            if node is not WorkflowNode.TERMINAL_CLEANUP:
                await self._observer.stage_started(state, node)
            result = await self._operations.run_node(node, state, self._bounds)
            counters = state["counters"].increment(result.counters, self._bounds)
        except (WorkLimitExceeded, WorkflowStopRequested) as exc:
            limit_update: dict[str, object] = {}
            if isinstance(exc, WorkLimitExceeded):
                if exc.stage is None:
                    exc.observed_at(node.value)
                limit_update = {"limit_code": exc.limit, "limit_stage": exc.stage}
            return {
                **self._partial_operation_update(
                    state, operation_counters_before, operation_usage_before
                ),
                "status": "stopping",
                "stage": node.value,
                "warnings": (*state["warnings"], _warning_code(exc)),
                "trace": trace,
                **limit_update,
            }
        except (TimeoutError, WorkflowDependencyUnavailable):
            return {
                **self._partial_operation_update(
                    state, operation_counters_before, operation_usage_before
                ),
                "status": "stopping",
                "stage": node.value,
                "warnings": (*state["warnings"], "dependency_unavailable"),
                "trace": trace,
            }
        except WorkflowContractFailure:
            return {
                **self._partial_operation_update(
                    state, operation_counters_before, operation_usage_before
                ),
                "status": "failed",
                "stage": node.value,
                "warnings": (*state["warnings"], "workflow_contract_failure"),
                "trace": trace,
            }
        except WorkflowPartialCommitFailure as exc:
            return {
                **self._partial_operation_update(
                    state, operation_counters_before, operation_usage_before
                ),
                "status": "failed",
                "stage": node.value,
                "warnings": (*state["warnings"], "opportunity_commit_failed"),
                "committed_count": exc.committed_count,
                "trace": trace,
            }
        except Exception:
            return {
                **self._partial_operation_update(
                    state, operation_counters_before, operation_usage_before
                ),
                "status": "failed",
                "stage": node.value,
                "warnings": (*state["warnings"], "workflow_execution_failed"),
                "trace": trace,
            }
        operation_counters_after = self._operation_counters()
        if operation_counters_before is not None and operation_counters_after is not None:
            counters = state["counters"].increment(
                _counter_difference(operation_counters_before, operation_counters_after),
                self._bounds,
            )
        operation_usage_after = self._operation_usage()
        added_usage = (
            operation_usage_after[len(operation_usage_before) :]
            if operation_usage_before is not None and operation_usage_after is not None
            else result.model_usage
        )
        update: dict[str, object] = {
            "stage": node.value,
            "counters": counters,
            "warnings": (*state["warnings"], *result.warnings),
            "trace": trace,
            "model_usage": (*state["model_usage"], *added_usage),
        }
        for key in (
            "survivor_count",
            "admitted_count",
            "evaluated_count",
            "achieved_count",
            "committed_count",
        ):
            value = getattr(result, key)
            if value is not None:
                if value < 0:
                    update["status"] = "failed"
                    update["warnings"] = (
                        *cast(tuple[str, ...], update["warnings"]),
                        "workflow_contract_failure",
                    )
                    break
                update[key] = value
        if result.candidate_ready is not None:
            update["candidate_ready"] = result.candidate_ready
        if result.candidate_outcome is not None:
            update["non_counting_outcomes"] = state["non_counting_outcomes"].add(
                result.candidate_outcome
            )
        if result.shortfall_code is not None:
            update["shortfall_code"] = result.shortfall_code
        if result.limit_code is not None:
            update["limit_code"] = result.limit_code
        if result.limit_stage is not None:
            update["limit_stage"] = result.limit_stage
        if node is not WorkflowNode.TERMINAL_CLEANUP:
            try:
                await self._observer.stage_completed({**state, **update}, node, result)
            except WorkflowStopRequested:
                update["status"] = "stopping"
                update["warnings"] = (
                    *cast(tuple[str, ...], update["warnings"]),
                    "redis_unavailable",
                )
            except Exception:
                update["status"] = "failed"
                update["warnings"] = (
                    *cast(tuple[str, ...], update["warnings"]),
                    "progress_snapshot_failed",
                )
        if node is WorkflowNode.TERMINAL_CLEANUP:
            prior_status = state["status"]
            if prior_status == "running" and state["achieved_count"] < state["target_count"]:
                shortfall = _shortfall_code(state)
                update["shortfall_code"] = shortfall
                update["warnings"] = (
                    *cast(tuple[str, ...], update["warnings"]),
                    "opportunity_target_shortfall",
                )
            if prior_status == "stopping":
                update["status"] = "stopped"
            elif prior_status == "failed":
                update["status"] = "failed"
            elif update["warnings"]:
                update["status"] = "completed_with_warnings"
            else:
                update["status"] = "completed"
        elif await self._cancellation.requested(state["run_id"]):
            update["status"] = "stopping"
        return update

    def _operation_counters(self) -> WorkCounters | None:
        snapshot = getattr(self._operations, "accounting_snapshot", None)
        if snapshot is None:
            return None
        value = snapshot()
        if not isinstance(value, WorkCounters):
            raise WorkflowContractFailure("operation accounting snapshot is invalid")
        return value

    def _operation_usage(self) -> tuple[ModelCallUsage, ...] | None:
        snapshot = getattr(self._operations, "model_usage_snapshot", None)
        if snapshot is None:
            return None
        value = snapshot()
        if not isinstance(value, tuple) or not all(
            isinstance(item, ModelCallUsage) for item in value
        ):
            raise WorkflowContractFailure("operation model usage snapshot is invalid")
        return value

    def _partial_operation_update(
        self,
        state: WorkflowState,
        counters_before: WorkCounters | None,
        usage_before: tuple[ModelCallUsage, ...] | None,
    ) -> dict[str, object]:
        update: dict[str, object] = {}
        counters_after = self._operation_counters()
        if counters_before is not None and counters_after is not None:
            update["counters"] = state["counters"].increment(
                _counter_difference(counters_before, counters_after), self._bounds
            )
        usage_after = self._operation_usage()
        if usage_before is not None and usage_after is not None:
            update["model_usage"] = (*state["model_usage"], *usage_after[len(usage_before) :])
        return update

    @staticmethod
    async def _route_running(state: WorkflowState) -> str:
        if state["status"] != "running":
            return "cleanup"
        return "continue"

    @staticmethod
    async def _route_after_preliminary_gate(state: WorkflowState) -> str:
        if state["status"] != "running" or state["survivor_count"] == 0:
            return "cleanup"
        return "concept"

    @staticmethod
    async def _route_after_concept_design(state: WorkflowState) -> str:
        if state["status"] != "running" or state["admitted_count"] == 0:
            return "cleanup"
        return "candidate"

    @staticmethod
    async def _route_after_final_analysis(state: WorkflowState) -> str:
        if state["status"] != "running":
            return "cleanup"
        if state["candidate_ready"]:
            return "commit"
        if state["evaluated_count"] < state["admitted_count"]:
            return "candidate"
        return "cleanup"

    @staticmethod
    async def _route_after_commit(state: WorkflowState) -> str:
        if state["status"] != "running" or state["achieved_count"] >= state["target_count"]:
            return "cleanup"
        if state["evaluated_count"] < state["admitted_count"]:
            return "candidate"
        return "cleanup"


def _warning_code(error: Exception) -> str:
    if isinstance(error, WorkLimitExceeded):
        return "work_limit_exceeded"
    return "stop_requested"


def _counter_difference(before: WorkCounters, after: WorkCounters) -> CounterDelta:
    values = {
        item.name: getattr(after, item.name) - getattr(before, item.name)
        for item in fields(WorkCounters)
    }
    return CounterDelta(**values)


def _is_warning_code(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 64
        and value.isascii()
        and all(
            character.islower() or character.isdigit() or character == "_" for character in value
        )
    )


def _shortfall_code(state: WorkflowState) -> ShortfallCode:
    outcomes = state["non_counting_outcomes"]
    if (
        state["admitted_count"] < state["target_count"]
        or outcomes.invalid_candidate
        or outcomes.incomplete_candidate
    ):
        return "insufficient_evidence"
    return "bounded_pool_exhausted"


def _limit_code(message: str) -> LimitCode:
    normalized = message.casefold()
    aliases: tuple[tuple[str, LimitCode], ...] = (
        ("unique_urls", "unique_urls"),
        ("retrieved_pages", "retrieved_pages"),
        ("retrieved_bytes", "retrieved_bytes"),
        ("source byte", "source_bytes"),
        ("run byte", "retrieved_bytes"),
        ("search_results", "search_results"),
        ("search_retries", "search_retries"),
        ("preliminary_survivors", "preliminary_survivors"),
        ("model_calls", "model_calls"),
        ("model call", "model_calls"),
        ("total_tokens", "total_tokens"),
        ("repair", "repairs"),
        ("total run token", "total_tokens"),
        ("structured role token", "role_tokens"),
        ("queries", "queries"),
        ("signals", "signals"),
        ("patterns", "patterns"),
        ("segments", "segments"),
        ("concepts", "concepts"),
        ("opportunities", "opportunities"),
    )
    for marker, code in aliases:
        if marker in normalized:
            return code
    raise ValueError(f"work limit message has no typed identifier: {message}")
