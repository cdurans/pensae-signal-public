"""Current run REST and SSE contract tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from tests.fakes.health import fake_health_service

from pensae.api.app import create_app
from pensae.config.protected import ProtectedConfig
from pensae.config.settings import BootstrapSettings, Environment
from pensae.opportunities import RunDetail, RunSnapshot
from pensae.research.workflow import (
    NonCountingOutcomes,
    WorkCounters,
    WorkflowResult,
    WorkflowStatus,
)
from pensae.runs.control import (
    CancellationController,
    ProgressEvent,
    ProgressRecord,
    ProgressReplay,
    RunManager,
)
from pensae.runs.service import ResearchService


class MemoryProgress:
    def __init__(self) -> None:
        self.events: list[ProgressRecord] = []
        self.cancelled: set[UUID] = set()

    async def initialize(self, run_id: UUID) -> None:
        self.cancelled.discard(run_id)

    async def publish(self, event: ProgressEvent) -> str:
        event_id = f"{len(self.events) + 1}-0"
        self.events.append(ProgressRecord(event_id=event_id, event=event))
        return event_id

    async def request_stop(self, run_id: UUID) -> None:
        self.cancelled.add(run_id)

    async def is_stop_requested(self, run_id: UUID) -> bool:
        return run_id in self.cancelled

    async def replay(self, run_id: UUID, after_id: str | None) -> ProgressReplay:
        records = tuple(item for item in self.events if item.event.run_id == run_id)
        if after_id is None:
            return ProgressReplay(records, False)
        for index, record in enumerate(records):
            if record.event_id == after_id:
                return ProgressReplay(records[index + 1 :], False)
        return ProgressReplay((), True)


class MemoryRunStore:
    def __init__(self) -> None:
        self.details: dict[UUID, RunDetail] = {}
        self.created_snapshots: list[RunSnapshot] = []

    async def create_run(self, snapshot: RunSnapshot) -> UUID:
        self.created_snapshots.append(snapshot)
        now = datetime(2026, 7, 22, tzinfo=UTC)
        self.details[snapshot.id] = RunDetail(
            id=snapshot.id,
            state="running",
            opportunity_id=None,
            effective_config=snapshot.effective_config,
            workflow_version=snapshot.workflow_version,
            schema_version=snapshot.schema_version,
            created_at=now,
            updated_at=now,
        )
        return snapshot.id

    async def set_run_state(self, run_id: UUID, state: WorkflowStatus) -> None:
        self.details[run_id] = self.details[run_id].model_copy(update={"state": state})

    async def delete_empty_run(self, run_id: UUID) -> bool:
        return self.details.pop(run_id, None) is not None

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
        self.details[run_id] = self.details[run_id].model_copy(
            update={
                "state": state,
                "current_stage": stage,
                "work_counters": dict(counters),
                "warning_codes": warning_codes,
                "committed_count": committed_count,
                "model_usage": model_usage,
            }
        )

    async def get_run(self, run_id: UUID) -> RunDetail | None:
        return self.details.get(run_id)

    async def get_detail(self, opportunity_id: UUID) -> None:
        del opportunity_id
        return None


class ImmediateExecutor:
    async def run(self, run_id: UUID) -> WorkflowResult:
        return WorkflowResult(
            run_id=run_id,
            status="completed",
            counters=WorkCounters(queries=1, input_tokens=12, output_tokens=4),
            warnings=(),
            survivor_count=0,
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


class EmptyStoppedExecutor:
    async def run(self, run_id: UUID) -> WorkflowResult:
        return WorkflowResult(
            run_id=run_id,
            status="stopped",
            counters=WorkCounters(queries=8, search_results=60, unique_urls=60),
            warnings=("work_limit_exceeded",),
            survivor_count=0,
            target_count=5,
            admitted_count=0,
            evaluated_count=0,
            achieved_count=0,
            committed_count=0,
            non_counting_outcomes=NonCountingOutcomes(),
            shortfall_code="bounded_pool_exhausted",
            limit_code="queries",
            limit_stage="discovery_search",
            trace=("discovery_search", "terminal_cleanup"),
        )


class RaisingExecutor:
    async def run(self, run_id: UUID) -> WorkflowResult:
        del run_id
        raise RuntimeError("synthetic executor failure")


class MissingActiveSnapshotService:
    @property
    def active_run_id(self) -> UUID:
        return UUID(int=999)

    async def get_run(self, run_id: UUID) -> None:
        del run_id
        return None


def test_start_run_openapi_declares_runtime_conflict_and_service_errors() -> None:
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(settings=settings, health_service=fake_health_service())

    responses = app.openapi()["paths"]["/api/runs"]["post"]["responses"]

    assert responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/StartRunConflictResponse"
    )
    assert responses["501"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApiErrorResponse"
    )

    active_responses = app.openapi()["paths"]["/api/runs/active"]["get"]["responses"]
    assert active_responses["503"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ActiveRunUnavailableResponse"
    )
    run_properties = app.openapi()["components"]["schemas"]["RunDetail"]["properties"]
    assert {
        "target_count",
        "admitted_count",
        "evaluated_count",
        "achieved_count",
        "committed_count",
        "committed_opportunity_ids",
        "non_counting_outcomes",
        "capacity",
        "limit_code",
        "limit_stage",
        "shortfall_code",
        "shortfall_detail",
    } <= set(run_properties)
    assert run_properties["model_usage"]["maxItems"] == 256


@pytest.mark.anyio
async def test_research_start_snapshot_sse_replay_and_terminal_stop_contract() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    store = MemoryRunStore()
    manager = RunManager(
        executor=ImmediateExecutor(),
        state_store=store,
        progress=progress,
        cancellation=cancellation,
    )
    service = ResearchService(
        manager=manager,
        progress=progress,
        store=cast(Any, store),
        protected=ProtectedConfig.load(),
    )
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        research_service=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        headers = {"x-pensae-session": nonce, "origin": settings.canonical_origin}
        started = await client.post("/api/runs", json={}, headers=headers)
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        await manager.wait(UUID(run_id))

        active = await client.get("/api/runs/active")
        snapshot = await client.get(f"/api/runs/{run_id}")
        events = await client.get(f"/api/runs/{run_id}/events")
        gap = await client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "999-0"})
        invalid_cursor = await client.get(
            f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "not-an-event-id"}
        )
        stopped = await client.post(f"/api/runs/{run_id}/stop", json={}, headers=headers)

    assert active.status_code == 204
    assert active.content == b""
    assert snapshot.status_code == 200
    assert snapshot.json()["state"] == "completed"
    assert snapshot.json()["work_counters"]["input_tokens"] == 12
    assert snapshot.json()["target_count"] == 5
    assert snapshot.json()["achieved_count"] == 1
    assert snapshot.json()["admitted_count"] == 1
    assert snapshot.json()["evaluated_count"] == 1
    assert snapshot.json()["non_counting_outcomes"] == {
        "automatic_exact_rediscovery": 0,
        "updated_version": 0,
        "unresolved_possible_rediscovery": 0,
        "invalid_candidate": 0,
        "incomplete_candidate": 0,
    }
    assert snapshot.json()["capacity"]["reserved"] == {
        "queries": 0,
        "pages": 0,
        "bytes": 0,
        "model_calls": 0,
        "repairs": 0,
        "total_tokens": 0,
    }
    assert snapshot.json()["capacity"]["consumed"]["total_tokens"] == 16
    assert snapshot.json()["limit_code"] is None
    assert snapshot.json()["shortfall_code"] is None
    effective = snapshot.json()["effective_config"]
    assert effective["industry"] == "Cross-industry opportunity discovery"
    assert effective["settings_revision"] == 1
    assert effective["saved_settings"]["research"]["discovery_mode"] == "broad"
    assert effective["fingerprint_version"] == "sha256-canonical-json-v1"
    assert effective["similarity_threshold_version"] == "phase5.labeled-offline-live-v1"
    assert effective["bounds"]["run_queries"] == 40
    assert effective["score_weights"] == [35, 30, 25, 10]
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: run_started" in events.text
    assert "event: terminal" in events.text
    assert '"target_count":5' in events.text
    assert '"capacity":{' in events.text
    assert "event: snapshot_required" in gap.text
    assert invalid_cursor.status_code == 422
    assert stopped.json() == {"run_id": run_id, "accepted": False}


@pytest.mark.anyio
async def test_empty_terminal_run_is_transiently_readable_after_durable_deletion() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    store = MemoryRunStore()
    manager = RunManager(
        executor=EmptyStoppedExecutor(),
        state_store=store,
        progress=progress,
        cancellation=cancellation,
    )
    service = ResearchService(
        manager=manager,
        progress=progress,
        store=cast(Any, store),
        protected=ProtectedConfig.load(),
    )
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        research_service=service,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        headers = {"x-pensae-session": nonce, "origin": settings.canonical_origin}
        started = await client.post("/api/runs", json={}, headers=headers)
        run_id = UUID(started.json()["run_id"])
        await manager.wait(run_id)

        assert run_id not in store.details
        active = await client.get("/api/runs/active")
        snapshot = await client.get(f"/api/runs/{run_id}")
        events = await client.get(f"/api/runs/{run_id}/events")

    assert active.status_code == 204
    assert snapshot.status_code == 200
    assert snapshot.json()["state"] == "stopped"
    assert snapshot.json()["current_stage"] == "terminal_cleanup"
    assert snapshot.json()["warning_codes"] == ["work_limit_exceeded"]
    assert snapshot.json()["committed_count"] == 0
    assert snapshot.json()["target_count"] == 5
    assert snapshot.json()["shortfall_code"] == "bounded_pool_exhausted"
    assert snapshot.json()["limit_code"] == "queries"
    assert snapshot.json()["limit_stage"] == "discovery_search"
    assert snapshot.json()["capacity"]["reserved"] == {
        "queries": 0,
        "pages": 0,
        "bytes": 0,
        "model_calls": 0,
        "repairs": 0,
        "total_tokens": 0,
    }
    assert "did not add filler" in snapshot.json()["shortfall_detail"]
    assert snapshot.json()["created_at"] == "2026-07-22T00:00:00Z"
    assert events.status_code == 200
    assert "event: terminal" in events.text


@pytest.mark.anyio
async def test_active_run_distinguishes_idle_from_unavailable_or_inconsistent_service() -> None:
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    unavailable = create_app(settings=settings, health_service=fake_health_service())
    inconsistent = create_app(
        settings=settings,
        health_service=fake_health_service(),
        research_service=cast(Any, MissingActiveSnapshotService()),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unavailable), base_url=settings.canonical_origin
    ) as client:
        response = await client.get("/api/runs/active")
    assert response.status_code == 503
    assert response.json() == {"detail": "research service unavailable"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=inconsistent), base_url=settings.canonical_origin
    ) as client:
        response = await client.get("/api/runs/active")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "active run snapshot unavailable",
        "run_id": "00000000-0000-0000-0000-0000000003e7",
    }


@pytest.mark.anyio
async def test_executor_failure_remains_explainable_after_empty_run_deletion() -> None:
    progress = MemoryProgress()
    cancellation = CancellationController(progress)
    store = MemoryRunStore()
    manager = RunManager(
        executor=RaisingExecutor(),
        state_store=store,
        progress=progress,
        cancellation=cancellation,
    )
    service = ResearchService(
        manager=manager,
        progress=progress,
        store=cast(Any, store),
        protected=ProtectedConfig.load(),
    )
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        research_service=service,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        started = await client.post(
            "/api/runs",
            json={},
            headers={
                "x-pensae-session": nonce,
                "origin": settings.canonical_origin,
            },
        )
        run_id = UUID(started.json()["run_id"])
        await manager.wait(run_id)
        snapshot = await client.get(f"/api/runs/{run_id}")

    assert run_id not in store.details
    assert snapshot.status_code == 200
    assert snapshot.json()["state"] == "failed"
    assert snapshot.json()["warning_codes"] == ["run_execution_failed"]
    assert snapshot.json()["committed_count"] == 0
