from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import httpx

from .health_contract import LlamaHealthState, classify_llama_health
from .supervisor import EndpointProbe
from .types import EndpointObservation, EndpointState, LaunchSpec, ModelRole


class HttpxEndpointProbe(EndpointProbe):
    """Bounded llama.cpp health, identity, API, and capability inspection."""

    def __init__(
        self,
        *,
        embedding_dimension: int,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if embedding_dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        if timeout_seconds <= 0:
            raise ValueError("endpoint timeout must be positive")
        self._embedding_dimension = embedding_dimension
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def inspect(self, spec: LaunchSpec) -> EndpointObservation:
        base_url = f"http://127.0.0.1:{spec.port}"
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=self._timeout,
                trust_env=False,
                transport=self._transport,
            ) as client:
                health = await client.get("/health")
        except httpx.ConnectError:
            return EndpointObservation(
                state=EndpointState.FREE,
                detail=f"{spec.role.value} endpoint port {spec.port} is free",
            )
        except httpx.HTTPError:
            return EndpointObservation(
                state=EndpointState.UNKNOWN_LISTENER,
                detail=(
                    f"port {spec.port} has a listener that did not complete the bounded "
                    "llama.cpp health contract; no signal was sent"
                ),
            )
        health_state = classify_llama_health(health)
        if health_state is LlamaHealthState.STARTING:
            return EndpointObservation(
                state=EndpointState.STARTING,
                detail=f"protected {spec.role.value} llama.cpp model is loading",
            )
        if health_state is not LlamaHealthState.READY:
            return EndpointObservation(
                state=EndpointState.UNKNOWN_LISTENER,
                detail=(f"port {spec.port} is occupied by an unknown listener; no signal was sent"),
            )
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=self._timeout,
                trust_env=False,
                transport=self._transport,
            ) as client:
                models = await client.get("/v1/models")
                models.raise_for_status()
                payload = models.json()
                if not isinstance(payload, Mapping) or spec.expected_model not in _model_ids(
                    payload
                ):
                    return EndpointObservation(
                        state=EndpointState.INCOMPATIBLE,
                        detail=(
                            f"port {spec.port} serves llama.cpp but not protected model "
                            f"{spec.expected_model}; no signal was sent"
                        ),
                    )
                compatible = (
                    await self._chat_capability(client, spec)
                    if spec.role is ModelRole.CHAT
                    else await self._embedding_capability(client, spec)
                )
        except (httpx.HTTPError, ValueError, TypeError):
            compatible = False
        if not compatible:
            return EndpointObservation(
                state=EndpointState.INCOMPATIBLE,
                detail=(
                    f"port {spec.port} failed the {spec.role.value} capability contract; "
                    "no signal was sent"
                ),
            )
        return EndpointObservation(
            state=EndpointState.COMPATIBLE,
            detail=(
                f"compatible {spec.role.value} endpoint serves {spec.expected_model} "
                f"on 127.0.0.1:{spec.port}"
            ),
        )

    async def _chat_capability(self, client: httpx.AsyncClient, spec: LaunchSpec) -> bool:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": spec.expected_model,
                "messages": [{"role": "user", "content": 'Return {"ok":true}.'}],
                "max_tokens": 16,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        return _structured_chat_ok(response)

    async def _embedding_capability(self, client: httpx.AsyncClient, spec: LaunchSpec) -> bool:
        response = await client.post(
            "/v1/embeddings",
            json={"model": spec.expected_model, "input": ["Pensae capability probe"]},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            return False
        data = payload["data"]
        if not data or not isinstance(data[0], Mapping):
            return False
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or len(vector) != self._embedding_dimension:
            return False
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        return math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3)


def _structured_chat_ok(response: httpx.Response) -> bool:
    payload = response.json()
    if not isinstance(payload, Mapping):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return False
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        return False
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    parsed = json.loads(content)
    return isinstance(parsed, Mapping) and parsed.get("ok") is True


def _model_ids(payload: Mapping[str, Any]) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return set()
    return {
        str(item["id"])
        for item in data
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
