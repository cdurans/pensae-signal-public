"""Direct typed HTTPX clients for the protected llama.cpp APIs."""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx

from .contracts import ChatMessage, ChatRequest, ChatResponse, EmbeddingResponse


class ModelContractError(RuntimeError):
    """The local endpoint returned data outside the protected API contract."""


@dataclass(frozen=True, slots=True)
class ChatParameters:
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    presence_penalty: float
    repetition_penalty: float


class LlamaCppChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        parameters: ChatParameters,
        timeout_seconds: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._parameters = parameters
        self._timeout = timeout_seconds
        self._transport = transport

    async def count_tokens(self, messages: tuple[ChatMessage, ...]) -> int:
        if not messages:
            raise ValueError("token count requires messages")
        serialized = "\n".join(f"{message.role}: {message.content}" for message in messages)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/tokenize",
                    json={"content": serialized, "add_special": True, "with_pieces": False},
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelContractError("local chat token count failed") from exc
        tokens = body.get("tokens") if isinstance(body, dict) else None
        if not isinstance(tokens, list) or any(not isinstance(token, int) for token in tokens):
            raise ModelContractError("local chat token count response is malformed")
        return len(tokens)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if request.max_tokens <= 0:
            raise ValueError("chat output cap must be positive")
        if not request.messages:
            raise ValueError("chat request requires messages")
        payload: dict[str, object] = {
            "model": self._model_id,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "stream": False,
            "max_tokens": request.max_tokens,
            "seed": request.seed,
            "temperature": self._parameters.temperature,
            "top_p": self._parameters.top_p,
            "top_k": self._parameters.top_k,
            "min_p": self._parameters.min_p,
            "presence_penalty": self._parameters.presence_penalty,
            "repeat_penalty": self._parameters.repetition_penalty,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": dict(request.response_schema),
                },
            }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelContractError("local chat request failed") from exc
        try:
            content = body["choices"][0]["message"]["content"]
            prompt_tokens = body["usage"]["prompt_tokens"]
            completion_tokens = body["usage"]["completion_tokens"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelContractError("local chat response is malformed") from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelContractError("local chat response content is empty")
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            raise ModelContractError("local chat token usage is malformed")
        return ChatResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


class LlamaCppEmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        dimension: int,
        timeout_seconds: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._dimension = dimension
        self._timeout = timeout_seconds
        self._transport = transport

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResponse:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("embedding input must contain nonempty text")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/v1/embeddings",
                    json={
                        "model": self._model_id,
                        "input": list(texts),
                        "encoding_format": "float",
                    },
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelContractError("local embedding request failed") from exc
        raw_data = body.get("data") if isinstance(body, dict) else None
        raw_usage = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(raw_data, list) or len(raw_data) != len(texts):
            raise ModelContractError("local embedding response count is incompatible")
        indexed: dict[int, tuple[float, ...]] = {}
        for item in raw_data:
            if not isinstance(item, dict):
                raise ModelContractError("local embedding response item is malformed")
            index = item.get("index")
            vector = item.get("embedding")
            if not isinstance(index, int) or not isinstance(vector, list):
                raise ModelContractError("local embedding response item is malformed")
            if len(vector) != self._dimension or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in vector
            ):
                raise ModelContractError("local embedding dimension or values are incompatible")
            indexed[index] = tuple(float(value) for value in vector)
        if tuple(sorted(indexed)) != tuple(range(len(texts))):
            raise ModelContractError("local embedding indexes are incompatible")
        input_tokens = raw_usage.get("prompt_tokens", 0) if isinstance(raw_usage, dict) else 0
        if not isinstance(input_tokens, int):
            raise ModelContractError("local embedding token usage is malformed")
        return EmbeddingResponse(
            vectors=tuple(indexed[index] for index in range(len(texts))),
            input_tokens=input_tokens,
        )
