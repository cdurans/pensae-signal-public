from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.fakes.opportunities import fake_opportunity_aggregate, fake_run_snapshot

from pensae.infrastructure.db import create_engine, create_session_factory, schema
from pensae.opportunities import OpportunityAggregateStore, OpportunityLifecycleService


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


@pytest.mark.integration
@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_startup_reconciles_abandoned_mixed_commits_and_deletes_empty_run() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    lifecycle = OpportunityLifecycleService(session_factory)
    committed_snapshot = fake_run_snapshot()
    empty_snapshot = fake_run_snapshot()
    counted_aggregate = fake_opportunity_aggregate(committed_snapshot.id)
    non_counted_aggregate = fake_opportunity_aggregate(committed_snapshot.id).model_copy(
        update={
            "relation_id": uuid4(),
            "related_opportunity_id": counted_aggregate.opportunity_id,
            "relation_similarity": Decimal("0.90"),
            "classification": "possible_rediscovery",
        }
    )
    try:
        await store.create_run(committed_snapshot)
        # Both opportunity transactions commit, but no progress-observer update follows.
        await store.commit(counted_aggregate)
        await store.commit(non_counted_aggregate)
        await store.create_run(empty_snapshot)

        async with session_factory() as session:
            stale_row = (
                await session.execute(
                    sa.select(
                        schema.runs.c.committed_count,
                        schema.runs.c.work_counters,
                    ).where(schema.runs.c.id == committed_snapshot.id)
                )
            ).one()
        assert stale_row.committed_count == 0
        assert stale_row.work_counters == {}

        reconciled_before_restart = await store.get_run(committed_snapshot.id)
        assert reconciled_before_restart is not None
        assert reconciled_before_restart.committed_count == 2
        assert reconciled_before_restart.achieved_count == 1
        assert reconciled_before_restart.committed_opportunity_ids == (
            counted_aggregate.opportunity_id,
            non_counted_aggregate.opportunity_id,
        )
        assert reconciled_before_restart.non_counting_outcomes.unresolved_possible_rediscovery == 1

        orphan_source = uuid4()
        orphan_evidence = uuid4()
        orphan_signal = uuid4()
        orphan_pattern = uuid4()
        async with session_factory() as session, session.begin():
            await session.execute(
                sa.insert(schema.sources).values(
                    id=orphan_source,
                    url="https://example.test/transient",
                    title="Transient source",
                    publisher=None,
                    publication_date=None,
                    retrieved_at=datetime.now(UTC),
                    credibility_note="startup cleanup fixture",
                    limitation=None,
                    content_fingerprint="a" * 64,
                )
            )
            await session.execute(
                sa.insert(schema.evidence_items).values(
                    id=orphan_evidence,
                    source_id=orphan_source,
                    excerpt="Mechanically bounded transient evidence.",
                    supported_claim="Transient claim",
                    evidence_kind="supporting",
                )
            )
            await session.execute(
                sa.insert(schema.problem_signals).values(
                    id=orphan_signal,
                    affected_user="Operator",
                    recurring_workflow="Restart cleanup",
                    current_workaround="Manual cleanup",
                    business_consequence="Transient rows remain",
                    confidence=Decimal("0.500"),
                )
            )
            await session.execute(
                sa.insert(schema.problem_signal_evidence).values(
                    signal_id=orphan_signal, evidence_id=orphan_evidence
                )
            )
            await session.execute(
                sa.insert(schema.problem_patterns).values(
                    id=orphan_pattern,
                    summary="Transient startup pattern",
                    fingerprint="b" * 64,
                    embedding=[0.0] * 1024,
                    embedding_model_id="integration-fixture",
                    fingerprint_version="v1",
                )
            )
            await session.execute(
                sa.insert(schema.problem_pattern_signals).values(
                    pattern_id=orphan_pattern, signal_id=orphan_signal
                )
            )

        result = await store.resolve_abandoned_runs()
        swept = await lifecycle.sweep_orphan_support()

        assert committed_snapshot.id in result.stopped_run_ids
        assert empty_snapshot.id in result.deleted_run_ids
        committed = await store.get_run(committed_snapshot.id)
        assert committed is not None
        assert committed.state == "stopped"
        assert committed.current_stage == "startup_cleanup"
        assert committed.warning_codes == ("application_restart",)
        assert committed.opportunity_id == non_counted_aggregate.opportunity_id
        assert committed.committed_count == 2
        assert committed.achieved_count == 1
        assert committed.committed_opportunity_ids == (
            counted_aggregate.opportunity_id,
            non_counted_aggregate.opportunity_id,
        )
        assert committed.non_counting_outcomes.unresolved_possible_rediscovery == 1
        assert await store.get_run(empty_snapshot.id) is None
        assert await store.get_detail(counted_aggregate.opportunity_id) is not None
        assert await store.get_detail(non_counted_aggregate.opportunity_id) is not None
        assert swept.total == 4
        async with session_factory() as session:
            persisted = (
                await session.execute(
                    sa.select(
                        schema.runs.c.committed_count,
                        schema.runs.c.work_counters,
                    ).where(schema.runs.c.id == committed_snapshot.id)
                )
            ).one()
        assert persisted.committed_count == 2
        assert persisted.work_counters["p7_achieved_count"] == 1
        assert persisted.work_counters["p7_unresolved_possible_rediscovery"] == 1
        async with session_factory() as session:
            for table, record_id in (
                (schema.sources, orphan_source),
                (schema.evidence_items, orphan_evidence),
                (schema.problem_signals, orphan_signal),
                (schema.problem_patterns, orphan_pattern),
            ):
                remaining = await session.scalar(
                    sa.select(table.c.id).where(table.c.id == record_id)
                )
                assert remaining is None
    finally:
        await engine.dispose()
