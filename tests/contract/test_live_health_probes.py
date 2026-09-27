from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from pensae.domain.health import DEPENDENCY_ORDER, DependencyHealth, DependencyName, HealthState
from pensae.infrastructure.health.live import ModelCapabilityProbe, SearxngProbe
from pensae.infrastructure.health.service import HealthProbe, HealthService, StaticHealthProbe


@pytest.mark.anyio
async def test_searxng_requires_json_search_contract() -> None:
    html_only = httpx.MockTransport(lambda _: httpx.Response(404, text="not found"))

    result = await SearxngProbe("http://127.0.0.1:8888", transport=html_only).check()

    assert result.state is HealthState.INCOMPATIBLE
    assert "JSON search contract" in result.summary


@pytest.mark.anyio
async def test_searxng_accepts_required_json_search_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        assert parse_qs(request.content.decode("ascii"))["engines"] == ["brave,duckduckgo,yandex"]
        return httpx.Response(200, json={"results": []})

    result = await SearxngProbe(
        "http://127.0.0.1:8888", transport=httpx.MockTransport(handler)
    ).check()

    assert result.state is HealthState.READY


@pytest.mark.anyio
async def test_live_model_probe_distinguishes_unknown_listener() -> None:
    unknown = httpx.MockTransport(lambda _: httpx.Response(200, text="not llama.cpp"))

    result = await ModelCapabilityProbe(
        dependency=DependencyName.CHAT,
        base_url="http://127.0.0.1:8085",
        expected_model="expected-chat",
        transport=unknown,
    ).check()

    assert result.state is HealthState.UNKNOWN_LISTENER
    assert "will not signal" in (result.action or "")


@pytest.mark.anyio
async def test_live_model_probe_distinguishes_connection_refusal() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await ModelCapabilityProbe(
        dependency=DependencyName.CHAT,
        base_url="http://127.0.0.1:8085",
        expected_model="expected-chat",
        transport=httpx.MockTransport(refuse),
    ).check()

    assert result.state is HealthState.UNAVAILABLE


@pytest.mark.anyio
async def test_pinned_loading_state_reaches_preflight_and_blocks_runs() -> None:
    loading = httpx.MockTransport(
        lambda _: httpx.Response(
            503,
            json={
                "error": {
                    "code": 503,
                    "message": "Loading model",
                    "type": "unavailable_error",
                }
            },
        )
    )
    probes: dict[DependencyName, HealthProbe] = {
        name: StaticHealthProbe(
            DependencyHealth(
                dependency=name,
                state=HealthState.READY,
                summary=f"{name.value} ready",
            )
        )
        for name in DEPENDENCY_ORDER
    }
    probes[DependencyName.CHAT] = ModelCapabilityProbe(
        dependency=DependencyName.CHAT,
        base_url="http://127.0.0.1:8085",
        expected_model="expected-chat",
        transport=loading,
    )

    preflight = await HealthService(probes).preflight()

    chat = next(check for check in preflight.checks if check.dependency is DependencyName.CHAT)
    assert chat.state is HealthState.STARTING
    assert not preflight.ready
    assert preflight.blockers[0].state is HealthState.STARTING


@pytest.mark.anyio
async def test_unrelated_503_is_unknown_in_production_health() -> None:
    unrelated = httpx.MockTransport(lambda _: httpx.Response(503, json={"status": "maintenance"}))

    result = await ModelCapabilityProbe(
        dependency=DependencyName.CHAT,
        base_url="http://127.0.0.1:8085",
        expected_model="expected-chat",
        transport=unrelated,
    ).check()

    assert result.state is HealthState.UNKNOWN_LISTENER


@pytest.mark.anyio
@pytest.mark.parametrize(
    "chat_payload",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "not json"}}]},
        {"choices": [{"message": {"content": "{}"}}]},
    ],
)
async def test_live_chat_probe_rejects_unusable_structured_output(
    chat_payload: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "expected-chat"}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json=chat_payload)
        return httpx.Response(404)

    result = await ModelCapabilityProbe(
        dependency=DependencyName.CHAT,
        base_url="http://127.0.0.1:8085",
        expected_model="expected-chat",
        transport=httpx.MockTransport(handler),
    ).check()

    assert result.state is HealthState.INCOMPATIBLE


@pytest.mark.anyio
async def test_live_chat_probe_disables_thinking_like_the_production_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "expected-chat"}]})
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
        return httpx.Response(404)

    result = await ModelCapabilityProbe(
        dependency=DependencyName.CHAT,
        base_url="http://127.0.0.1:8085",
        expected_model="expected-chat",
        transport=httpx.MockTransport(handler),
    ).check()

    assert result.state is HealthState.READY
