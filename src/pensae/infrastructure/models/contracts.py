from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    max_tokens: int
    schema_name: str = "structured_response"
    response_schema: Mapping[str, object] | None = None
    seed: int = 0


@dataclass(frozen=True, slots=True)
class ChatResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int


class ChatClient(Protocol):
    async def count_tokens(self, messages: tuple[ChatMessage, ...]) -> int: ...

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int


class EmbeddingClient(Protocol):
    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse: ...
