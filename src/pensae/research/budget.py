"""Deterministic workflow feasibility and remaining-slot reservation policy."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Literal, Protocol

from pensae.config.protected import ProtectedResearchPolicy, ProtectedWorkflowBounds
from pensae.research.workflow import LimitCode, WorkCounters, WorkLimitExceeded

_WORKFLOW_LIMIT: dict[WorkDimension, LimitCode]

CALIBRATED_PROMPT_INPUT_MAX_TOKENS = 8_192
CALIBRATED_DEFAULT_REPAIRS = 8


class SavedWorkflowBudget(Protocol):
    discovery_queries: int
    first_pass_pages: int
    run_queries: int
    run_pages: int
    patterns: int
    segments_per_pattern: int
    preliminary_survivors: int
    concepts: int
    focused_queries_per_concept: int
    focused_pages_per_concept: int
    opportunities: Literal[5]
    model_calls: int
    total_run_tokens: int
    planner_output_max_tokens: int
    problem_analyst_output_max_tokens: int
    product_strategist_output_max_tokens: int
    opportunity_analyst_output_max_tokens: int


class WorkDimension(StrEnum):
    """Closed identifiers for the independently bounded run resources."""

    QUERIES = "queries"
    PAGES = "pages"
    BYTES = "bytes"
    MODEL_CALLS = "model_calls"
    REPAIRS = "repairs"
    TOTAL_TOKENS = "total_tokens"


_WORKFLOW_LIMIT = {
    WorkDimension.QUERIES: "queries",
    WorkDimension.PAGES: "retrieved_pages",
    WorkDimension.BYTES: "retrieved_bytes",
    WorkDimension.MODEL_CALLS: "model_calls",
    WorkDimension.REPAIRS: "repairs",
    WorkDimension.TOTAL_TOKENS: "total_tokens",
}


@dataclass(frozen=True, slots=True)
class WorkCapacity:
    """One closed, serializable view of bounded run capacity."""

    queries: int = 0
    pages: int = 0
    bytes: int = 0
    model_calls: int = 0
    repairs: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if any(getattr(self, item.name) < 0 for item in fields(self)):
            raise ValueError("work capacity values must be nonnegative")

    def __add__(self, other: WorkCapacity) -> WorkCapacity:
        return WorkCapacity(
            **{
                item.name: getattr(self, item.name) + getattr(other, item.name)
                for item in fields(self)
            }
        )

    def scaled(self, multiplier: int) -> WorkCapacity:
        if multiplier < 0:
            raise ValueError("work capacity multiplier must be nonnegative")
        return WorkCapacity(
            **{item.name: getattr(self, item.name) * multiplier for item in fields(self)}
        )

    def value(self, dimension: WorkDimension) -> int:
        return getattr(self, dimension.value)

    def remaining_after(self, consumed: WorkCapacity) -> WorkCapacity:
        values = {
            item.name: max(getattr(self, item.name) - getattr(consumed, item.name), 0)
            for item in fields(self)
        }
        return WorkCapacity(**values)

    @classmethod
    def from_bounds(cls, bounds: ProtectedWorkflowBounds) -> WorkCapacity:
        return cls(
            queries=bounds.run_queries,
            pages=bounds.run_pages,
            bytes=bounds.run_bytes,
            model_calls=bounds.model_calls,
            repairs=bounds.repairs,
            total_tokens=bounds.total_run_tokens,
        )

    @classmethod
    def from_counters(cls, counters: WorkCounters) -> WorkCapacity:
        return cls(
            queries=counters.queries,
            pages=counters.retrieved_pages,
            bytes=counters.retrieved_bytes,
            model_calls=counters.model_calls,
            repairs=counters.repairs,
            total_tokens=counters.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Capacity partition at one deterministic work boundary."""

    capacity: WorkCapacity
    consumed: WorkCapacity
    reserved: WorkCapacity
    remaining: WorkCapacity
    available: WorkCapacity
    remaining_target_slots: int


class BudgetLimitExceeded(WorkLimitExceeded):
    """A typed limit failure that preserves dimension and stage separately."""

    def __init__(
        self,
        *,
        dimension: WorkDimension,
        stage: str,
        required: int,
        available: int,
        reserved: int,
    ) -> None:
        if not stage:
            raise ValueError("budget limit stage must be non-empty")
        self.dimension = dimension
        self.stage = stage
        self.required = required
        self.available = available
        self.reserved = reserved
        super().__init__(
            f"protected {dimension.value} ceiling reached during {stage}",
            limit=_WORKFLOW_LIMIT[dimension],
            stage=stage,
        )


@dataclass(frozen=True, slots=True)
class FinalizationBudget:
    """Protected target policy and the deterministic capacity reserved per slot."""

    target: int
    capacity: WorkCapacity
    per_slot: WorkCapacity
    upstream_floor: WorkCapacity
    normal_slot: WorkCapacity
    repair_cost: WorkCapacity
    upstream_normal: WorkCapacity
    maximum_repairs: int
    configured_concepts: int

    @classmethod
    def from_policy(
        cls,
        policy: ProtectedResearchPolicy,
        bounds: ProtectedWorkflowBounds | None = None,
    ) -> FinalizationBudget:
        bounds = bounds or policy.bounds
        # A slot covers the complete normal admission path: focused plan, one evidence-selection
        # call, final analysis, embedding, and the single permitted repair. Extra evidence pages
        # remain bounded optional work; query/page/byte capacity still reserves their full maxima.
        largest_repair_tokens = policy.prompt_input_max_tokens + max(
            policy.planner_output_max_tokens,
            policy.problem_analyst_output_max_tokens,
            policy.opportunity_analyst_output_max_tokens,
        )
        normal_slot = WorkCapacity(
            queries=bounds.focused_queries_per_concept,
            pages=bounds.focused_pages_per_concept,
            bytes=bounds.focused_pages_per_concept * bounds.source_bytes,
            model_calls=4,
            total_tokens=(
                policy.prompt_input_max_tokens
                + policy.planner_output_max_tokens
                + policy.prompt_input_max_tokens
                + policy.problem_analyst_output_max_tokens
                + policy.prompt_input_max_tokens
                + policy.opportunity_analyst_output_max_tokens
                + policy.prompt_input_max_tokens
            ),
        )
        repair_cost = WorkCapacity(model_calls=1, repairs=1, total_tokens=largest_repair_tokens)
        per_slot = normal_slot + repair_cost
        if bounds.repairs < bounds.opportunities:
            raise ValueError("repairs cannot be fewer than the protected target slots")
        upstream_repairs = bounds.repairs - bounds.opportunities
        upstream_normal_calls = bounds.first_pass_pages + bounds.concepts + 2 + 3 * bounds.patterns
        # The upstream floor covers the complete configured topology, rather than assuming that
        # the first two patterns happen to yield five acceptable candidates. Each configured first-
        # pass page, pattern synthesis/embedding/map triple, and concept design is admitted. The
        # additional per-pattern Problem Analyst allowance conservatively covers tokenizer drift in
        # the pattern embedding input while the embedding call itself is also capped independently.
        upstream_normal_tokens = (
            upstream_normal_calls * policy.prompt_input_max_tokens
            + policy.planner_output_max_tokens
            + bounds.first_pass_pages * policy.problem_analyst_output_max_tokens
            + bounds.patterns
            * (
                2 * policy.problem_analyst_output_max_tokens
                + policy.product_strategist_output_max_tokens
            )
            + bounds.concepts * policy.product_strategist_output_max_tokens
        )
        upstream_normal = WorkCapacity(
            queries=bounds.discovery_queries,
            pages=bounds.first_pass_pages,
            bytes=bounds.first_pass_pages * bounds.source_bytes,
            model_calls=upstream_normal_calls,
            total_tokens=upstream_normal_tokens,
        )
        upstream_floor = upstream_normal + repair_cost.scaled(upstream_repairs)
        return cls(
            target=bounds.opportunities,
            capacity=WorkCapacity.from_bounds(bounds),
            per_slot=per_slot,
            upstream_floor=upstream_floor,
            normal_slot=normal_slot,
            repair_cost=repair_cost,
            upstream_normal=upstream_normal,
            maximum_repairs=bounds.repairs,
            configured_concepts=bounds.concepts,
        )

    def validate_feasibility(self) -> None:
        required = self.maximum_cost_for_evaluations(self.target)
        for dimension in WorkDimension:
            if required.value(dimension) > self.capacity.value(dimension):
                raise ValueError(
                    "protected workflow is cross-field infeasible: "
                    f"{dimension.value} requires {required.value(dimension)} but "
                    f"only {self.capacity.value(dimension)} is configured"
                )

    def reserve_for_slots(self, slots: int) -> WorkCapacity:
        if not 0 <= slots <= self.target:
            raise ValueError("remaining target slots are outside the protected target")
        return self.per_slot.scaled(slots)

    def maximum_cost_for_evaluations(self, evaluations: int) -> WorkCapacity:
        """Full configured upstream plus bounded candidates and the run-wide repair maximum."""

        if not 0 <= evaluations <= self.configured_concepts:
            raise ValueError("candidate evaluations are outside the configured concept pool")
        return (
            self.upstream_normal
            + self.normal_slot.scaled(evaluations)
            + self.repair_cost.scaled(self.maximum_repairs)
        )

    def snapshot(
        self,
        *,
        consumed: WorkCapacity,
        remaining_target_slots: int,
    ) -> BudgetSnapshot:
        reserved = self.reserve_for_slots(remaining_target_slots)
        remaining = self.capacity.remaining_after(consumed)
        available = remaining.remaining_after(reserved)
        return BudgetSnapshot(
            capacity=self.capacity,
            consumed=consumed,
            reserved=reserved,
            remaining=remaining,
            available=available,
            remaining_target_slots=remaining_target_slots,
        )

    def admit(
        self,
        *,
        consumed: WorkCapacity,
        request: WorkCapacity,
        remaining_target_slots: int,
        stage: str,
    ) -> BudgetSnapshot:
        snapshot = self.snapshot(
            consumed=consumed,
            remaining_target_slots=remaining_target_slots,
        )
        for dimension in WorkDimension:
            required = request.value(dimension)
            available = snapshot.available.value(dimension)
            if required > available:
                raise BudgetLimitExceeded(
                    dimension=dimension,
                    stage=stage,
                    required=required,
                    available=available,
                    reserved=snapshot.reserved.value(dimension),
                )
        return snapshot


def validate_protected_budget(policy: ProtectedResearchPolicy) -> FinalizationBudget:
    budget = FinalizationBudget.from_policy(policy)
    if budget.target != 5:
        raise ValueError("protected opportunity target must be exactly five")
    if policy.prompt_input_max_tokens != CALIBRATED_PROMPT_INPUT_MAX_TOKENS:
        raise ValueError(
            "protected prompt input maximum must match the calibrated 8192-token workflow cap"
        )
    if policy.bounds.preliminary_survivors < policy.bounds.concepts:
        raise ValueError("preliminary survivors cannot be fewer than concepts")
    if policy.bounds.concepts < budget.target:
        raise ValueError("concepts cannot be fewer than the protected opportunity target")
    if policy.bounds.patterns * policy.bounds.segments_per_pattern < budget.target:
        raise ValueError(
            "patterns and segments per pattern cannot yield the protected opportunity target"
        )
    budget.validate_feasibility()
    return budget


def validate_saved_workflow_budget(workflow: SavedWorkflowBudget) -> None:
    """Reject a future-run value that cannot preserve the protected five-slot envelope."""

    target = workflow.opportunities
    if target != 5:
        raise ValueError("final opportunity target must remain protected at five")
    if workflow.preliminary_survivors < workflow.concepts:
        raise ValueError("preliminary survivors cannot be fewer than solution concepts")
    if workflow.concepts < target:
        raise ValueError("solution concepts cannot be fewer than the protected target")
    if workflow.patterns * workflow.segments_per_pattern < target:
        raise ValueError("patterns and segments per pattern cannot yield the protected target")
    required_queries = workflow.discovery_queries + target * workflow.focused_queries_per_concept
    if workflow.run_queries < required_queries:
        raise ValueError(
            f"total run queries must be at least {required_queries} for the protected target"
        )
    required_pages = workflow.first_pass_pages + target * workflow.focused_pages_per_concept
    if workflow.run_pages < required_pages:
        raise ValueError(
            f"total run pages must be at least {required_pages} for the protected target"
        )
    if target > CALIBRATED_DEFAULT_REPAIRS:
        raise ValueError("configured repair capacity cannot preserve the protected target")
    upstream_normal_calls = (
        workflow.first_pass_pages + workflow.concepts + 2 + 3 * workflow.patterns
    )
    required_calls = upstream_normal_calls + target * 4 + CALIBRATED_DEFAULT_REPAIRS
    if workflow.model_calls < required_calls:
        raise ValueError(
            f"total model calls must be at least {required_calls} for the protected target"
        )
    prompt_input_max_tokens = CALIBRATED_PROMPT_INPUT_MAX_TOKENS
    per_slot_tokens = (
        5 * prompt_input_max_tokens
        + workflow.planner_output_max_tokens
        + workflow.problem_analyst_output_max_tokens
        + 2 * workflow.opportunity_analyst_output_max_tokens
    )
    upstream_tokens = (
        upstream_normal_calls * prompt_input_max_tokens
        + workflow.planner_output_max_tokens
        + workflow.first_pass_pages * workflow.problem_analyst_output_max_tokens
        + workflow.patterns
        * (
            2 * workflow.problem_analyst_output_max_tokens
            + workflow.product_strategist_output_max_tokens
        )
        + workflow.concepts * workflow.product_strategist_output_max_tokens
    )
    upstream_repairs = CALIBRATED_DEFAULT_REPAIRS - target
    largest_repair_tokens = prompt_input_max_tokens + max(
        workflow.planner_output_max_tokens,
        workflow.problem_analyst_output_max_tokens,
        workflow.product_strategist_output_max_tokens,
        workflow.opportunity_analyst_output_max_tokens,
    )
    required_tokens = (
        target * per_slot_tokens + upstream_tokens + upstream_repairs * largest_repair_tokens
    )
    if workflow.total_run_tokens < required_tokens:
        raise ValueError(
            f"total run tokens must be at least {required_tokens} for the protected target"
        )


__all__ = [
    "CALIBRATED_DEFAULT_REPAIRS",
    "CALIBRATED_PROMPT_INPUT_MAX_TOKENS",
    "BudgetLimitExceeded",
    "BudgetSnapshot",
    "FinalizationBudget",
    "WorkCapacity",
    "WorkDimension",
    "validate_protected_budget",
    "validate_saved_workflow_budget",
]
