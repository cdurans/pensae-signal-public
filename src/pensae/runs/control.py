"""Single-task run ownership and bounded progress contracts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pensae.opportunities import RunDetail, RunSnapshot
from pensae.opportunities.records import (
    RunCapacity,
    RunNonCountingOutcomes,
    persisted_run_counters,
    persisted_run_warnings,
)
from pensae.research.workflow import (
    LimitCode,
    ModelCallUsage,
    NodeResult,
    NonCountingOutcomes,
    ShortfallCode,
    WorkCounters,
    WorkflowNode,
    WorkflowResult,
    WorkflowStatus,
    WorkflowStopRequested,
)

ProgressKind = Literal[
    "run_started",
    "stage_started",
    "stage_completed",
    "warning",
    "opportunity_committed",
    "stopping",
    "terminal",
]

_LOGGER = logging.getLogger(__name__)


class ProgressEvent(BaseModel):
    """Allowlisted event payload; retrieved/model/operator content has no field here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    kind: ProgressKind
    state: WorkflowStatus
    stage: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    counters: WorkCounters
    target_count: int = Field(default=0, ge=0)
    admitted_count: int = Field(default=0, ge=0)
    evaluated_count: int = Field(default=0, ge=0)
    achieved_count: int = Field(default=0, ge=0)
    committed_count: int = Field(ge=0)
    non_counting_outcomes: RunNonCountingOutcomes = Field(default_factory=RunNonCountingOutcomes)
    capacity: RunCapacity | None = None
    limit_code: LimitCode | None = None
    limit_stage: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")
    shortfall_code: ShortfallCode | None = None
    warning_code: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"
    )


class ProgressRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(pattern=r"^[0-9]+-[0-9]+$")
    event: ProgressEvent


@dataclass(frozen=True, slots=True)
class ProgressReplay:
    records: tuple[ProgressRecord, ...]
    requires_snapshot: bool


class ActiveRunError(RuntimeError):
    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"research run {run_id} is already active")
        self.run_id = run_id


class ProgressUnavailable(RuntimeError):
    """Redis progress/cancellation state is unavailable or malformed."""


class ProgressStore(Protocol):
    async def initialize(self, run_id: UUID) -> None: ...

    async def publish(self, event: ProgressEvent) -> str: ...

    async def request_stop(self, run_id: UUID) -> None: ...

    async def is_stop_requested(self, run_id: UUID) -> bool: ...

    async def replay(self, run_id: UUID, after_id: str | None) -> ProgressReplay: ...


class RunStateStore(Protocol):
    async def create_run(self, snapshot: RunSnapshot) -> UUID: ...

    async def set_run_state(self, run_id: UUID, state: WorkflowStatus) -> None: ...

    async def delete_empty_run(self, run_id: UUID) -> bool: ...

    async def get_run(self, run_id: UUID) -> RunDetail | None: ...

    async def update_run_progress(
        self,
        run_id: UUID,
        *,
        state: WorkflowStatus,
        stage: str,
        counters: Mapping[str, int],
        warning_codes: tuple[str, ...],
        committed_count: int,
        model_usage: tuple[ModelCallUsage, ...] = (),
    ) -> None: ...


class RunExecutor(Protocol):
    async def run(self, run_id: UUID) -> WorkflowResult: ...


class CancellationController:
    """Redis-authoritative cancellation with an in-process Event accelerator."""

    def __init__(self, progress: ProgressStore) -> None:
        self._progress = progress
        self._events: dict[UUID, asyncio.Event] = {}
        self._redis_lost: set[UUID] = set()

    def activate(self, run_id: UUID) -> None:
        self._events[run_id] = asyncio.Event()
        self._redis_lost.discard(run_id)

    def deactivate(self, run_id: UUID) -> None:
        self._events.pop(run_id, None)
        self._redis_lost.discard(run_id)

    async def request(self, run_id: UUID) -> None:
        event = self._events.setdefault(run_id, asyncio.Event())
        try:
            await self._progress.request_stop(run_id)
        except ProgressUnavailable:
            self._redis_lost.add(run_id)
        event.set()

    async def requested(self, run_id: UUID) -> bool:
        event = self._events.get(run_id)
        if event is not None and event.is_set():
            return True
        try:
            return await self._progress.is_stop_requested(run_id)
        except ProgressUnavailable:
            self._redis_lost.add(run_id)
            if event is not None:
                event.set()
            return True

    def redis_was_lost(self, run_id: UUID) -> bool:
        return run_id in self._redis_lost

    def fail_closed(self, run_id: UUID) -> None:
        self._redis_lost.add(run_id)
        self._events.setdefault(run_id, asyncio.Event()).set()


class RunProgressObserver:
    """Mirror each fixed graph boundary to PostgreSQL and the bounded Redis stream."""

    def __init__(
        self,
        *,
        run_id: UUID,
        state_store: RunStateStore,
        progress: ProgressStore,
        cancellation: CancellationController,
    ) -> None:
        self._run_id = run_id
        self._state_store = state_store
        self._progress = progress
        self._cancellation = cancellation
        self._last_committed = 0
        self._published_warnings: set[str] = set()

    async def stage_started(self, state: Mapping[str, object], node: WorkflowNode) -> None:
        await self._record(state, node, "stage_started")

    async def stage_completed(
        self, state: Mapping[str, object], node: WorkflowNode, result: NodeResult
    ) -> None:
        del result
        await self._record(state, node, "stage_completed")

    async def _record(
        self, state: Mapping[str, object], node: WorkflowNode, kind: ProgressKind
    ) -> None:
        counters = state.get("counters")
        warnings = state.get("warnings", ())
        committed = state.get("committed_count", 0)
        model_usage = state.get("model_usage", ())
        (
            target_count,
            admitted_count,
            evaluated_count,
            achieved_count,
            non_counting_outcomes,
            shortfall_code,
            limit_code,
            limit_stage,
        ) = _progress_diagnostics(state)
        if not isinstance(counters, WorkCounters):
            raise TypeError("workflow observer requires typed counters")
        if not isinstance(warnings, tuple) or not all(
            isinstance(code, str) and _is_identifier(code) for code in warnings
        ):
            raise TypeError("workflow observer requires warning identifiers")
        if not isinstance(committed, int) or committed < 0:
            raise TypeError("workflow observer requires a valid committed count")
        if not isinstance(model_usage, tuple) or not all(
            isinstance(item, ModelCallUsage) for item in model_usage
        ):
            raise TypeError("workflow observer requires typed model usage")
        try:
            await self._state_store.update_run_progress(
                self._run_id,
                state="running",
                stage=node.value,
                counters=persisted_run_counters(
                    asdict(counters),
                    target_count=target_count,
                    admitted_count=admitted_count,
                    evaluated_count=evaluated_count,
                    achieved_count=achieved_count,
                    non_counting_outcomes=asdict(non_counting_outcomes),
                ),
                warning_codes=persisted_run_warnings(
                    warnings,
                    shortfall_code=shortfall_code,
                    limit_code=limit_code,
                    limit_stage=limit_stage,
                ),
                committed_count=committed,
                model_usage=cast(tuple[ModelCallUsage, ...], model_usage),
            )
            await self._progress.publish(
                ProgressEvent(
                    run_id=self._run_id,
                    kind=kind,
                    state="running",
                    stage=node.value,
                    counters=counters,
                    target_count=target_count,
                    admitted_count=admitted_count,
                    evaluated_count=evaluated_count,
                    achieved_count=achieved_count,
                    committed_count=committed,
                    non_counting_outcomes=_run_outcomes(non_counting_outcomes),
                    limit_code=limit_code,
                    limit_stage=limit_stage,
                    shortfall_code=shortfall_code,
                )
            )
            if committed > self._last_committed:
                await self._progress.publish(
                    ProgressEvent(
                        run_id=self._run_id,
                        kind="opportunity_committed",
                        state="running",
                        stage=node.value,
                        counters=counters,
                        target_count=target_count,
                        admitted_count=admitted_count,
                        evaluated_count=evaluated_count,
                        achieved_count=achieved_count,
                        committed_count=committed,
                        non_counting_outcomes=_run_outcomes(non_counting_outcomes),
                        limit_code=limit_code,
                        limit_stage=limit_stage,
                        shortfall_code=shortfall_code,
                    )
                )
                self._last_committed = committed
            for warning in warnings:
                if warning not in self._published_warnings:
                    await self._progress.publish(
                        ProgressEvent(
                            run_id=self._run_id,
                            kind="warning",
                            state="running",
                            stage=node.value,
                            counters=counters,
                            target_count=target_count,
                            admitted_count=admitted_count,
                            evaluated_count=evaluated_count,
                            achieved_count=achieved_count,
                            committed_count=committed,
                            non_counting_outcomes=_run_outcomes(non_counting_outcomes),
                            limit_code=limit_code,
                            limit_stage=limit_stage,
                            shortfall_code=shortfall_code,
                            warning_code=warning,
                        )
                    )
                    self._published_warnings.add(warning)
        except ProgressUnavailable as exc:
            self._cancellation.fail_closed(self._run_id)
            raise WorkflowStopRequested("progress dependency unavailable") from exc


class RunManager:
    """Own exactly one in-process asyncio task; browser lifetime is irrelevant."""

    def __init__(
        self,
        *,
        executor: RunExecutor,
        state_store: RunStateStore,
        progress: ProgressStore,
        cancellation: CancellationController,
    ) -> None:
        self._executor = executor
        self._state_store = state_store
        self._progress = progress
        self._cancellation = cancellation
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._active_run_id: UUID | None = None
        self._transient_terminal: RunDetail | None = None
        self._stopping: set[UUID] = set()

    @property
    def active_run_id(self) -> UUID | None:
        task = self._task
        return self._active_run_id if task is not None and not task.done() else None

    def get_transient_terminal(self, run_id: UUID) -> RunDetail | None:
        snapshot = self._transient_terminal
        return snapshot if snapshot is not None and snapshot.id == run_id else None

    async def start(self, snapshot: RunSnapshot) -> UUID:
        async with self._lock:
            active = self.active_run_id
            if active is not None:
                raise ActiveRunError(active)
            await self._progress.initialize(snapshot.id)
            await self._state_store.create_run(snapshot)
            self._cancellation.activate(snapshot.id)
            self._active_run_id = snapshot.id
            try:
                target_count = _target_from_snapshot(snapshot)
                await self._progress.publish(
                    ProgressEvent(
                        run_id=snapshot.id,
                        kind="run_started",
                        state="running",
                        stage="created",
                        counters=WorkCounters(),
                        target_count=target_count,
                        committed_count=0,
                    )
                )
            except ProgressUnavailable:
                await self._state_store.delete_empty_run(snapshot.id)
                self._cancellation.deactivate(snapshot.id)
                self._active_run_id = None
                raise
            self._transient_terminal = None
            self._task = asyncio.create_task(
                self._drive(snapshot.id), name=f"pensae-run-{snapshot.id}"
            )
            return snapshot.id

    async def stop(self, run_id: UUID) -> bool:
        async with self._lock:
            if self.active_run_id != run_id:
                return False
            if run_id in self._stopping:
                return True
            await self._cancellation.request(run_id)
            self._stopping.add(run_id)
            snapshot = await self._state_store.get_run(run_id)
            if snapshot is not None:
                snapshot = _validated_detail(snapshot)
            counters = (
                _counters_from_mapping(snapshot.work_counters)
                if snapshot is not None
                else WorkCounters()
            )
            committed_count = snapshot.committed_count if snapshot is not None else 0
            warnings = snapshot.warning_codes if snapshot is not None else ()
            await self._state_store.update_run_progress(
                run_id,
                state="stopping",
                stage="stopping",
                counters=persisted_run_counters(
                    asdict(counters),
                    target_count=snapshot.target_count if snapshot is not None else 0,
                    admitted_count=snapshot.admitted_count if snapshot is not None else 0,
                    evaluated_count=snapshot.evaluated_count if snapshot is not None else 0,
                    achieved_count=snapshot.achieved_count if snapshot is not None else 0,
                    non_counting_outcomes=(
                        snapshot.non_counting_outcomes.model_dump(mode="python")
                        if snapshot is not None
                        else RunNonCountingOutcomes().model_dump(mode="python")
                    ),
                ),
                warning_codes=persisted_run_warnings(
                    warnings,
                    shortfall_code=snapshot.shortfall_code if snapshot is not None else None,
                    limit_code=snapshot.limit_code if snapshot is not None else None,
                    limit_stage=snapshot.limit_stage if snapshot is not None else None,
                ),
                committed_count=committed_count,
                model_usage=snapshot.model_usage if snapshot is not None else (),
            )
            try:
                await self._progress.publish(
                    ProgressEvent(
                        run_id=run_id,
                        kind="stopping",
                        state="stopping",
                        stage="stopping",
                        counters=counters,
                        target_count=snapshot.target_count if snapshot is not None else 0,
                        admitted_count=snapshot.admitted_count if snapshot is not None else 0,
                        evaluated_count=snapshot.evaluated_count if snapshot is not None else 0,
                        achieved_count=snapshot.achieved_count if snapshot is not None else 0,
                        committed_count=committed_count,
                        non_counting_outcomes=(
                            snapshot.non_counting_outcomes
                            if snapshot is not None
                            else RunNonCountingOutcomes()
                        ),
                        limit_code=snapshot.limit_code if snapshot is not None else None,
                        limit_stage=snapshot.limit_stage if snapshot is not None else None,
                        shortfall_code=snapshot.shortfall_code if snapshot is not None else None,
                    )
                )
            except ProgressUnavailable:
                self._cancellation.fail_closed(run_id)
            return True

    async def wait(self, run_id: UUID) -> None:
        task = self._task
        if task is not None and self._active_run_id == run_id:
            await task

    async def _drive(self, run_id: UUID) -> None:
        counters = WorkCounters()
        committed_count = 0
        stage = "terminal_cleanup"
        terminal: WorkflowStatus = "failed"
        warning_code: str | None = None
        warning_codes: tuple[str, ...] = ()
        model_usage: tuple[ModelCallUsage, ...] = ()
        target_count = 0
        admitted_count = 0
        evaluated_count = 0
        achieved_count = 0
        non_counting_outcomes = NonCountingOutcomes()
        shortfall_code: ShortfallCode | None = None
        limit_code: LimitCode | None = None
        limit_stage: str | None = None
        authoritative_unknown = False
        try:
            result = await self._executor.run(run_id)
            counters = result.counters
            committed_count = result.committed_count
            stage = result.trace[-1] if result.trace else stage
            terminal = result.status
            model_usage = result.model_usage
            warning_codes = result.warnings
            target_count = result.target_count
            admitted_count = result.admitted_count
            evaluated_count = result.evaluated_count
            achieved_count = result.achieved_count
            non_counting_outcomes = result.non_counting_outcomes
            shortfall_code = result.shortfall_code
            limit_code = result.limit_code
            limit_stage = result.limit_stage
            if self._cancellation.redis_was_lost(run_id):
                terminal = "stopped"
                warning_code = "redis_unavailable"
        except Exception:
            if self._cancellation.redis_was_lost(run_id):
                terminal = "stopped"
                warning_code = "redis_unavailable"
            else:
                terminal = "failed"
                warning_code = "run_execution_failed"
            try:
                authoritative = await self._state_store.get_run(run_id)
                if authoritative is not None:
                    authoritative = _validated_detail(authoritative)
                    counters = _counters_from_mapping(authoritative.work_counters)
                    committed_count = authoritative.committed_count
                    stage = authoritative.current_stage
                    warning_codes = authoritative.warning_codes
                    model_usage = authoritative.model_usage
                    target_count = authoritative.target_count
                    admitted_count = authoritative.admitted_count
                    evaluated_count = authoritative.evaluated_count
                    achieved_count = authoritative.achieved_count
                    non_counting_outcomes = NonCountingOutcomes(
                        **authoritative.non_counting_outcomes.model_dump(mode="python")
                    )
                    shortfall_code = authoritative.shortfall_code
                    limit_code = authoritative.limit_code
                    limit_stage = authoritative.limit_stage
            except Exception:
                authoritative_unknown = True
        try:
            final_warnings = tuple(
                dict.fromkeys((*warning_codes, *((warning_code,) if warning_code else ())))
            )
            authoritative = None
            if committed_count == 0 and not authoritative_unknown:
                authoritative = await self._state_store.get_run(run_id)
                if authoritative is not None:
                    authoritative = _validated_detail(authoritative)
            deleted = (
                committed_count == 0
                and not authoritative_unknown
                and await self._state_store.delete_empty_run(run_id)
            )
            if deleted and authoritative is not None:
                self._transient_terminal = _validated_detail(
                    authoritative.model_copy(
                        update={
                            "state": terminal,
                            "opportunity_id": None,
                            "current_stage": stage,
                            "work_counters": asdict(counters),
                            "warning_codes": final_warnings,
                            "model_usage": model_usage,
                            "target_count": target_count,
                            "admitted_count": admitted_count,
                            "evaluated_count": evaluated_count,
                            "achieved_count": achieved_count,
                            "non_counting_outcomes": RunNonCountingOutcomes(
                                **asdict(non_counting_outcomes)
                            ),
                            "shortfall_code": shortfall_code,
                            "shortfall_detail": None,
                            "limit_code": limit_code,
                            "limit_stage": limit_stage,
                            "committed_count": 0,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )
            if authoritative_unknown:
                await self._state_store.set_run_state(run_id, terminal)
            elif not deleted:
                await self._state_store.update_run_progress(
                    run_id,
                    state=terminal,
                    stage=stage,
                    counters=persisted_run_counters(
                        asdict(counters),
                        target_count=target_count,
                        admitted_count=admitted_count,
                        evaluated_count=evaluated_count,
                        achieved_count=achieved_count,
                        non_counting_outcomes=asdict(non_counting_outcomes),
                    ),
                    warning_codes=persisted_run_warnings(
                        final_warnings,
                        shortfall_code=shortfall_code,
                        limit_code=limit_code,
                        limit_stage=limit_stage,
                    ),
                    committed_count=committed_count,
                    model_usage=model_usage,
                )
            await self._progress.publish(
                ProgressEvent(
                    run_id=run_id,
                    kind="terminal",
                    state=terminal,
                    stage=stage,
                    counters=counters,
                    target_count=target_count,
                    admitted_count=admitted_count,
                    evaluated_count=evaluated_count,
                    achieved_count=achieved_count,
                    committed_count=committed_count,
                    non_counting_outcomes=_run_outcomes(non_counting_outcomes),
                    limit_code=limit_code,
                    limit_stage=limit_stage,
                    shortfall_code=shortfall_code,
                    warning_code=warning_code or (warning_codes[-1] if warning_codes else None),
                )
            )
        except ProgressUnavailable:
            if terminal not in ("stopped", "failed"):
                await self._state_store.set_run_state(run_id, "stopped")
        except Exception:
            _LOGGER.error("run_terminal_persistence_failed")
        finally:
            self._cancellation.deactivate(run_id)
            self._stopping.discard(run_id)


def progress_payload_contains_prohibited_key(value: Mapping[str, object]) -> bool:
    prohibited = {
        "prompt",
        "raw_output",
        "extracted_text",
        "excerpt",
        "url",
        "query",
        "body",
        "note",
        "vector",
        "nonce",
        "credential",
        "header",
        "cookie",
    }
    for key, nested in value.items():
        if key.casefold() in prohibited:
            return True
        if isinstance(nested, Mapping) and progress_payload_contains_prohibited_key(nested):
            return True
        if isinstance(nested, (list, tuple)) and any(
            isinstance(item, Mapping) and progress_payload_contains_prohibited_key(item)
            for item in nested
        ):
            return True
    return False


def _is_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 64
        and value.isascii()
        and all(
            character.islower() or character.isdigit() or character == "_" for character in value
        )
    )


def _counters_from_mapping(values: Mapping[str, int]) -> WorkCounters:
    known = WorkCounters.__dataclass_fields__
    return WorkCounters(**{key: value for key, value in values.items() if key in known})


def _progress_diagnostics(
    state: Mapping[str, object],
) -> tuple[
    int,
    int,
    int,
    int,
    NonCountingOutcomes,
    ShortfallCode | None,
    LimitCode | None,
    str | None,
]:
    counts: list[int] = []
    for name in ("target_count", "admitted_count", "evaluated_count", "achieved_count"):
        value = state.get(name, 0)
        if not isinstance(value, int) or value < 0:
            raise TypeError(f"workflow observer requires a valid {name}")
        counts.append(value)
    outcomes = state.get("non_counting_outcomes", NonCountingOutcomes())
    if not isinstance(outcomes, NonCountingOutcomes):
        raise TypeError("workflow observer requires typed non-counting outcomes")
    shortfall_code = state.get("shortfall_code")
    if shortfall_code not in (None, "bounded_pool_exhausted", "insufficient_evidence"):
        raise TypeError("workflow observer requires a closed shortfall code")
    limit_code = state.get("limit_code")
    allowed_limits = set(LimitCode.__args__)
    if limit_code is not None and limit_code not in allowed_limits:
        raise TypeError("workflow observer requires a closed limit code")
    limit_stage = state.get("limit_stage")
    if limit_stage is not None and (
        not isinstance(limit_stage, str) or not _is_identifier(limit_stage)
    ):
        raise TypeError("workflow observer requires a bounded limit stage")
    return (
        counts[0],
        counts[1],
        counts[2],
        counts[3],
        outcomes,
        cast(ShortfallCode | None, shortfall_code),
        cast(LimitCode | None, limit_code),
        cast(str | None, limit_stage),
    )


def _target_from_snapshot(snapshot: RunSnapshot) -> int:
    bounds = snapshot.effective_config.get("bounds")
    if isinstance(bounds, Mapping):
        target = bounds.get("opportunities")
        if isinstance(target, int) and target >= 0:
            return target
    return 0


def _validated_detail(detail: RunDetail) -> RunDetail:
    return RunDetail.model_validate(detail.model_dump(mode="python"))


def _run_outcomes(outcomes: NonCountingOutcomes) -> RunNonCountingOutcomes:
    return RunNonCountingOutcomes(
        automatic_exact_rediscovery=outcomes.automatic_exact_rediscovery,
        updated_version=outcomes.updated_version,
        unresolved_possible_rediscovery=outcomes.unresolved_possible_rediscovery,
        invalid_candidate=outcomes.invalid_candidate,
        incomplete_candidate=outcomes.incomplete_candidate,
    )
