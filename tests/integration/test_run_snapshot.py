from __future__ import annotations

import os
from dataclasses import asdict

import pytest
from tests.fakes.opportunities import fake_opportunity_aggregate, fake_run_snapshot

from pensae.infrastructure.db import create_engine, create_session_factory
from pensae.opportunities import OpportunityAggregateStore
from pensae.opportunities.records import persisted_run_counters, persisted_run_warnings
from pensae.research.workflow import ModelCallUsage, NonCountingOutcomes


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


@pytest.mark.integration
@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_authoritative_run_snapshot_persists_stopping_and_bounded_progress() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    store = OpportunityAggregateStore(create_session_factory(engine))
    snapshot = fake_run_snapshot()
    try:
        await store.create_run(snapshot)
        await store.update_run_progress(
            snapshot.id,
            state="stopping",
            stage="focused_retrieval",
            counters=persisted_run_counters(
                {"queries": 11, "retrieved_pages": 25, "total_tokens": 2048},
                target_count=5,
                admitted_count=3,
                evaluated_count=2,
                achieved_count=1,
                non_counting_outcomes=asdict(NonCountingOutcomes(invalid_candidate=1)),
            ),
            warning_codes=persisted_run_warnings(
                ("source_unavailable",),
                shortfall_code="insufficient_evidence",
                limit_code="model_calls",
                limit_stage="final_analysis",
            ),
            committed_count=1,
            model_usage=(
                ModelCallUsage(
                    model="chat",
                    role="problem_analyst",
                    call_index=1,
                    attempts=1,
                    input_tokens=120,
                    output_tokens=42,
                ),
            ),
        )

        detail = await store.get_run(snapshot.id)

        assert detail is not None
        assert detail.state == "stopping"
        assert detail.current_stage == "focused_retrieval"
        assert detail.work_counters == {
            "queries": 11,
            "retrieved_pages": 25,
            "total_tokens": 2048,
        }
        assert detail.warning_codes == ("source_unavailable",)
        assert detail.committed_count == 1
        assert detail.target_count == 5
        assert detail.admitted_count == 3
        assert detail.evaluated_count == 2
        assert detail.achieved_count == 1
        assert detail.non_counting_outcomes.invalid_candidate == 1
        assert detail.shortfall_code == "insufficient_evidence"
        assert detail.limit_code == "model_calls"
        assert detail.limit_stage == "final_analysis"
        assert detail.model_usage[0].role == "problem_analyst"
        assert detail.model_usage[0].input_tokens == 120
        assert detail.updated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_run_snapshot_lists_all_commits_in_deterministic_commit_order() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    store = OpportunityAggregateStore(create_session_factory(engine))
    snapshot = fake_run_snapshot()
    try:
        await store.create_run(snapshot)
        aggregates = tuple(fake_opportunity_aggregate(snapshot.id) for _ in range(5))
        for aggregate in aggregates:
            await store.commit(aggregate)
        await store.update_run_progress(
            snapshot.id,
            state="completed",
            stage="terminal_cleanup",
            counters=persisted_run_counters(
                {"opportunities": 5},
                target_count=5,
                admitted_count=5,
                evaluated_count=5,
                achieved_count=5,
                non_counting_outcomes=asdict(NonCountingOutcomes()),
            ),
            warning_codes=(),
            committed_count=5,
        )

        detail = await store.get_run(snapshot.id)

        assert detail is not None
        expected_ids = tuple(aggregate.opportunity_id for aggregate in aggregates)
        assert detail.committed_opportunity_ids == expected_ids
        assert detail.opportunity_id == expected_ids[-1]
    finally:
        await engine.dispose()
