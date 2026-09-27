from __future__ import annotations

import pytest
from tests.fakes.models import FakeChatClient, FakeEmbeddingClient

from pensae.infrastructure.models import (
    ChatClient,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingClient,
    EmbeddingResponse,
)


@pytest.mark.anyio
async def test_fake_chat_is_typed_deterministic_and_records_requests() -> None:
    response = ChatResponse(content='{"ok":true}', prompt_tokens=4, completion_tokens=3)
    fake = FakeChatClient((response,))
    client: ChatClient = fake
    request = ChatRequest(
        messages=(ChatMessage(role="user", content="Return JSON"),),
        max_tokens=16,
    )

    assert await client.complete(request) == response
    assert fake.requests == [request]
    with pytest.raises(AssertionError, match="exhausted"):
        await client.complete(request)


@pytest.mark.anyio
async def test_fake_embedding_is_typed_deterministic_and_records_requests() -> None:
    response = EmbeddingResponse(vectors=((1.0, 0.0),), input_tokens=2)
    fake = FakeEmbeddingClient((response,))
    client: EmbeddingClient = fake

    assert await client.embed(("problem statement",)) == response
    assert fake.requests == [("problem statement",)]
    with pytest.raises(AssertionError, match="exhausted"):
        await client.embed(("second",))
