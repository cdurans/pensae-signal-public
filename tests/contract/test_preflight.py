import httpx
import pytest
from tests.fakes.health import fake_health_service

from pensae.api.app import create_app
from pensae.config.settings import BootstrapSettings, Environment
from pensae.domain.health import DependencyName, HealthState


@pytest.mark.anyio
async def test_degraded_preflight_returns_all_blockers_and_start_creates_no_run() -> None:
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(
        settings=settings,
        health_service=fake_health_service(
            {
                DependencyName.POSTGRESQL: HealthState.INCOMPATIBLE,
                DependencyName.CHAT: HealthState.UNAVAILABLE,
                DependencyName.EMBEDDING: HealthState.UNKNOWN_LISTENER,
            }
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.canonical_origin,
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        preflight = await client.get("/api/runs/preflight")
        start = await client.post(
            "/api/runs",
            json={},
            headers={"x-pensae-session": nonce, "sec-fetch-site": "same-origin"},
        )

    assert preflight.status_code == 200
    assert not preflight.json()["ready"]
    assert len(preflight.json()["blockers"]) == 3
    assert start.status_code == 409
    assert "blocked" in start.json()["detail"]["message"]
