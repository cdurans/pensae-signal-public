import json
from pathlib import Path

import httpx
import pytest

from pensae.infrastructure.model_runtime import (
    EndpointState,
    HttpxEndpointProbe,
    LaunchSpec,
    ModelRole,
)


def spec(role: ModelRole) -> LaunchSpec:
    return LaunchSpec(
        role=role,
        executable=Path("/opt/llama/llama-server"),
        model_path=Path(f"/models/{role.value}.gguf"),
        port=8085 if role is ModelRole.CHAT else 8086,
        expected_model=f"expected-{role.value}",
        fixed_arguments=("--ctx-size", "8192"),
    )


@pytest.mark.anyio
async def test_connection_refusal_is_free_and_never_unknown_killable_state() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=httpx.MockTransport(refuse),
    ).inspect(spec(ModelRole.CHAT))

    assert result.state is EndpointState.FREE


@pytest.mark.anyio
async def test_non_llama_listener_is_unknown() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="not llama.cpp"))

    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=transport,
    ).inspect(spec(ModelRole.CHAT))

    assert result.state is EndpointState.UNKNOWN_LISTENER
    assert "no signal" in result.detail


@pytest.mark.anyio
async def test_exact_pinned_loading_health_is_starting() -> None:
    transport = httpx.MockTransport(
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

    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=transport,
    ).inspect(spec(ModelRole.CHAT))

    assert result.state is EndpointState.STARTING


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": 503, "message": "different", "type": "unavailable_error"}},
        {"status": "loading"},
        {"error": "Loading model"},
    ],
)
async def test_unrelated_503_listener_remains_unknown(payload: object) -> None:
    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json=payload)),
    ).inspect(spec(ModelRole.CHAT))

    assert result.state is EndpointState.UNKNOWN_LISTENER
    assert "no signal" in result.detail


@pytest.mark.anyio
async def test_wrong_model_is_incompatible() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=httpx.MockTransport(handler),
    ).inspect(spec(ModelRole.CHAT))

    assert result.state is EndpointState.INCOMPATIBLE
    assert "no signal" in result.detail


@pytest.mark.anyio
@pytest.mark.parametrize("role", [ModelRole.CHAT, ModelRole.EMBEDDING])
async def test_role_capability_contract_can_reuse_compatible_endpoint(role: ModelRole) -> None:
    expected = f"expected-{role.value}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": expected}]})
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
        if request.url.path == "/v1/embeddings":
            return httpx.Response(200, json={"data": [{"embedding": [1.0] + [0.0] * 1023}]})
        return httpx.Response(404)

    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=httpx.MockTransport(handler),
    ).inspect(spec(role))

    assert result.state is EndpointState.COMPATIBLE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "chat_payload",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "not json"}}]},
        {"choices": [{"message": {"content": "{}"}}]},
        {"choices": [{"message": {}}]},
    ],
)
async def test_chat_reuse_rejects_empty_or_non_structured_output(
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

    result = await HttpxEndpointProbe(
        embedding_dimension=1024,
        transport=httpx.MockTransport(handler),
    ).inspect(spec(ModelRole.CHAT))

    assert result.state is EndpointState.INCOMPATIBLE
