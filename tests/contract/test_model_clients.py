from __future__ import annotations

import json

import httpx
import pytest

from pensae.infrastructure.models import (
    ChatMessage,
    ChatParameters,
    ChatRequest,
    LlamaCppChatClient,
    LlamaCppEmbeddingClient,
    ModelContractError,
)


def _parameters() -> ChatParameters:
    return ChatParameters(
        temperature=0.7,
        top_p=0.8,
        top_k=20,
        min_p=0,
        presence_penalty=1.5,
        repetition_penalty=1,
    )


@pytest.mark.anyio
async def test_chat_client_counts_tokens_without_generation() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert json.loads(request.content) == {
            "content": "system: fixed policy\nuser: bounded input",
            "add_special": True,
            "with_pieces": False,
        }
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})

    client = LlamaCppChatClient(
        base_url="http://127.0.0.1:8085",
        model_id="chat-model",
        parameters=_parameters(),
        transport=httpx.MockTransport(handler),
    )

    count = await client.count_tokens(
        (
            ChatMessage(role="system", content="fixed policy"),
            ChatMessage(role="user", content="bounded input"),
        )
    )

    assert count == 4
    assert paths == ["/tokenize"]


@pytest.mark.anyio
async def test_chat_client_sends_fixed_nonthinking_schema_constrained_payload() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"queries":["workflow"]}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    client = LlamaCppChatClient(
        base_url="http://127.0.0.1:8085",
        model_id="chat-model",
        parameters=_parameters(),
        transport=httpx.MockTransport(handler),
    )
    response = await client.complete(
        ChatRequest(
            messages=(ChatMessage(role="user", content="Return a plan"),),
            max_tokens=2_048,
            schema_name="query_plan",
            response_schema={
                "type": "object",
                "properties": {"queries": {"type": "array"}},
                "required": ["queries"],
            },
            seed=42,
        )
    )

    assert response.prompt_tokens == 12
    assert response.completion_tokens == 5
    assert requests == [
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "Return a plan"}],
            "stream": False,
            "max_tokens": 2_048,
            "seed": 42,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0,
            "presence_penalty": 1.5,
            "repeat_penalty": 1,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "query_plan",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"queries": {"type": "array"}},
                        "required": ["queries"],
                    },
                },
            },
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    ],
)
async def test_chat_client_rejects_malformed_responses(payload: object) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = LlamaCppChatClient(
        base_url="http://127.0.0.1:8085",
        model_id="chat-model",
        parameters=_parameters(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelContractError):
        await client.complete(
            ChatRequest(messages=(ChatMessage(role="user", content="Return JSON"),), max_tokens=16)
        )


@pytest.mark.anyio
async def test_embedding_client_orders_and_validates_exact_dimension() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 8},
            },
        )

    client = LlamaCppEmbeddingClient(
        base_url="http://127.0.0.1:8086",
        model_id="embedding-model",
        dimension=3,
        transport=httpx.MockTransport(handler),
    )

    result = await client.embed(("first problem", "second problem"))

    assert result.vectors == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert result.input_tokens == 8
    assert requests == [
        {
            "model": "embedding-model",
            "input": ["first problem", "second problem"],
            "encoding_format": "float",
        }
    ]


@pytest.mark.anyio
async def test_embedding_client_rejects_wrong_dimension_and_nonfinite_values() -> None:
    responses = iter(
        (
            {"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
            {"data": [{"index": 0, "embedding": [1.0, float("nan"), 0.0]}]},
        )
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(next(responses), allow_nan=True).encode(),
            headers={"content-type": "application/json"},
        )

    client = LlamaCppEmbeddingClient(
        base_url="http://127.0.0.1:8086",
        model_id="embedding-model",
        dimension=3,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelContractError, match="dimension"):
        await client.embed(("problem",))
    with pytest.raises(ModelContractError, match="dimension"):
        await client.embed(("problem",))
