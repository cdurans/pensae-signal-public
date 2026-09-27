from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from pensae.opportunities import RunDetail, RunSnapshot
from pensae.research.workflow import (
    ModelCallUsage,
    NodeResult,
    NonCountingOutcomes,
    WorkCounters,
    WorkflowNode,
    WorkflowResult,
    WorkflowStatus,
)
from pensae.runs.control import (
    ActiveRunError,
    CancellationController,
    ProgressEvent,
    ProgressRecord,
    ProgressReplay,
    ProgressUnavailable,
    RunManager,
    RunProgressObserver,
    progress_payload_contains_prohibited_key,
)


class MemoryProgress:
    def __init__(self) -> None:
        self.cancelled: set[UUID] = set()
        self.events: list[ProgressRecord] = []
        self.available = True
        self.fail_publish = False

    async def initialize(self, run_id: UUID) -> None:
        if not self.available:
            raise ProgressUnavailable("redis unavailable")
        self.cancelled.discard(run_id)

    async def publish(self, event: ProgressEvent) -> str:
        if not self.available or self.fail_publish:
            raise ProgressUnavailable("redis unavailable")
        event_id = f"{len(self.events) + 1}-0"
        self.events.append(ProgressRecord(event_id=event_id, event=event))
        return event_id

    async def request_stop(self, run_id: UUID) -> None:
        if not self.available:
            raise ProgressUnavailable("redis unavailable")
        self.cancelled.add(run_id)

    async def is_stop_requested(self, run_id: UUID) -> bool:
        if not self.available:
            raise ProgressUnavailable("redis unavailable")
        return run_id in self.cancelled

    async def replay(self, run_id: UUID, after_id: str | None) -> ProgressReplay:
        records = tuple(item for item in self.events if item.event.run_id == run_id)
        if after_id is None:
            return ProgressReplay(records, False)
        indexes = [index for index, item in enumerate(records) if item.event_id == after_id]
        return ProgressReplay(records[indexes[0] + 1 :] if indexes else (), not indexes)


class MemoryRunStateStore:
    def __init__(self) -> None:
        self.snapshots: list[RunSnapshot] = []
        self.details: dict[UUID, RunDetail] = {}
        self.states: list[tuple[UUID, str]] = []
        self.deleted: list[UUID] = []

    async def create_run(self, snapshot: RunSnapshot) -> UUID:
        self.snapshots.append(snapshot)
        created_at = datetime(2026, 7, 22, tzinfo=UTC)
        self.details[snapshot.id] = RunDetail(
            id=snapshot.id,
            state="running",
            opportunity_id=None,
            effective_config=snapshot.effective_config,
            workflow_version=snapshot.workflow_version,
            schema_version=snapshot.schema_version,
            created_at=created_at,
            updated_at=created_at,
        )
        return snapshot.id

    async def set_run_state(self, run_id: UUID, state: WorkflowStatus) -> None:
        self.states.append((run_id, state))
        self.details[run_id] = self.details[run_id].model_copy(update={"state": state})

    async def delete_empty_run(self, run_id: UUID) -> bool:
        self.deleted.append(run_id)
        return self.details.pop(run_id, None) is not None

    async def get_run(self, run_id: UUID) -> RunDetail | None:
        return self.details.get(run_id)

    async def update_run_progress(
        self,
        run_id: UUID,
        *,
        state: WorkflowStatus,
        stage: str,
        counters: Mapping[str, int],
        warning_codes: tuple[str, ...],
        committed_count: int,
        model_usage: tuple[object, ...] = (),
    ) -> None:
        self.states.append((run_id, state))
        detail = self.details.get(run_id)
        if detail is not None:
            self.details[run_id] = detail.model_copy(
                update={
                    "state": state,
                    "current_stage": stage,
                    "work_counters": dict(counters),
                    "warning_codes": warning_codes,
                    "committed_count": committed_count,
                    "model_usage": model_usage,
                }
            )


class BlockingExecutor:
    def __init__(self, cancellation: CancellationController) -> None:
        self.cancellation = cancellation
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.run_ids: list[UUID] = []

    async def run(self, run_id: UUID) -> WorkflowResult:
        self.run_ids.append(run_id)
        self.started.set()
        await self.release.wait()
        stopped = await self.cancellation.requested(run_id)
        return WorkflowResult(
            run_id=run_id,
            status="stopped" if stopped else "completed",
            counters=WorkCounters(queries=2, input_tokens=10, output_tokens=5),
            warnings=(),
            survivor_count=1,
            target_count=5,
            admitted_count=1,
            evaluated_count=1,
            achieved_count=1,
            committed_count=1,
            non_counting_outcomes=NonCountingOutcomes(),
            shortfall_code=None,
            limit_code=None,
            limit_stage=None,
            trace=("terminal_cleanup",),
        )


class EmptyExecutor:
    def __init__(self, status: WorkflowStatus) -> None:
        self.status: WorkflowStatus = status

    async def run(self, run_id: UUID) -> WorkflowResult:
        return WorkflowResult(
            run_id=run_id,
            status=self.status,
            counters=WorkCounters(),
            warnings=(),
            survivor_count=0,
            target_count=5,
            admitted_count=0,
            evaluated_count=0,
            achieved_count=0,
            committed_count=0,
            non_counting_outcomes=NonCountingOutcomes(),
            shortfall_code="bounded_pool_exhausted",
            limit_code=None,
            limit_stage=None,
            trace=("terminal_cleanup",),
        )


class PartialCommitThenRaisingExecutor:
    def __init__(self, states: MemoryRunStateStore) -> None:
        self.states = states

    async def run(self, run_id: UUID) -> WorkflowResult:
        detail = self.states.details[run_id]
        self.states.details[run_id] = detail.model_copy(
            update={
                "current_stage": "focused_retrieval",
                "work_counters": {"queries": 14, "retrieved_pages": 30},
                "warning_codes": ("source_unavailable",),
                "target_count": 5,
                "admitted_count": 2,
                "evaluated_count": 1,
                "achieved_count": 1,
                "committed_count": 1,
            }
        )
        raise RuntimeError("later candidate failed")


def _snapshot(run_id: UUID) -> RunSnapshot:
    return RunSnapshot(
        id=run_id,
        effective_config={"industry": "Property management"},
        workflow_version="phase2.fixed-graph.v1",
        schema_version="phase2.run.v1",
    )


@pytest.mark.anyio
async def test_manager_owns_one_background_task_and_rejects_a_second_run() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    executor = BlockingExecutor(cancellation)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=executor,
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )

    first = UUID(int=1)
    assert await manager.start(_snapshot(first)) == first
    await executor.started.wait()
    assert manager.active_run_id == first
    with pytest.raises(ActiveRunError) as error:
        await manager.start(_snapshot(UUID(int=2)))
    assert error.value.run_id == first
    assert len(states.snapshots) == 1

    executor.release.set()
    await manager.wait(first)

    assert manager.active_run_id is None
    assert states.states == [(first, "completed")]
    assert states.deleted == []
    assert [item.event.kind for item in progress.events] == ["run_started", "terminal"]
    assert progress.events[-1].event.counters.total_tokens == 15


@pytest.mark.anyio
async def test_stop_is_idempotent_and_never_cancels_the_asyncio_task() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    executor = BlockingExecutor(cancellation)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=executor,
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    run_id = UUID(int=3)
    await manager.start(_snapshot(run_id))
    await executor.started.wait()

    assert await manager.stop(run_id) is True
    assert await manager.stop(run_id) is True
    assert manager.active_run_id == run_id
    executor.release.set()
    await manager.wait(run_id)

    assert states.states[-1] == (run_id, "stopped")
    assert states.deleted == []
    assert [event.event.kind for event in progress.events].count("stopping") == 1
    assert await manager.stop(run_id) is False


@pytest.mark.anyio
async def test_redis_loss_at_a_boundary_becomes_cooperative_stop() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    cancellation.activate(UUID(int=4))
    progress.available = False

    assert await cancellation.requested(UUID(int=4)) is True
    assert cancellation.redis_was_lost(UUID(int=4)) is True


@pytest.mark.anyio
async def test_redis_loss_during_stop_is_accepted_and_finishes_stopped() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    executor = BlockingExecutor(cancellation)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=executor,
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    run_id = UUID(int=40)
    await manager.start(_snapshot(run_id))
    await executor.started.wait()
    progress.available = False

    assert await manager.stop(run_id) is True
    executor.release.set()
    await manager.wait(run_id)

    assert states.states[-1] == (run_id, "stopped")
    assert states.deleted == []


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["completed", "completed_with_warnings", "stopped", "failed"])
async def test_normal_empty_terminal_run_is_deleted(terminal: WorkflowStatus) -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=EmptyExecutor(cast(WorkflowStatus, terminal)),
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    run_id = UUID(int=50)

    await manager.start(_snapshot(run_id))
    await manager.wait(run_id)

    assert states.deleted == [run_id]
    assert states.states == []
    assert progress.events[-1].event.state == terminal
    transient = manager.get_transient_terminal(run_id)
    assert transient is not None
    assert transient.state == terminal
    assert transient.current_stage == "terminal_cleanup"
    assert transient.committed_count == 0
    assert transient.created_at == datetime(2026, 7, 22, tzinfo=UTC)
    assert transient.effective_config == _snapshot(run_id).effective_config


@pytest.mark.anyio
async def test_next_accepted_run_evicts_the_previous_transient_terminal_snapshot() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=EmptyExecutor("completed"),
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    first = UUID(int=52)
    second = UUID(int=53)

    await manager.start(_snapshot(first))
    await manager.wait(first)
    assert manager.get_transient_terminal(first) is not None

    await manager.start(_snapshot(second))

    assert manager.get_transient_terminal(first) is None
    await manager.wait(second)
    assert manager.get_transient_terminal(second) is not None


@pytest.mark.anyio
async def test_failed_next_start_keeps_the_previous_transient_terminal_snapshot() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=EmptyExecutor("completed"),
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    first = UUID(int=54)
    second = UUID(int=55)

    await manager.start(_snapshot(first))
    await manager.wait(first)
    previous = manager.get_transient_terminal(first)
    assert previous is not None

    progress.fail_publish = True
    with pytest.raises(ProgressUnavailable):
        await manager.start(_snapshot(second))

    assert manager.get_transient_terminal(first) == previous


def test_new_manager_has_no_transient_terminal_snapshot_after_restart() -> None:
    progress = MemoryProgress()
    run_id = UUID(int=56)
    manager = RunManager(
        executor=EmptyExecutor("completed"),
        state_store=MemoryRunStateStore(),
        progress=progress,
        cancellation=CancellationController(progress),
    )

    assert manager.get_transient_terminal(run_id) is None


@pytest.mark.anyio
async def test_start_publish_failure_deletes_the_empty_run() -> None:
    progress = MemoryProgress()
    progress.fail_publish = True
    cancellation = CancellationController(progress)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=EmptyExecutor("completed"),
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    run_id = UUID(int=51)

    with pytest.raises(ProgressUnavailable):
        await manager.start(_snapshot(run_id))

    assert states.deleted == [run_id]
    assert manager.active_run_id is None


@pytest.mark.anyio
async def test_outer_executor_failure_preserves_authoritative_earlier_commit_and_counters() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    states = MemoryRunStateStore()
    manager = RunManager(
        executor=PartialCommitThenRaisingExecutor(states),
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    run_id = UUID(int=57)

    await manager.start(_snapshot(run_id))
    await manager.wait(run_id)

    detail = states.details[run_id]
    assert detail.state == "failed"
    assert detail.current_stage == "focused_retrieval"
    assert detail.work_counters["queries"] == 14
    assert detail.committed_count == 1
    assert detail.achieved_count == 1
    assert detail.admitted_count == 2
    assert detail.warning_codes == ("source_unavailable", "run_execution_failed")
    assert states.deleted == []


def _usage_records(count: int) -> tuple[ModelCallUsage, ...]:
    return tuple(
        ModelCallUsage(
            model="offline-chat",
            role="problem_analyst",
            call_index=index,
            input_tokens=1,
            output_tokens=1,
        )
        for index in range(1, count + 1)
    )


def test_run_detail_accepts_65_and_256_usage_records_but_rejects_257() -> None:
    common = {
        "id": UUID(int=58),
        "state": "running",
        "opportunity_id": None,
        "effective_config": {},
        "workflow_version": "phase7.sequential.v1",
        "schema_version": "phase2.run.v1",
        "created_at": datetime(2026, 8, 1, tzinfo=UTC),
    }

    assert (
        len(RunDetail.model_validate({**common, "model_usage": _usage_records(65)}).model_usage)
        == 65
    )
    assert (
        len(RunDetail.model_validate({**common, "model_usage": _usage_records(256)}).model_usage)
        == 256
    )
    with pytest.raises(ValidationError, match="at most 256"):
        RunDetail.model_validate({**common, "model_usage": _usage_records(257)})


def test_progress_event_has_no_payload_channel_for_sensitive_content() -> None:
    event = ProgressEvent(
        run_id=UUID(int=5),
        kind="stage_started",
        state="running",
        stage="discovery_search",
        counters=WorkCounters(),
        committed_count=0,
    )
    assert set(event.model_dump()) == {
        "run_id",
        "kind",
        "state",
        "stage",
        "counters",
        "target_count",
        "admitted_count",
        "evaluated_count",
        "achieved_count",
        "committed_count",
        "non_counting_outcomes",
        "capacity",
        "limit_code",
        "limit_stage",
        "shortfall_code",
        "warning_code",
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProgressEvent.model_validate({**event.model_dump(), "excerpt": "forbidden"})
    with pytest.raises(ValidationError, match="stage"):
        ProgressEvent.model_validate(
            {**event.model_dump(), "stage": "https://source.example/private?q=secret"}
        )
    assert progress_payload_contains_prohibited_key({"safe": [{"raw_output": "secret"}]})


@pytest.mark.anyio
async def test_graph_observer_publishes_stage_commit_and_warning_events() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    run_id = UUID(int=6)
    cancellation.activate(run_id)
    states = MemoryRunStateStore()
    observer = RunProgressObserver(
        run_id=run_id,
        state_store=states,
        progress=progress,
        cancellation=cancellation,
    )
    state = {
        "counters": WorkCounters(queries=2, input_tokens=5),
        "warnings": ("source_unavailable",),
        "committed_count": 1,
        "model_usage": (),
    }

    await observer.stage_started(state, WorkflowNode.SIMILARITY_COMMIT)
    await observer.stage_completed(
        state,
        WorkflowNode.SIMILARITY_COMMIT,
        NodeResult(committed_count=1),
    )

    assert [record.event.kind for record in progress.events] == [
        "stage_started",
        "opportunity_committed",
        "warning",
        "stage_completed",
    ]
