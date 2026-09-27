from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from tests.fakes.health import fake_health_service

from pensae.api.app import create_app
from pensae.config.protected import ProtectedConfig
from pensae.config.settings import BootstrapSettings, Environment
from pensae.opportunities import RunSnapshot
from pensae.runs.service import ResearchService
from pensae.settings import (
    DiscoveryMode,
    DurableSettingsValue,
    FutureRunSettingsSnapshot,
    LocalEndpointSettings,
    ResearchSettings,
    SavedSettingsService,
)


class SnapshotManager:
    def __init__(self) -> None:
        self.snapshots: list[RunSnapshot] = []

    @property
    def active_run_id(self):
        return None

    async def start(self, snapshot: RunSnapshot) -> None:
        self.snapshots.append(snapshot)


class MutableSettingsStore:
    def __init__(self) -> None:
        self.policy = SavedSettingsService()
        self.current = self.policy.initial_value()

    async def snapshot_for_future_run(self) -> FutureRunSettingsSnapshot:
        return self.policy.snapshot_for_future_run(self.current)

    async def get(self) -> DurableSettingsValue:
        return self.current

    def replace(self, current: DurableSettingsValue) -> None:
        self.current = current


@pytest.mark.anyio
async def test_saved_settings_apply_only_to_snapshots_created_after_save() -> None:
    manager = SnapshotManager()
    settings = MutableSettingsStore()
    service = ResearchService(
        manager=cast(Any, manager),
        progress=cast(Any, object()),
        store=cast(Any, object()),
        protected=ProtectedConfig.load(),
        settings_store=cast(Any, settings),
        recovery_complete=True,
    )

    await service.start()
    first = manager.snapshots[0]
    candidate = settings.current.values.model_copy(
        update={
            "research": ResearchSettings(
                focus="Construction estimating",
                discovery_mode=DiscoveryMode.DIRECTED,
                preferred_technologies=("Rust", "TypeScript"),
            ),
            "workflow": settings.current.values.workflow.model_copy(
                update={"discovery_queries": 3, "run_queries": 18, "model_calls": 92}
            ),
        }
    )
    settings.replace(settings.policy.save(settings.current, candidate))
    await service.start()
    second = manager.snapshots[1]

    assert first.effective_config["settings_revision"] == 1
    assert first.effective_config["industry"] == "Cross-industry opportunity discovery"
    assert first.effective_config["bounds"]["discovery_queries"] == 8
    assert second.effective_config["settings_revision"] == 2
    assert second.effective_config["industry"] == "Construction estimating"
    assert second.effective_config["bounds"]["discovery_queries"] == 3
    assert second.effective_config["bounds"]["run_queries"] == 18
    assert second.effective_config["bounds"]["model_calls"] == 92
    assert second.effective_config["saved_settings"]["research"] == {
        "focus": "Construction estimating",
        "country": "United States",
        "language": "English",
        "discovery_mode": "directed",
        "preferred_technologies": ["Rust", "TypeScript"],
    }
    assert first.effective_config["bounds"]["discovery_queries"] == 8


@pytest.mark.anyio
async def test_start_uses_one_preflighted_settings_revision_during_save_race() -> None:
    manager = SnapshotManager()
    saved = MutableSettingsStore()
    research = ResearchService(
        manager=cast(Any, manager),
        progress=cast(Any, object()),
        store=cast(Any, object()),
        protected=ProtectedConfig.load(),
        settings_store=cast(Any, saved),
        recovery_complete=True,
    )
    bootstrap = BootstrapSettings.model_validate({"environment": Environment.TEST})
    preflight_chat_urls: list[str] = []

    class RacingHealth:
        async def preflight(self):
            replacement = saved.current.values.model_copy(
                update={"endpoints": LocalEndpointSettings(chat_url="http://127.0.0.1:8099")}
            )
            saved.replace(saved.policy.save(saved.current, replacement))
            return await fake_health_service().preflight()

    def health_factory(settings: BootstrapSettings):
        preflight_chat_urls.append(settings.chat_url)
        return cast(Any, RacingHealth())

    app = create_app(
        settings=bootstrap,
        health_service=fake_health_service(),
        health_service_factory=health_factory,
        research_service=research,
        settings_store=cast(Any, saved),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=bootstrap.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        response = await client.post(
            "/api/runs",
            json={},
            headers={
                "x-pensae-session": nonce,
                "origin": bootstrap.canonical_origin,
            },
        )

    assert response.status_code == 202
    assert preflight_chat_urls == ["http://127.0.0.1:8085"]
    assert saved.current.revision == 2
    assert saved.current.values.endpoints.chat_url == "http://127.0.0.1:8099"
    assert manager.snapshots[0].effective_config["settings_revision"] == 1
    assert (
        manager.snapshots[0].effective_config["saved_settings"]["endpoints"]["chat_url"]
        == "http://127.0.0.1:8085"
    )
