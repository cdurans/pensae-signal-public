import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
from starlette.types import Message
from tests.fakes.health import fake_health_service

from pensae.api.app import RequestCorrelationMiddleware, create_app
from pensae.config.settings import BootstrapSettings, Environment
from pensae.diagnostics import (
    LauncherOwnershipStatus,
    OwnershipState,
    configure_operational_logging,
)


def make_client(*, release: bool = False) -> httpx.AsyncClient:
    settings = BootstrapSettings.model_validate(
        {"environment": Environment.RELEASE if release else Environment.TEST}
    )
    app = create_app(settings=settings, health_service=fake_health_service())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.canonical_origin,
    )


@pytest.mark.anyio
async def test_invalid_host_and_forwarded_headers_are_rejected() -> None:
    async with make_client() as client:
        bad_host = await client.get("/api/status", headers={"host": "localhost:8000"})
        forwarded = await client.get("/api/status", headers={"x-forwarded-host": "evil.test"})

    assert bad_host.status_code == 400
    assert forwarded.status_code == 400


@pytest.mark.anyio
async def test_bootstrap_returns_nonce_and_security_headers() -> None:
    async with make_client() as client:
        response = await client.get("/api/bootstrap")

    assert response.status_code == 200
    assert response.json()["session_nonce"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-request-id"]


@pytest.mark.anyio
async def test_status_exposes_safe_dependency_and_launcher_ownership_metadata() -> None:
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        ownership_status_provider=lambda: LauncherOwnershipStatus(
            app=OwnershipState.OWNED,
            chat=OwnershipState.INVALID,
            embedding=OwnershipState.NONE,
            failure_code="chat_identity_mismatch",
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        response = await client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert [item["dependency"] for item in payload["checks"]] == [
        "postgresql",
        "redis",
        "searxng",
        "chat",
        "embedding",
    ]
    assert payload["launcher"] == {
        "app": "owned",
        "chat": "invalid",
        "embedding": "none",
        "failure_code": "chat_identity_mismatch",
    }
    assert "pid" not in response.text
    assert "executable" not in response.text


@pytest.mark.anyio
async def test_mutation_requires_json_nonce_and_same_origin() -> None:
    async with make_client() as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]

        wrong_content = await asyncio.wait_for(client.post("/api/runs", content="{}"), timeout=2)
        missing_nonce = await asyncio.wait_for(client.post("/api/runs", json={}), timeout=2)
        wrong_origin = await asyncio.wait_for(
            client.post(
                "/api/runs",
                json={},
                headers={"x-pensae-session": nonce, "origin": "http://evil.test"},
            ),
            timeout=2,
        )
        accepted_boundary = await asyncio.wait_for(
            client.post(
                "/api/runs",
                json={},
                headers={
                    "x-pensae-session": nonce,
                    "origin": "http://127.0.0.1:8000",
                },
            ),
            timeout=2,
        )

    assert wrong_content.status_code == 415
    assert missing_nonce.status_code == 403
    assert wrong_origin.status_code == 403
    assert accepted_boundary.status_code == 501


@pytest.mark.anyio
async def test_release_mode_disables_interactive_api_documentation() -> None:
    async with make_client(release=True) as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        openapi = await client.get("/openapi.json")

    assert docs.status_code == 404
    assert redoc.status_code == 404
    assert openapi.status_code == 404


@pytest.mark.anyio
async def test_failed_infrastructure_preparation_blocks_preflight_and_start() -> None:
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        infrastructure_ready=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        preflight = await client.get("/api/runs/preflight")
        start = await client.post(
            "/api/runs",
            json={},
            headers={"x-pensae-session": nonce, "origin": settings.canonical_origin},
        )

    assert preflight.status_code == 200
    assert preflight.json()["ready"] is False
    postgres = next(
        check for check in preflight.json()["checks"] if check["dependency"] == "postgresql"
    )
    assert postgres["state"] == "incompatible"
    assert start.status_code == 409


@pytest.mark.anyio
async def test_unhandled_api_exception_is_sanitized_before_response_and_logging(
    tmp_path: Path,
) -> None:
    secret = "prompt=private-model-output&cookie=nonce-secret"

    class ExplodingHealth:
        async def preflight(self):
            raise RuntimeError(secret)

    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    logger = configure_operational_logging(tmp_path, verbose=True)
    app = create_app(
        settings=settings,
        health_service=ExplodingHealth(),  # type: ignore[arg-type]
        operational_logger=logger,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        response = await client.get("/api/status")

    for handler in logging.getLogger("pensae.operations").handlers:
        handler.flush()
    log_text = (tmp_path / "pensae.jsonl").read_text(encoding="utf-8")
    payload = json.loads(log_text)
    assert response.status_code == 500
    assert response.json()["detail"] == "request failed safely"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert secret not in response.text
    assert secret not in log_text
    assert payload["event"] == "request_failed"
    assert payload["error_type"] == "RuntimeError"


@pytest.mark.anyio
async def test_post_response_exception_is_completed_without_secret_propagation(
    tmp_path: Path,
) -> None:
    secret = "SECRET_STREAM_MESSAGE_WITH_PROMPT_OUTPUT"
    logger = configure_operational_logging(tmp_path, verbose=True)
    sent: list[Message] = []

    async def failing_stream(scope, receive, send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError(secret)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def validating_send(message: Message) -> None:
        if sent and message["type"] == "http.response.start":
            raise RuntimeError("a second response start is invalid")
        if not sent and message["type"] != "http.response.start":
            raise RuntimeError("response body arrived before response start")
        sent.append(message)

    middleware = RequestCorrelationMiddleware(failing_stream, logger=logger)
    await middleware({"type": "http"}, receive, validating_send)  # type: ignore[arg-type]

    for handler in logging.getLogger("pensae.operations").handlers:
        handler.flush()
    log_text = (tmp_path / "pensae.jsonl").read_text(encoding="utf-8")
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert secret not in log_text
