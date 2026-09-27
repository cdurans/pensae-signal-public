from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import httpx
import psycopg
import redis.asyncio as redis

from pensae.config.settings import BootstrapSettings
from pensae.domain.health import DependencyHealth, DependencyName, HealthState
from pensae.infrastructure.health.service import HealthProbe, HealthService
from pensae.infrastructure.model_runtime.health_contract import (
    LlamaHealthState,
    classify_llama_health,
)
from pensae.infrastructure.search import SEARCH_ENGINES

_SEARXNG_PROBE_TIMEOUT_SECONDS = 15
_LIVE_HEALTH_TIMEOUT_SECONDS = 20


def _failed(
    dependency: DependencyName,
    summary: str,
    action: str,
    *,
    incompatible: bool = False,
    unknown_listener: bool = False,
) -> DependencyHealth:
    state = HealthState.UNAVAILABLE
    if incompatible:
        state = HealthState.INCOMPATIBLE
    if unknown_listener:
        state = HealthState.UNKNOWN_LISTENER
    return DependencyHealth(
        dependency=dependency,
        state=state,
        summary=summary,
        action=action,
    )


class PostgresProbe:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    async def check(self) -> DependencyHealth:
        try:
            async with (
                await psycopg.AsyncConnection.connect(
                    self._dsn,
                    connect_timeout=2,
                ) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
                )
                row = await cursor.fetchone()
        except (psycopg.Error, OSError):
            return _failed(
                DependencyName.POSTGRESQL,
                "PostgreSQL is unavailable on Fedora loopback",
                "Run `make infra-up`, then apply the empty migration.",
            )
        if row is None or row[0] is not True:
            return _failed(
                DependencyName.POSTGRESQL,
                "PostgreSQL is reachable but pgvector is not enabled",
                "Run `uv run alembic upgrade head` against the local Pensae Signal database.",
                incompatible=True,
            )
        return DependencyHealth(
            dependency=DependencyName.POSTGRESQL,
            state=HealthState.READY,
            summary="PostgreSQL and pgvector are ready",
        )


class RedisProbe:
    def __init__(self, url: str) -> None:
        self._url = url

    async def check(self) -> DependencyHealth:
        client = redis.from_url(self._url, socket_connect_timeout=2, socket_timeout=2)
        try:
            pong = await client.ping()
        except (redis.RedisError, OSError):
            return _failed(
                DependencyName.REDIS,
                "Redis is unavailable on Fedora loopback",
                "Run `make infra-up` and inspect the Redis health check.",
            )
        finally:
            await client.aclose()
        if pong is not True:
            return _failed(
                DependencyName.REDIS,
                "Redis returned an incompatible PING response",
                "Verify the pinned Redis service configuration.",
                incompatible=True,
            )
        return DependencyHealth(
            dependency=DependencyName.REDIS,
            state=HealthState.READY,
            summary="Redis transient control is ready",
        )


class SearxngProbe:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def check(self) -> DependencyHealth:
        try:
            async with httpx.AsyncClient(
                timeout=_SEARXNG_PROBE_TIMEOUT_SECONDS,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/search",
                    params={"format": "json"},
                    data={
                        "q": "Pensae capability check",
                        "categories": "general",
                        "language": "en-US",
                        "safesearch": "1",
                        "engines": ",".join(SEARCH_ENGINES),
                    },
                )
        except httpx.ConnectError:
            return _failed(
                DependencyName.SEARXNG,
                "SearXNG is unavailable on Fedora loopback",
                "Run `make infra-up` and inspect the private SearXNG container health.",
            )
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return _failed(
                DependencyName.SEARXNG,
                "SearXNG does not satisfy the required JSON search contract",
                "Inspect the local `/search?format=json` configuration; there is no paid fallback.",
                incompatible=True,
            )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            return _failed(
                DependencyName.SEARXNG,
                "SearXNG returned an incompatible JSON search response",
                "Inspect the local SearXNG formats and engine configuration.",
                incompatible=True,
            )
        return DependencyHealth(
            dependency=DependencyName.SEARXNG,
            state=HealthState.READY,
            summary="Private SearXNG endpoint is ready",
        )


def _model_ids(payload: Mapping[str, Any]) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return set()
    return {
        str(item["id"])
        for item in data
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


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


class ModelCapabilityProbe:
    def __init__(
        self,
        *,
        dependency: DependencyName,
        base_url: str,
        expected_model: str,
        embedding_dimension: int = 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if dependency not in {DependencyName.CHAT, DependencyName.EMBEDDING}:
            raise ValueError("model capability probe requires chat or embedding dependency")
        self._dependency = dependency
        self._base_url = base_url.rstrip("/")
        self._expected_model = expected_model
        self._embedding_dimension = embedding_dimension
        self._transport = transport

    async def check(self) -> DependencyHealth:
        try:
            async with httpx.AsyncClient(
                timeout=10, trust_env=False, transport=self._transport
            ) as client:
                health = await client.get(f"{self._base_url}/health")
        except httpx.ConnectError:
            return _failed(
                self._dependency,
                f"{self._dependency.value} model endpoint is unavailable",
                "Use the Fedora launcher status; configure or start the protected endpoint.",
            )
        except httpx.HTTPError:
            return _failed(
                self._dependency,
                f"port for {self._dependency.value} has an unknown listener",
                "Inspect the listener manually; Pensae Signal will not signal an unknown process.",
                unknown_listener=True,
            )
        health_state = classify_llama_health(health)
        if health_state is LlamaHealthState.STARTING:
            return DependencyHealth(
                dependency=self._dependency,
                state=HealthState.STARTING,
                summary=f"{self._dependency.value} llama.cpp model is loading",
                action="Wait for the bounded Fedora launcher readiness deadline.",
            )
        if health_state is not LlamaHealthState.READY:
            return _failed(
                self._dependency,
                f"port for {self._dependency.value} has an unknown listener",
                "Inspect the listener manually; Pensae Signal will not signal an unknown process.",
                unknown_listener=True,
            )
        try:
            async with httpx.AsyncClient(
                timeout=10, trust_env=False, transport=self._transport
            ) as client:
                models = await client.get(f"{self._base_url}/v1/models")
                models.raise_for_status()
                model_payload = models.json()
                if not isinstance(model_payload, Mapping):
                    raise ValueError("model list is not an object")
                if self._expected_model not in _model_ids(model_payload):
                    return _failed(
                        self._dependency,
                        f"endpoint does not serve required model {self._expected_model}",
                        "Stop only a launcher-owned incompatible endpoint or correct "
                        "protected paths.",
                        incompatible=True,
                    )
                if self._dependency is DependencyName.CHAT:
                    capability_ok = await self._probe_chat(client)
                else:
                    capability_ok = await self._probe_embedding(client)
        except (httpx.HTTPError, ValueError, TypeError):
            return _failed(
                self._dependency,
                f"{self._dependency.value} llama.cpp endpoint is incompatible",
                "Verify the protected model identity, API, and role capability.",
                incompatible=True,
            )
        if not capability_ok:
            return _failed(
                self._dependency,
                f"{self._dependency.value} endpoint is reachable but incompatible",
                "Verify the pinned llama.cpp build, model identity, and role arguments.",
                incompatible=True,
            )
        return DependencyHealth(
            dependency=self._dependency,
            state=HealthState.READY,
            summary=f"{self._dependency.value} model capability is ready",
        )

    async def _probe_chat(self, client: httpx.AsyncClient) -> bool:
        response = await client.post(
            f"{self._base_url}/v1/chat/completions",
            json={
                "model": self._expected_model,
                "messages": [{"role": "user", "content": 'Return JSON: {"ok": true}'}],
                "max_tokens": 16,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        return _structured_chat_ok(response)

    async def _probe_embedding(self, client: httpx.AsyncClient) -> bool:
        response = await client.post(
            f"{self._base_url}/v1/embeddings",
            json={"model": self._expected_model, "input": ["Pensae capability probe"]},
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


def live_health_service(settings: BootstrapSettings) -> HealthService:
    probes: dict[DependencyName, HealthProbe] = {
        DependencyName.POSTGRESQL: PostgresProbe(settings.postgres_dsn),
        DependencyName.REDIS: RedisProbe(settings.redis_url),
        DependencyName.SEARXNG: SearxngProbe(settings.searxng_url),
        DependencyName.CHAT: ModelCapabilityProbe(
            dependency=DependencyName.CHAT,
            base_url=settings.chat_url,
            expected_model="Qwen3.6-35B-A3B-UD-IQ4_XS",
        ),
        DependencyName.EMBEDDING: ModelCapabilityProbe(
            dependency=DependencyName.EMBEDDING,
            base_url=settings.embedding_url,
            expected_model="Qwen3-Embedding-0.6B",
        ),
    }
    return HealthService(probes, timeout_seconds=_LIVE_HEALTH_TIMEOUT_SECONDS)
