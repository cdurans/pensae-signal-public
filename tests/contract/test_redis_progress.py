from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

import pytest

from pensae.config.protected import ProtectedConfig
from pensae.infrastructure.progress import RedisProgressStore
from pensae.research.workflow import WorkCounters
from pensae.runs.control import ProgressEvent, ProgressUnavailable


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.expirations: dict[str, int] = {}
        self.fail = False

    async def delete(self, *names: str) -> int:
        self._check()
        for name in names:
            self.values.pop(name, None)
            self.streams.pop(name, None)
        return len(names)

    async def set(self, name: str, value: str, *, ex: int) -> bool:
        self._check()
        self.values[name] = value
        self.expirations[name] = ex
        return True

    async def get(self, name: str) -> str | None:
        self._check()
        return self.values.get(name)

    async def expire(self, name: str, time: int) -> bool:
        self._check()
        self.expirations[name] = time
        return True

    async def xadd(
        self,
        name: str,
        fields: Mapping[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> str:
        self._check()
        assert approximate is False
        stream = self.streams.setdefault(name, [])
        event_id = f"{len(stream) + 1}-0"
        stream.append((event_id, dict(fields)))
        if len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return event_id

    async def xrange(
        self, name: str, min: str = "-", max: str = "+"
    ) -> Sequence[tuple[str, Mapping[str, str]]]:
        del min, max
        self._check()
        return list(self.streams.get(name, []))

    def _check(self) -> None:
        if self.fail:
            raise OSError("fake Redis unavailable")


def _event(run_id: UUID, stage: str) -> ProgressEvent:
    return ProgressEvent(
        run_id=run_id,
        kind="stage_completed",
        state="running",
        stage=stage,
        counters=WorkCounters(queries=1),
        committed_count=0,
    )


@pytest.mark.anyio
async def test_stream_is_exactly_bounded_and_keys_expire() -> None:
    fake = FakeRedis()
    bounds = ProtectedConfig.load().research.bounds
    store = RedisProgressStore(fake, bounds)
    run_id = UUID(int=1)
    await store.initialize(run_id)

    for index in range(bounds.redis_stream_events + 3):
        await store.publish(_event(run_id, f"stage_{index}"))

    stream_key = f"pensae:run:{run_id}:events"
    cancel_key = f"pensae:run:{run_id}:cancel"
    assert len(fake.streams[stream_key]) == bounds.redis_stream_events
    assert fake.expirations[stream_key] == bounds.redis_active_ttl_seconds
    assert fake.expirations[cancel_key] == bounds.redis_active_ttl_seconds

    await store.publish(
        ProgressEvent(
            run_id=run_id,
            kind="terminal",
            state="completed",
            stage="terminal_cleanup",
            counters=WorkCounters(),
            committed_count=0,
        )
    )
    assert fake.expirations[stream_key] == bounds.redis_terminal_ttl_seconds
    assert fake.expirations[cancel_key] == bounds.redis_terminal_ttl_seconds


@pytest.mark.anyio
async def test_replay_after_known_id_and_trim_gap_requires_snapshot() -> None:
    fake = FakeRedis()
    bounds = ProtectedConfig.load().research.bounds
    store = RedisProgressStore(fake, bounds)
    run_id = UUID(int=2)
    await store.initialize(run_id)
    ids = [await store.publish(_event(run_id, f"stage_{index}")) for index in range(3)]

    replay = await store.replay(run_id, ids[0])
    assert tuple(item.event_id for item in replay.records) == tuple(ids[1:])
    assert replay.requires_snapshot is False

    fake.streams[f"pensae:run:{run_id}:events"] = fake.streams[f"pensae:run:{run_id}:events"][1:]
    gap = await store.replay(run_id, ids[0])
    assert gap.records == ()
    assert gap.requires_snapshot is True

    future = await store.replay(run_id, "999999-0")
    assert future.records == ()
    assert future.requires_snapshot is True


@pytest.mark.anyio
async def test_cancel_state_is_strict_and_redis_failure_fails_closed() -> None:
    fake = FakeRedis()
    store = RedisProgressStore(fake, ProtectedConfig.load().research.bounds)
    run_id = UUID(int=3)
    await store.initialize(run_id)
    assert await store.is_stop_requested(run_id) is False
    await store.request_stop(run_id)
    assert await store.is_stop_requested(run_id) is True

    fake.values[f"pensae:run:{run_id}:cancel"] = "unexpected"
    with pytest.raises(ProgressUnavailable, match="malformed"):
        await store.is_stop_requested(run_id)
    fake.fail = True
    with pytest.raises(ProgressUnavailable, match="replay"):
        await store.replay(run_id, None)


@pytest.mark.anyio
async def test_malformed_stream_payload_is_never_forwarded() -> None:
    fake = FakeRedis()
    store = RedisProgressStore(fake, ProtectedConfig.load().research.bounds)
    run_id = UUID(int=4)
    fake.streams[f"pensae:run:{run_id}:events"] = [("1-0", {"event": '{"url":"x"}'})]

    with pytest.raises(ProgressUnavailable, match="malformed"):
        await store.replay(run_id, None)
