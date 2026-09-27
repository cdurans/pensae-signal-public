"""Redis 7 bounded Stream implementation; never permanent business storage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

import redis.asyncio as redis
from pydantic import ValidationError

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.runs.control import (
    ProgressEvent,
    ProgressRecord,
    ProgressReplay,
    ProgressUnavailable,
)


class RedisStreamClient(Protocol):
    async def delete(self, *names: str) -> int: ...

    async def set(self, name: str, value: str, *, ex: int) -> object: ...

    async def get(self, name: str) -> object: ...

    async def expire(self, name: str, time: int) -> object: ...

    async def xadd(
        self,
        name: str,
        fields: Mapping[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> object: ...

    async def xrange(
        self, name: str, min: str = "-", max: str = "+"
    ) -> Sequence[tuple[str, Mapping[str, str]]]: ...


class RedisProgressStore:
    def __init__(self, client: RedisStreamClient, bounds: ProtectedWorkflowBounds) -> None:
        self._client = client
        self._bounds = bounds

    async def initialize(self, run_id: UUID) -> None:
        try:
            await self._client.delete(self._stream_key(run_id), self._cancel_key(run_id))
            await self._client.set(
                self._cancel_key(run_id),
                "0",
                ex=self._bounds.redis_cancellation_ttl_seconds,
            )
        except (redis.RedisError, OSError, TimeoutError) as exc:
            raise ProgressUnavailable("Redis initialization failed") from exc

    async def publish(self, event: ProgressEvent) -> str:
        try:
            raw_id = await self._client.xadd(
                self._stream_key(event.run_id),
                {"event": event.model_dump_json()},
                maxlen=self._bounds.redis_stream_events,
                approximate=False,
            )
            ttl = (
                self._bounds.redis_terminal_ttl_seconds
                if event.kind == "terminal"
                else self._bounds.redis_active_ttl_seconds
            )
            await self._client.expire(self._stream_key(event.run_id), ttl)
            await self._client.expire(self._cancel_key(event.run_id), ttl)
        except (redis.RedisError, OSError, TimeoutError) as exc:
            raise ProgressUnavailable("Redis progress publish failed") from exc
        event_id = _text(raw_id)
        _parse_event_id(event_id)
        return event_id

    async def request_stop(self, run_id: UUID) -> None:
        try:
            await self._client.set(
                self._cancel_key(run_id),
                "1",
                ex=self._bounds.redis_cancellation_ttl_seconds,
            )
        except (redis.RedisError, OSError, TimeoutError) as exc:
            raise ProgressUnavailable("Redis cancellation write failed") from exc

    async def is_stop_requested(self, run_id: UUID) -> bool:
        try:
            raw = await self._client.get(self._cancel_key(run_id))
        except (redis.RedisError, OSError, TimeoutError) as exc:
            raise ProgressUnavailable("Redis cancellation read failed") from exc
        if raw is None:
            raise ProgressUnavailable("Redis cancellation state is missing")
        value = _text(raw)
        if value not in {"0", "1"}:
            raise ProgressUnavailable("Redis cancellation state is malformed")
        return value == "1"

    async def replay(self, run_id: UUID, after_id: str | None) -> ProgressReplay:
        try:
            raw_records = await self._client.xrange(self._stream_key(run_id))
        except (redis.RedisError, OSError, TimeoutError) as exc:
            raise ProgressUnavailable("Redis progress replay failed") from exc
        try:
            records = tuple(self._decode_record(raw) for raw in raw_records)
        except (ValueError, ValidationError, TypeError) as exc:
            raise ProgressUnavailable("Redis progress stream is malformed") from exc
        if after_id is None:
            return ProgressReplay(records=records, requires_snapshot=False)
        requested = _parse_event_id(after_id)
        if not records:
            return ProgressReplay(records=(), requires_snapshot=True)
        for index, record in enumerate(records):
            if record.event_id == after_id:
                return ProgressReplay(records=records[index + 1 :], requires_snapshot=False)
        # Any non-present cursor is a trim, gap, stale epoch, or future/forged ID.
        # PostgreSQL is authoritative for every one of those cases.
        del requested
        return ProgressReplay(records=(), requires_snapshot=True)

    def _decode_record(self, raw: tuple[str, Mapping[str, str]]) -> ProgressRecord:
        raw_id, fields = raw
        event_id = _text(raw_id)
        _parse_event_id(event_id)
        payload = next(
            (_text(value) for key, value in fields.items() if _text(key) == "event"),
            None,
        )
        if payload is None:
            raise ValueError("progress record omitted event")
        return ProgressRecord(event_id=event_id, event=ProgressEvent.model_validate_json(payload))

    @staticmethod
    def _stream_key(run_id: UUID) -> str:
        return f"pensae:run:{run_id}:events"

    @staticmethod
    def _cancel_key(run_id: UUID) -> str:
        return f"pensae:run:{run_id}:cancel"


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("Redis text response has an unexpected type")


def _parse_event_id(value: str) -> tuple[int, int]:
    parts = value.split("-")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ValueError("invalid Redis Stream event ID")
    return int(parts[0]), int(parts[1])
