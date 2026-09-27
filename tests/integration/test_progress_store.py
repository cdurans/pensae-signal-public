from __future__ import annotations

import os
from typing import Any, cast
from uuid import uuid4

import pytest
import redis.asyncio as redis

from pensae.config.protected import ProtectedConfig
from pensae.infrastructure.progress import RedisProgressStore
from pensae.research.workflow import WorkCounters
from pensae.runs.control import ProgressEvent


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


@pytest.mark.integration
@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_real_redis_stream_is_bounded_expiring_and_replayable() -> None:
    client = redis.from_url(os.environ["PENSAE_REDIS_URL"], decode_responses=True)
    bounds = ProtectedConfig.load().research.bounds
    store = RedisProgressStore(cast(Any, client), bounds)
    run_id = uuid4()
    stream_key = f"pensae:run:{run_id}:events"
    cancel_key = f"pensae:run:{run_id}:cancel"
    try:
        await store.initialize(run_id)
        event_ids = []
        for index in range(bounds.redis_stream_events + 2):
            event_ids.append(
                await store.publish(
                    ProgressEvent(
                        run_id=run_id,
                        kind="stage_completed",
                        state="running",
                        stage=f"stage_{index}",
                        counters=WorkCounters(queries=1),
                        committed_count=0,
                    )
                )
            )
        assert await client.xlen(stream_key) == bounds.redis_stream_events
        assert 0 < await client.ttl(stream_key) <= bounds.redis_active_ttl_seconds
        assert 0 < await client.ttl(cancel_key) <= bounds.redis_active_ttl_seconds

        trimmed = await store.replay(run_id, event_ids[0])
        assert trimmed.requires_snapshot is True
        latest = await store.replay(run_id, event_ids[-2])
        assert tuple(item.event_id for item in latest.records) == (event_ids[-1],)

        await store.request_stop(run_id)
        assert await store.is_stop_requested(run_id) is True
        await store.publish(
            ProgressEvent(
                run_id=run_id,
                kind="terminal",
                state="stopped",
                stage="terminal_cleanup",
                counters=WorkCounters(queries=1),
                committed_count=0,
            )
        )
        assert 0 < await client.ttl(stream_key) <= bounds.redis_terminal_ttl_seconds
        assert 0 < await client.ttl(cancel_key) <= bounds.redis_terminal_ttl_seconds
    finally:
        await client.delete(stream_key, cancel_key)
        await client.aclose()
