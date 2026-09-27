from __future__ import annotations

import math
import os
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes.opportunities import fake_opportunity_aggregate, fake_run_snapshot

from pensae.infrastructure.db import create_engine, create_session_factory, schema
from pensae.opportunities import OpportunityAggregateStore, OpportunityLifecycleService
from pensae.opportunities.records import OpportunityAggregate
from pensae.opportunities.writer import ImmutableSupportConflict

pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


async def _count_for_opportunity(
    session_factory: async_sessionmaker[AsyncSession], opportunity_id: UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(schema.opportunities)
                .where(schema.opportunities.c.id == opportunity_id)
            )
            or 0
        )


async def _count_ids(
    session_factory: async_sessionmaker[AsyncSession],
    table: sa.Table,
    identifiers: tuple[UUID, ...],
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count()).select_from(table).where(table.c.id.in_(identifiers))
            )
            or 0
        )


def _with_shared_support(
    aggregate: OpportunityAggregate,
    support: OpportunityAggregate,
) -> OpportunityAggregate:
    signals = tuple(
        signal.model_copy(update={"evidence_ids": source_signal.evidence_ids})
        for signal, source_signal in zip(aggregate.signals, support.signals, strict=True)
    )
    return aggregate.model_copy(
        update={
            "sources": support.sources,
            "evidence": support.evidence,
            "signals": signals,
            "report": support.report,
        }
    )


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_complete_aggregate_is_atomic_and_prior_commit_survives_each_failure() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    try:
        successful_snapshot = fake_run_snapshot()
        successful = fake_opportunity_aggregate(successful_snapshot.id)
        store = OpportunityAggregateStore(session_factory)
        await store.create_run(successful_snapshot)
        assert await store.commit(successful) == successful.opportunity_id
        await store.set_run_state(successful_snapshot.id, "completed")

        detail = await store.get_detail(successful.opportunity_id)
        assert detail is not None
        assert detail.report.opportunity_name == "Maintenance Request Triage Assistant"
        assert len(detail.evidence) == 4
        assert detail.scores["weighted"] == Decimal("3.90")
        assert detail.verdict == "promising"
        assert "normalized_text" not in str(detail.model_dump(mode="json"))

        similarity = await store.nearest_problem((1.0,) + (0.0,) * 1023)
        assert similarity is not None
        assert similarity.opportunity_id == successful.opportunity_id
        assert similarity.similarity == pytest.approx(1.0)
        assert await store.find_identity(successful.identity_fingerprint) == (
            successful.opportunity_id
        )
        assert await store.find_pattern_fingerprint(successful.pattern.fingerprint) == (
            successful.pattern.id
        )
        pattern_matches = await store.nearest_patterns(
            successful.pattern.embedding,
            limit=3,
            minimum_similarity=0.99,
        )
        assert pattern_matches[0].pattern_id == successful.pattern.id
        assert pattern_matches[0].similarity == pytest.approx(1.0)
        rediscovery_event_id = UUID(int=9000)
        await store.record_rediscovery(
            run_id=successful_snapshot.id,
            opportunity_id=successful.opportunity_id,
            lifecycle_event_id=rediscovery_event_id,
        )
        async with session_factory() as session:
            rediscovery = (
                await session.execute(
                    sa.select(
                        schema.lifecycle_events.c.event_type,
                        schema.lifecycle_events.c.event_data,
                    ).where(schema.lifecycle_events.c.id == rediscovery_event_id)
                )
            ).one()
        assert rediscovery.event_type == "rediscovered"
        assert rediscovery.event_data["run_id"] == str(successful_snapshot.id)
        detail = await store.get_detail(successful.opportunity_id)
        assert detail is not None
        assert detail.classification == "rediscovered"
        assert detail.revision == 2

        related_snapshot = fake_run_snapshot()
        related = fake_opportunity_aggregate(related_snapshot.id).model_copy(
            update={
                "relation_id": UUID(int=9001),
                "related_opportunity_id": successful.opportunity_id,
                "relation_similarity": Decimal("0.95"),
                "classification": "possible_rediscovery",
            }
        )
        await store.create_run(related_snapshot)
        await store.commit(related)
        async with session_factory() as session:
            relation_row = (
                await session.execute(
                    sa.select(
                        schema.opportunity_relations.c.to_opportunity_id,
                        schema.opportunity_relations.c.relation_kind,
                        schema.lifecycle_events.c.event_type,
                    ).join(
                        schema.lifecycle_events,
                        schema.lifecycle_events.c.opportunity_id
                        == schema.opportunity_relations.c.from_opportunity_id,
                    )
                )
            ).one()
        assert relation_row == (
            successful.opportunity_id,
            "possible_rediscovery",
            "possible_rediscovery",
        )

        for failure_step in (
            "sources",
            "evidence",
            "signals",
            "pattern",
            "opportunity",
            "version",
            "joins",
            "event_and_current_version",
        ):
            failed_snapshot = fake_run_snapshot()
            failed = fake_opportunity_aggregate(failed_snapshot.id)
            await store.create_run(failed_snapshot)

            def fail_at(step: str, *, expected: str = failure_step) -> None:
                if step == expected:
                    raise RuntimeError(f"injected failure after {step}")

            failing_store = OpportunityAggregateStore(session_factory, after_step=fail_at)
            with pytest.raises(RuntimeError, match="injected failure"):
                await failing_store.commit(failed)
            assert await _count_for_opportunity(session_factory, failed.opportunity_id) == 0
            assert await store.get_detail(successful.opportunity_id) == detail
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_exact_similarity_returns_three_strongest_including_current_run_commits() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    store = OpportunityAggregateStore(create_session_factory(engine))
    snapshot = fake_run_snapshot()
    expected: list[tuple[UUID, float]] = []
    try:
        await store.create_run(snapshot)
        for similarity in (0.99, 0.90, 0.80, 0.70):
            aggregate = fake_opportunity_aggregate(snapshot.id)
            vector = (
                0.0,
                similarity,
                math.sqrt(1 - similarity**2),
                *((0.0,) * 1021),
            )
            aggregate = aggregate.model_copy(update={"opportunity_embedding": vector})
            await store.commit(aggregate)
            expected.append((aggregate.opportunity_id, similarity))

        query = (0.0, 1.0) + (0.0,) * 1022
        matches = await store.nearest_problems(
            query,
            limit=3,
            minimum_similarity=0.75,
        )

        assert [item.opportunity_id for item in matches] == [item[0] for item in expected[:3]]
        assert [item.similarity for item in matches] == pytest.approx(
            [item[1] for item in expected[:3]]
        )
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_empty_run_deletion_is_guarded_by_committed_provenance() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    store = OpportunityAggregateStore(create_session_factory(engine))
    empty = fake_run_snapshot()
    retained = fake_run_snapshot()
    aggregate = fake_opportunity_aggregate(retained.id)
    try:
        await store.create_run(empty)
        assert await store.delete_empty_run(empty.id) is True
        assert await store.get_run(empty.id) is None

        await store.create_run(retained)
        await store.commit(aggregate)
        assert await store.delete_empty_run(retained.id) is False
        assert await store.get_run(retained.id) is not None
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_five_commits_reuse_shared_support_and_reject_conflicting_ids_atomically() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    lifecycle = OpportunityLifecycleService(session_factory)
    snapshot = fake_run_snapshot()
    first = fake_opportunity_aggregate(snapshot.id)
    aggregates = (
        first,
        *(
            _with_shared_support(fake_opportunity_aggregate(snapshot.id), first)
            for _index in range(4)
        ),
    )
    source_ids = tuple(item.id for item in first.sources)
    evidence_ids = tuple(item.id for item in first.evidence)
    try:
        await store.create_run(snapshot)
        for aggregate in aggregates:
            await store.commit(aggregate)

        detail = await store.get_run(snapshot.id)
        assert detail is not None
        assert detail.committed_opportunity_ids == tuple(
            aggregate.opportunity_id for aggregate in aggregates
        )
        assert await _count_ids(session_factory, schema.sources, source_ids) == len(source_ids)
        assert await _count_ids(session_factory, schema.evidence_items, evidence_ids) == len(
            evidence_ids
        )

        for conflict_kind in ("source", "evidence"):
            candidate = _with_shared_support(fake_opportunity_aggregate(snapshot.id), first)
            if conflict_kind == "source":
                candidate = candidate.model_copy(
                    update={
                        "sources": (
                            candidate.sources[0].model_copy(update={"title": "Conflicting title"}),
                            *candidate.sources[1:],
                        )
                    }
                )
            else:
                candidate = candidate.model_copy(
                    update={
                        "evidence": (
                            candidate.evidence[0].model_copy(
                                update={"excerpt": "Conflicting excerpt"}
                            ),
                            *candidate.evidence[1:],
                        )
                    }
                )
            with pytest.raises(ImmutableSupportConflict, match=conflict_kind):
                await store.commit(candidate)
            assert await _count_for_opportunity(session_factory, candidate.opportunity_id) == 0

        detail = await store.get_run(snapshot.id)
        assert detail is not None
        assert detail.committed_opportunity_ids == tuple(
            aggregate.opportunity_id for aggregate in aggregates
        )

        for aggregate in aggregates[:-1]:
            await lifecycle.delete_permanently(aggregate.opportunity_id, expected_revision=1)
            assert await _count_ids(session_factory, schema.sources, source_ids) == len(source_ids)
            assert await _count_ids(session_factory, schema.evidence_items, evidence_ids) == len(
                evidence_ids
            )
        await lifecycle.delete_permanently(aggregates[-1].opportunity_id, expected_revision=1)
        assert await _count_ids(session_factory, schema.sources, source_ids) == 0
        assert await _count_ids(session_factory, schema.evidence_items, evidence_ids) == 0
    finally:
        await engine.dispose()
