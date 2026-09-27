from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.opportunities import (
    OpportunityAggregateStore,
    OpportunityDetail,
    RecoverySummary,
    RunDetail,
    RunSnapshot,
)
from pensae.opportunities.records import RunCapacity, RunResourceCapacity
from pensae.research.budget import FinalizationBudget, WorkCapacity
from pensae.research.workflow import WorkCounters
from pensae.runs.control import ProgressRecord, ProgressStore, RunManager
from pensae.settings import FutureRunSettingsSnapshot, SavedSettingsService, SavedSettingsStore


@dataclass(frozen=True, slots=True)
class RunStartOutcome:
    run_id: UUID
    state: str
    opportunity_id: UUID | None = None
    warnings: tuple[str, ...] = ()


class ResearchService:
    def __init__(
        self,
        *,
        manager: RunManager,
        progress: ProgressStore,
        store: OpportunityAggregateStore,
        protected: ProtectedConfig,
        close_callback: Callable[[], Awaitable[None]] | None = None,
        recovery_callback: Callable[[], Awaitable[RecoverySummary]] | None = None,
        recovery_complete: bool = False,
        settings_store: SavedSettingsStore | None = None,
    ) -> None:
        self._manager = manager
        self._progress = progress
        self._store = store
        self._protected = protected
        self._close_callback = close_callback
        self._recovery_callback = recovery_callback
        self._recovery_complete = recovery_complete
        self._recovery_lock = asyncio.Lock()
        self._settings_store = settings_store

    @property
    def active_run_id(self) -> UUID | None:
        return self._manager.active_run_id

    async def snapshot_future_settings(self) -> FutureRunSettingsSnapshot:
        """Capture the exact server-owned settings revision used for preflight and creation."""

        return await self._future_settings()

    async def start(
        self, *, settings_snapshot: FutureRunSettingsSnapshot | None = None
    ) -> RunStartOutcome:
        await self._ensure_recovered()
        run_id = uuid4()
        snapshot = self._snapshot(
            run_id,
            settings_snapshot if settings_snapshot is not None else await self._future_settings(),
        )
        await self._manager.start(snapshot)
        return RunStartOutcome(run_id=run_id, state="running")

    async def stop(self, run_id: UUID) -> bool:
        return await self._manager.stop(run_id)

    async def aclose(self) -> None:
        if self._close_callback is not None:
            await self._close_callback()

    async def _ensure_recovered(self) -> None:
        if self._recovery_complete or self._recovery_callback is None:
            return
        async with self._recovery_lock:
            if self._recovery_complete:
                return
            await self._recovery_callback()
            self._recovery_complete = True

    async def get_run(self, run_id: UUID) -> RunDetail | None:
        durable = await self._store.get_run(run_id)
        detail = durable if durable is not None else self._manager.get_transient_terminal(run_id)
        return self._decorate_run(detail) if detail is not None else None

    async def get_opportunity(self, opportunity_id: UUID) -> OpportunityDetail | None:
        return await self._store.get_detail(opportunity_id)

    async def get_opportunity_version(
        self, opportunity_id: UUID, version_id: UUID
    ) -> OpportunityDetail | None:
        return await self._store.get_version_detail(opportunity_id, version_id)

    async def stream_events(
        self,
        run_id: UUID,
        after_id: str | None,
        *,
        poll_seconds: float = 0.25,
    ) -> AsyncIterator[ProgressRecord | None]:
        cursor = after_id
        while True:
            replay = await self._progress.replay(run_id, cursor)
            if replay.requires_snapshot:
                yield None
                return
            snapshot = await self.get_run(run_id)
            for record in replay.records:
                cursor = record.event_id
                capacity = (
                    self._capacity_for(
                        snapshot,
                        counters=record.event.counters,
                        target_count=record.event.target_count,
                        admitted_count=record.event.admitted_count,
                        evaluated_count=record.event.evaluated_count,
                        achieved_count=record.event.achieved_count,
                        state=record.event.state,
                    )
                    if snapshot is not None
                    else None
                )
                yield record.model_copy(
                    update={"event": record.event.model_copy(update={"capacity": capacity})}
                )
            snapshot = await self.get_run(run_id)
            if snapshot is None or snapshot.state in {
                "completed",
                "completed_with_warnings",
                "stopped",
                "failed",
            }:
                return
            await asyncio.sleep(poll_seconds)

    def _decorate_run(self, detail: RunDetail) -> RunDetail:
        validated = RunDetail.model_validate(detail.model_dump(mode="python"))
        capacity = self._capacity_for(
            validated,
            counters=_work_counters(validated.work_counters),
            target_count=validated.target_count,
            admitted_count=validated.admitted_count,
            evaluated_count=validated.evaluated_count,
            achieved_count=validated.achieved_count,
            state=validated.state,
        )
        return validated.model_copy(update={"capacity": capacity})

    def _capacity_for(
        self,
        detail: RunDetail,
        *,
        counters: WorkCounters,
        target_count: int,
        admitted_count: int,
        evaluated_count: int,
        achieved_count: int,
        state: str,
    ) -> RunCapacity | None:
        raw_bounds = detail.effective_config.get("bounds")
        if not isinstance(raw_bounds, dict):
            return None
        try:
            bounds = ProtectedWorkflowBounds(
                **{name: raw_bounds[name] for name in ProtectedWorkflowBounds.__dataclass_fields__}
            )
            budget = FinalizationBudget.from_policy(self._protected.research, bounds)
            if state == "running" and admitted_count == evaluated_count == achieved_count == 0:
                remaining_slots = min(target_count, budget.target)
            else:
                remaining_slots = min(
                    max(target_count - achieved_count, 0),
                    max(admitted_count - evaluated_count, 0),
                    budget.target,
                )
            snapshot = budget.snapshot(
                consumed=WorkCapacity.from_counters(counters),
                remaining_target_slots=remaining_slots,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return RunCapacity(
            available=RunResourceCapacity(**asdict(snapshot.available)),
            reserved=RunResourceCapacity(**asdict(snapshot.reserved)),
            consumed=RunResourceCapacity(**asdict(snapshot.consumed)),
            remaining=RunResourceCapacity(**asdict(snapshot.remaining)),
        )

    async def _future_settings(self) -> FutureRunSettingsSnapshot:
        if self._settings_store is not None:
            return await self._settings_store.snapshot_for_future_run()
        policy = SavedSettingsService()
        return policy.snapshot_for_future_run(policy.initial_value())

    def _snapshot(self, run_id: UUID, settings_snapshot: FutureRunSettingsSnapshot) -> RunSnapshot:
        policy = self._protected.research
        saved = settings_snapshot.values
        default_bounds = asdict(policy.bounds)
        saved_workflow = saved.workflow.model_dump(mode="python")
        effective_bounds = {
            name: saved_workflow.get(name, value) for name, value in default_bounds.items()
        }
        industry = saved.research.focus or "Cross-industry opportunity discovery"
        return RunSnapshot(
            id=run_id,
            effective_config={
                "industry": industry,
                "settings_revision": settings_snapshot.settings_revision,
                "saved_settings": saved.model_dump(mode="json"),
                "chat_model_id": self._protected.policy.chat_model_id,
                "embedding_model_id": self._protected.policy.embedding_model_id,
                "embedding_dimension": self._protected.policy.embedding_dimension,
                "llama_build": self._protected.policy.llama_build,
                "workflow_version": policy.workflow_version,
                "schema_version": policy.schema_version,
                "fingerprint_version": policy.fingerprint_version,
                "model_parameter_version": policy.model_parameter_version,
                "similarity_threshold_version": policy.similarity_threshold_version,
                "related_similarity_threshold": policy.related_similarity_threshold,
                "possible_rediscovery_similarity_threshold": (
                    policy.possible_rediscovery_similarity_threshold
                ),
                "prompt_versions": {
                    "planner": policy.planner_prompt_version,
                    "problem_analyst": policy.problem_analyst_prompt_version,
                    "product_strategist": policy.product_strategist_prompt_version,
                    "opportunity_analyst": policy.opportunity_analyst_prompt_version,
                },
                "bounds": effective_bounds,
                "repair_max": policy.repair_max,
                "temperature": policy.temperature,
                "top_p": policy.top_p,
                "top_k": policy.top_k,
                "min_p": policy.min_p,
                "presence_penalty": policy.presence_penalty,
                "repetition_penalty": policy.repetition_penalty,
                "prompt_input_max_tokens": policy.prompt_input_max_tokens,
                "context_safety_tokens": policy.context_safety_tokens,
                "planner_output_max_tokens": policy.planner_output_max_tokens,
                "problem_analyst_output_max_tokens": (policy.problem_analyst_output_max_tokens),
                "product_strategist_output_max_tokens": (
                    policy.product_strategist_output_max_tokens
                ),
                "opportunity_analyst_output_max_tokens": (
                    policy.opportunity_analyst_output_max_tokens
                ),
                "score_weights": policy.score_weights,
            },
            workflow_version=policy.workflow_version,
            schema_version=policy.schema_version,
        )


def _work_counters(values: dict[str, Any]) -> WorkCounters:
    known = WorkCounters.__dataclass_fields__
    return WorkCounters(
        **{
            name: value
            for name, value in values.items()
            if name in known and isinstance(value, int)
        }
    )
