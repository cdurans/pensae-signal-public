from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes.opportunities import fake_opportunity_aggregate, fake_run_snapshot

from pensae.infrastructure.db import create_engine, create_session_factory, schema
from pensae.opportunities import (
    InvalidLifecycleTransition,
    LifecycleConflict,
    OpportunityAggregateStore,
    OpportunityLifecycleService,
    PortfolioService,
    RediscoveryDecision,
)

pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    table: sa.Table,
    column: object,
    ids: tuple[UUID, ...],
) -> int:
    if not ids:
        return 0
    async with session_factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count()).select_from(table).where(column.in_(ids))  # type: ignore[attr-defined]
            )
            or 0
        )


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_rediscovery_update_merge_reversal_and_metadata_are_durable() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    lifecycle = OpportunityLifecycleService(session_factory)
    portfolio = PortfolioService(session_factory)
    target_run = fake_run_snapshot()
    candidate_run = fake_run_snapshot()
    related_run = fake_run_snapshot()
    rediscovered_run = fake_run_snapshot()
    target = fake_opportunity_aggregate(target_run.id)
    candidate = fake_opportunity_aggregate(candidate_run.id).model_copy(
        update={
            "classification": "possible_rediscovery",
            "relation_id": uuid4(),
            "related_opportunity_id": target.opportunity_id,
            "relation_similarity": Decimal("0.94"),
        }
    )
    related_candidate = fake_opportunity_aggregate(related_run.id).model_copy(
        update={
            "classification": "possible_rediscovery",
            "relation_id": uuid4(),
            "related_opportunity_id": target.opportunity_id,
            "relation_similarity": Decimal("0.91"),
        }
    )
    rediscovered_candidate = fake_opportunity_aggregate(rediscovered_run.id).model_copy(
        update={
            "classification": "possible_rediscovery",
            "relation_id": uuid4(),
            "related_opportunity_id": target.opportunity_id,
            "relation_similarity": Decimal("0.93"),
        }
    )
    try:
        await store.create_run(target_run)
        await store.commit(target)
        await store.create_run(candidate_run)
        await store.commit(candidate)
        await store.create_run(related_run)
        await store.commit(related_candidate)
        await store.create_run(rediscovered_run)
        await store.commit(rediscovered_candidate)
        await portfolio.set_favorite(target.opportunity_id, favorite=True)
        await portfolio.set_note(target.opportunity_id, note="Stable target note")

        related = await lifecycle.decide_rediscovery(
            related_candidate.opportunity_id,
            target_id=target.opportunity_id,
            decision=RediscoveryDecision.RELATED,
            expected_candidate_revision=1,
            expected_target_revision=3,
        )
        rediscovered = await lifecycle.decide_rediscovery(
            rediscovered_candidate.opportunity_id,
            target_id=target.opportunity_id,
            decision=RediscoveryDecision.REDISCOVERED,
            expected_candidate_revision=1,
            expected_target_revision=3,
        )
        assert related.classification == "related"
        assert rediscovered.classification == "rediscovered"
        related_detail = await store.get_detail(related_candidate.opportunity_id)
        rediscovered_detail = await store.get_detail(rediscovered_candidate.opportunity_id)
        assert related_detail is not None and rediscovered_detail is not None
        assert related_detail.rediscovery_target_id is None
        assert rediscovered_detail.rediscovery_target_id == target.opportunity_id
        assert related_detail.lifecycle_events[0].event_type == "related_confirmed"
        assert rediscovered_detail.lifecycle_events[0].event_type == "rediscovered"

        updated = await lifecycle.decide_rediscovery(
            candidate.opportunity_id,
            target_id=target.opportunity_id,
            decision=RediscoveryDecision.UPDATED,
            expected_candidate_revision=1,
            expected_target_revision=3,
        )
        assert updated.created_version_id is not None
        assert updated.target_revision == 4
        target_detail = await store.get_detail(target.opportunity_id)
        candidate_detail = await store.get_detail(candidate.opportunity_id)
        assert target_detail is not None and candidate_detail is not None
        assert target_detail.favorite is True
        assert target_detail.note == "Stable target note"
        assert target_detail.revision == 4
        assert [version.version_number for version in target_detail.versions] == [2, 1]
        assert target_detail.version_id == updated.created_version_id
        historical = await store.get_version_detail(target.opportunity_id, target.version_id)
        assert historical is not None
        assert historical.version_number == 1
        assert historical.report == target.report
        assert candidate_detail.classification == "updated"
        assert candidate_detail.rediscovery_target_id == target.opportunity_id
        assert {event.event_type for event in target_detail.lifecycle_events} >= {"new", "updated"}
        target_updated_event = next(
            event for event in target_detail.lifecycle_events if event.event_type == "updated"
        )
        assert target_updated_event.event_data == {
            "actor": "operator",
            "source_opportunity_id": str(candidate.opportunity_id),
            "source_version_id": str(candidate.version_id),
        }
        candidate_updated_event = next(
            event
            for event in candidate_detail.lifecycle_events
            if event.event_type == "updated_source"
        )
        assert candidate_updated_event.event_data == {
            "actor": "operator",
            "decision": "updated",
            "target_opportunity_id": str(target.opportunity_id),
        }

        for replayed_decision in (
            RediscoveryDecision.UPDATED,
            RediscoveryDecision.REDISCOVERED,
        ):
            with pytest.raises(InvalidLifecycleTransition, match="undecided"):
                await lifecycle.decide_rediscovery(
                    candidate.opportunity_id,
                    target_id=target.opportunity_id,
                    decision=replayed_decision,
                    expected_candidate_revision=2,
                    expected_target_revision=4,
                )

        await portfolio.set_favorite(candidate.opportunity_id, favorite=True)
        await portfolio.set_note(candidate.opportunity_id, note="Merged source note")

        with pytest.raises(LifecycleConflict, match="refresh"):
            await lifecycle.merge(
                candidate.opportunity_id,
                survivor_id=target.opportunity_id,
                expected_revision=1,
                expected_survivor_revision=4,
            )
        with pytest.raises(InvalidLifecycleTransition, match="itself"):
            await lifecycle.merge(
                target.opportunity_id,
                survivor_id=target.opportunity_id,
                expected_revision=4,
                expected_survivor_revision=4,
            )
        async with session_factory() as session, session.begin():
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == target.opportunity_id)
                .values(merge_target_id=candidate.opportunity_id)
            )
        with pytest.raises(InvalidLifecycleTransition, match="independent"):
            await lifecycle.merge(
                candidate.opportunity_id,
                survivor_id=target.opportunity_id,
                expected_revision=4,
                expected_survivor_revision=4,
            )
        async with session_factory() as session, session.begin():
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == target.opportunity_id)
                .values(merge_target_id=None)
            )

        merged = await lifecycle.merge(
            candidate.opportunity_id,
            survivor_id=target.opportunity_id,
            expected_revision=4,
            expected_survivor_revision=4,
        )
        assert merged.lifecycle_status == "merged"
        merged_detail = await store.get_detail(candidate.opportunity_id)
        retained_target = await store.get_detail(target.opportunity_id)
        assert merged_detail is not None and retained_target is not None
        assert merged_detail.merge_target_id == target.opportunity_id
        assert merged_detail.report == candidate.report
        assert merged_detail.favorite is True
        assert merged_detail.note == "Merged source note"
        assert retained_target.favorite is True
        assert retained_target.note == "Stable target note"
        merged_event = next(
            event for event in merged_detail.lifecycle_events if event.event_type == "merged"
        )
        assert merged_event.event_data == {
            "actor": "operator",
            "survivor_id": str(target.opportunity_id),
        }
        with pytest.raises(InvalidLifecycleTransition, match="dependent merges"):
            await lifecycle.delete_permanently(target.opportunity_id, expected_revision=4)
        reversed_result = await lifecycle.reverse_merge(
            candidate.opportunity_id, expected_revision=5
        )
        assert reversed_result.lifecycle_status == "active"
        final_candidate = await store.get_detail(candidate.opportunity_id)
        assert final_candidate is not None
        assert final_candidate.merge_target_id is None
        assert [event.event_type for event in final_candidate.lifecycle_events][:2] == [
            "merge_reversed",
            "merged",
        ]
        assert final_candidate.lifecycle_events[0].event_data == {
            "actor": "operator",
            "former_survivor_id": str(target.opportunity_id),
        }
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_deletion_shared_exclusive_relations_and_rollback_matrix() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    lifecycle = OpportunityLifecycleService(session_factory)
    portfolio = PortfolioService(session_factory)
    target_run = fake_run_snapshot()
    candidate_run = fake_run_snapshot()
    exclusive_run = fake_run_snapshot()
    rollback_run = fake_run_snapshot()
    target = fake_opportunity_aggregate(target_run.id)
    candidate = fake_opportunity_aggregate(candidate_run.id).model_copy(
        update={
            "classification": "possible_rediscovery",
            "relation_id": uuid4(),
            "related_opportunity_id": target.opportunity_id,
            "relation_similarity": Decimal("0.96"),
        }
    )
    exclusive = fake_opportunity_aggregate(exclusive_run.id)
    rollback = fake_opportunity_aggregate(rollback_run.id)
    try:
        for snapshot, aggregate in (
            (target_run, target),
            (candidate_run, candidate),
            (exclusive_run, exclusive),
            (rollback_run, rollback),
        ):
            await store.create_run(snapshot)
            await store.commit(aggregate)
        await lifecycle.decide_rediscovery(
            candidate.opportunity_id,
            target_id=target.opportunity_id,
            decision=RediscoveryDecision.UPDATED,
            expected_candidate_revision=1,
            expected_target_revision=1,
        )
        async with session_factory() as session, session.begin():
            await session.execute(
                sa.insert(schema.opportunity_relations).values(
                    id=uuid4(),
                    from_opportunity_id=target.opportunity_id,
                    to_opportunity_id=candidate.opportunity_id,
                    similarity=Decimal("0.91"),
                    relation_kind="related",
                    threshold_version=target.provenance.threshold_version,
                )
            )

        candidate_support = {
            "sources": tuple(item.id for item in candidate.sources),
            "evidence": tuple(item.id for item in candidate.evidence),
            "signals": tuple(item.id for item in candidate.signals),
            "patterns": (candidate.pattern.id,),
        }
        result = await lifecycle.delete_permanently(candidate.opportunity_id, expected_revision=2)
        assert result.deleted
        assert await store.get_detail(candidate.opportunity_id) is None
        assert await store.get_detail(target.opportunity_id) is not None
        for name, table, column in (
            ("sources", schema.sources, schema.sources.c.id),
            ("evidence", schema.evidence_items, schema.evidence_items.c.id),
            ("signals", schema.problem_signals, schema.problem_signals.c.id),
            ("patterns", schema.problem_patterns, schema.problem_patterns.c.id),
        ):
            assert await _count(session_factory, table, column, candidate_support[name]) == len(
                candidate_support[name]
            )
        async with session_factory() as session:
            stale_relations = await session.scalar(
                sa.select(sa.func.count())
                .select_from(schema.opportunity_relations)
                .where(
                    sa.or_(
                        schema.opportunity_relations.c.from_opportunity_id
                        == candidate.opportunity_id,
                        schema.opportunity_relations.c.to_opportunity_id
                        == candidate.opportunity_id,
                    )
                )
            )
            audit = (
                (
                    await session.execute(
                        sa.select(
                            schema.operational_audit.c.action,
                            schema.operational_audit.c.target_id,
                            schema.operational_audit.c.status,
                        ).where(schema.operational_audit.c.id == result.audit_id)
                    )
                )
                .mappings()
                .one()
            )
        assert stale_relations == 0
        assert dict(audit) == {
            "action": "permanent_delete",
            "target_id": candidate.opportunity_id,
            "status": "completed",
        }

        await lifecycle.delete_permanently(exclusive.opportunity_id, expected_revision=1)
        assert await store.get_detail(exclusive.opportunity_id) is None
        assert (
            await _count(
                session_factory,
                schema.sources,
                schema.sources.c.id,
                tuple(item.id for item in exclusive.sources),
            )
            == 0
        )
        assert (
            await _count(
                session_factory,
                schema.evidence_items,
                schema.evidence_items.c.id,
                tuple(item.id for item in exclusive.evidence),
            )
            == 0
        )
        assert (
            await _count(
                session_factory,
                schema.problem_signals,
                schema.problem_signals.c.id,
                tuple(item.id for item in exclusive.signals),
            )
            == 0
        )
        assert (
            await _count(
                session_factory,
                schema.problem_patterns,
                schema.problem_patterns.c.id,
                (exclusive.pattern.id,),
            )
            == 0
        )

        def fail_after_delete(step: str) -> None:
            if step == "orphan_cleanup":
                raise RuntimeError("injected deletion failure")

        failing = OpportunityLifecycleService(session_factory, after_step=fail_after_delete)
        await portfolio.set_favorite(rollback.opportunity_id, favorite=True)
        await portfolio.set_note(rollback.opportunity_id, note="Intervening metadata")
        with pytest.raises(LifecycleConflict, match="refresh"):
            await lifecycle.delete_permanently(rollback.opportunity_id, expected_revision=1)
        current_rollback = await store.get_detail(rollback.opportunity_id)
        assert current_rollback is not None
        assert current_rollback.favorite is True
        assert current_rollback.note == "Intervening metadata"
        with pytest.raises(RuntimeError, match="injected deletion failure"):
            await failing.delete_permanently(rollback.opportunity_id, expected_revision=3)
        assert await store.get_detail(rollback.opportunity_id) is not None
        async with session_factory() as session:
            leaked_audit = await session.scalar(
                sa.select(sa.func.count())
                .select_from(schema.operational_audit)
                .where(schema.operational_audit.c.target_id == rollback.opportunity_id)
            )
        assert leaked_audit == 0
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_reference_aware_deletion_isolated_at_each_support_level() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    lifecycle = OpportunityLifecycleService(session_factory)
    try:
        for shared_level in ("source", "evidence", "signal", "pattern"):
            target_run = fake_run_snapshot()
            candidate_run = fake_run_snapshot()
            target = fake_opportunity_aggregate(target_run.id)
            candidate = fake_opportunity_aggregate(candidate_run.id)
            await store.create_run(target_run)
            await store.commit(target)
            await store.create_run(candidate_run)
            await store.commit(candidate)

            async with session_factory() as session, session.begin():
                if shared_level == "source":
                    await session.execute(
                        sa.update(schema.evidence_items)
                        .where(schema.evidence_items.c.id == target.evidence[0].id)
                        .values(source_id=candidate.sources[0].id)
                    )
                elif shared_level == "evidence":
                    await session.execute(
                        sa.insert(schema.opportunity_version_evidence).values(
                            version_id=target.version_id,
                            evidence_id=candidate.evidence[0].id,
                        )
                    )
                elif shared_level == "signal":
                    await session.execute(
                        sa.insert(schema.opportunity_version_signals).values(
                            version_id=target.version_id,
                            signal_id=candidate.signals[0].id,
                        )
                    )
                else:
                    await session.execute(
                        sa.insert(schema.opportunity_version_patterns).values(
                            version_id=target.version_id,
                            pattern_id=candidate.pattern.id,
                        )
                    )

            await lifecycle.delete_permanently(candidate.opportunity_id, expected_revision=1)

            expected_sources = {
                "source": {candidate.sources[0].id},
                "evidence": {candidate.sources[0].id},
                "signal": {candidate.sources[0].id},
                "pattern": {item.id for item in candidate.sources},
            }[shared_level]
            expected_evidence = {
                "source": set(),
                "evidence": {candidate.evidence[0].id},
                "signal": {candidate.evidence[0].id, candidate.evidence[1].id},
                "pattern": {item.id for item in candidate.evidence},
            }[shared_level]
            expected_signals = {
                "source": set(),
                "evidence": set(),
                "signal": {candidate.signals[0].id},
                "pattern": {item.id for item in candidate.signals},
            }[shared_level]
            expected_patterns = {candidate.pattern.id} if shared_level == "pattern" else set()
            for table, column, ids, expected in (
                (
                    schema.sources,
                    schema.sources.c.id,
                    tuple(item.id for item in candidate.sources),
                    expected_sources,
                ),
                (
                    schema.evidence_items,
                    schema.evidence_items.c.id,
                    tuple(item.id for item in candidate.evidence),
                    expected_evidence,
                ),
                (
                    schema.problem_signals,
                    schema.problem_signals.c.id,
                    tuple(item.id for item in candidate.signals),
                    expected_signals,
                ),
                (
                    schema.problem_patterns,
                    schema.problem_patterns.c.id,
                    (candidate.pattern.id,),
                    expected_patterns,
                ),
            ):
                async with session_factory() as session:
                    retained = set(
                        (
                            await session.scalars(
                                sa.select(column).select_from(table).where(column.in_(ids))
                            )
                        ).all()
                    )
                assert retained == expected, shared_level
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_lifecycle_target_search_reaches_records_beyond_the_newest_hundred() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    portfolio = PortfolioService(session_factory)
    run = fake_run_snapshot()
    source = fake_opportunity_aggregate(run.id)
    oldest = source.model_copy(
        update={
            "report": source.report.model_copy(
                update={"opportunity_name": "Unique oldest lifecycle survivor"}
            )
        }
    )
    try:
        await store.create_run(run)
        await store.commit(oldest)
        for index in range(101):
            filler = fake_opportunity_aggregate(run.id)
            filler = filler.model_copy(
                update={
                    "report": filler.report.model_copy(
                        update={"opportunity_name": f"Newer filler opportunity {index:03d}"}
                    )
                }
            )
            await store.commit(filler)

        default_page = await portfolio.lifecycle_targets(uuid4(), search=None, limit=100)
        searched = await portfolio.lifecycle_targets(uuid4(), search="unique oldest", limit=100)

        assert len(default_page.items) == 100
        assert default_page.has_more is True
        assert [item.id for item in searched.items] == [oldest.opportunity_id]
        assert searched.has_more is False
    finally:
        await engine.dispose()
