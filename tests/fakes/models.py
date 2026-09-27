from __future__ import annotations

from collections import deque

from pensae.infrastructure.models import ChatMessage, ChatRequest, ChatResponse, EmbeddingResponse


class FakeChatClient:
    def __init__(
        self,
        responses: tuple[ChatResponse, ...],
        *,
        token_counts: tuple[int, ...] = (),
    ) -> None:
        self._responses = deque(responses)
        self._token_counts = deque(token_counts)
        self.requests: list[ChatRequest] = []
        self.token_requests: list[tuple[ChatMessage, ...]] = []

    async def count_tokens(self, messages: tuple[ChatMessage, ...]) -> int:
        self.token_requests.append(messages)
        if self._token_counts:
            return self._token_counts.popleft()
        return sum(len(str(message)) for message in messages) // 4 + 1

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("fake chat response script is exhausted")
        return self._responses.popleft()


class FakeEmbeddingClient:
    def __init__(self, responses: tuple[EmbeddingResponse, ...]) -> None:
        self._responses = deque(responses)
        self.requests: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
        self.requests.append(texts)
        if not self._responses:
            raise AssertionError("fake embedding response script is exhausted")
        return self._responses.popleft()
