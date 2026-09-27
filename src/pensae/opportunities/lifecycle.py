"""Constrained operator lifecycle transactions for retained opportunities."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from pensae.infrastructure.db import schema
from pensae.opportunities.retention import OrphanSweepResult, SupportRetention


class LifecycleConflict(RuntimeError):
    """A revision or state changed after the operator loaded the decision surface."""


class InvalidLifecycleTransition(ValueError):
    """The requested lifecycle transition is not allowed by product policy."""


class RediscoveryDecision(StrEnum):
    RELATED = "related"
    REDISCOVERED = "rediscovered"
    UPDATED = "updated"


class LifecycleMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: UUID
    revision: int
    classification: str
    lifecycle_status: str
    target_opportunity_id: UUID | None = None
    target_revision: int | None = None
    created_version_id: UUID | None = None


class DeletionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: UUID
    deleted: bool
    audit_id: UUID


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
IdFactory = Callable[[], UUID]
StepHook = Callable[[str], None]


class OpportunityLifecycleService:
    """Own explicit lifecycle transitions and their short database transactions."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        id_factory: IdFactory = uuid4,
        after_step: StepHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory
        self._after_step = after_step or (lambda _step: None)
        self._retention = SupportRetention(session_factory)

    async def sweep_orphan_support(self) -> OrphanSweepResult:
        """Remove globally orphaned support during bounded startup recovery."""

        return await self._retention.sweep()

    async def decide_rediscovery(
        self,
        candidate_id: UUID,
        *,
        target_id: UUID,
        decision: RediscoveryDecision,
        expected_candidate_revision: int,
        expected_target_revision: int,
    ) -> LifecycleMutationResult:
        if candidate_id == target_id:
            raise InvalidLifecycleTransition("an opportunity cannot rediscover itself")
        async with self._session_factory() as session, session.begin():
            rows = await self._lock_opportunities(session, candidate_id, target_id)
            candidate = rows.get(candidate_id)
            target = rows.get(target_id)
            if candidate is None or target is None:
                raise LookupError("candidate or target opportunity does not exist")
            self._require_revision(candidate, expected_candidate_revision)
            self._require_revision(target, expected_target_revision)
            self._require_active(candidate, "candidate")
            self._require_active(target, "target")
            if candidate["classification"] != "possible_rediscovery":
                raise InvalidLifecycleTransition(
                    "only an undecided possible rediscovery can receive an operator decision"
                )
            relation = (
                (
                    await session.execute(
                        sa.select(
                            schema.opportunity_relations.c.id,
                            schema.opportunity_relations.c.relation_kind,
                        ).where(
                            schema.opportunity_relations.c.from_opportunity_id == candidate_id,
                            schema.opportunity_relations.c.to_opportunity_id == target_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if relation is None:
                raise InvalidLifecycleTransition(
                    "rediscovery decisions require a retained semantic relation"
                )
            created_version_id: UUID | None = None
            target_revision = int(target["revision"])
            if decision is RediscoveryDecision.RELATED:
                await session.execute(
                    sa.update(schema.opportunity_relations)
                    .where(schema.opportunity_relations.c.id == relation["id"])
                    .values(relation_kind="related")
                )
                event_type = "related_confirmed"
            elif decision is RediscoveryDecision.REDISCOVERED:
                event_type = "rediscovered"
            elif decision is RediscoveryDecision.UPDATED:
                created_version_id = self._id_factory()
                await self._copy_current_version(
                    session,
                    source=candidate,
                    target=target,
                    version_id=created_version_id,
                )
                target_revision += 1
                await self._insert_event(
                    session,
                    opportunity_id=target_id,
                    version_id=created_version_id,
                    event_type="updated",
                    event_data={
                        "actor": "operator",
                        "source_opportunity_id": str(candidate_id),
                        "source_version_id": str(candidate["current_version_id"]),
                    },
                )
                event_type = "updated_source"
            else:  # pragma: no cover - StrEnum validation makes this defensive.
                raise AssertionError(f"unsupported rediscovery decision: {decision}")

            classification = decision.value
            candidate_revision = int(candidate["revision"]) + 1
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == candidate_id)
                .values(
                    classification=classification,
                    rediscovery_target_id=(
                        None if decision is RediscoveryDecision.RELATED else target_id
                    ),
                    revision=candidate_revision,
                    updated_at=sa.func.now(),
                )
            )
            await self._insert_event(
                session,
                opportunity_id=candidate_id,
                version_id=candidate["current_version_id"],
                event_type=event_type,
                event_data={
                    "actor": "operator",
                    "decision": decision.value,
                    "target_opportunity_id": str(target_id),
                },
            )
            self._after_step("rediscovery_decision")
            return LifecycleMutationResult(
                opportunity_id=candidate_id,
                revision=candidate_revision,
                classification=classification,
                lifecycle_status="active",
                target_opportunity_id=target_id,
                target_revision=target_revision,
                created_version_id=created_version_id,
            )

    async def merge(
        self,
        opportunity_id: UUID,
        *,
        survivor_id: UUID,
        expected_revision: int,
        expected_survivor_revision: int,
    ) -> LifecycleMutationResult:
        if opportunity_id == survivor_id:
            raise InvalidLifecycleTransition("an opportunity cannot be merged into itself")
        async with self._session_factory() as session, session.begin():
            rows = await self._lock_opportunities(session, opportunity_id, survivor_id)
            source = rows.get(opportunity_id)
            survivor = rows.get(survivor_id)
            if source is None or survivor is None:
                raise LookupError("merge source or survivor does not exist")
            self._require_revision(source, expected_revision)
            self._require_revision(survivor, expected_survivor_revision)
            self._require_active(source, "merge source")
            self._require_active(survivor, "survivor")
            await self._reject_merge_cycle(session, source_id=opportunity_id, target_id=survivor_id)
            revision = int(source["revision"]) + 1
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == opportunity_id)
                .values(
                    lifecycle_status="merged",
                    merge_target_id=survivor_id,
                    revision=revision,
                    updated_at=sa.func.now(),
                )
            )
            await self._insert_event(
                session,
                opportunity_id=opportunity_id,
                version_id=source["current_version_id"],
                event_type="merged",
                event_data={"actor": "operator", "survivor_id": str(survivor_id)},
            )
            self._after_step("merge")
            return LifecycleMutationResult(
                opportunity_id=opportunity_id,
                revision=revision,
                classification=source["classification"],
                lifecycle_status="merged",
                target_opportunity_id=survivor_id,
                target_revision=int(survivor["revision"]),
            )

    async def reverse_merge(
        self, opportunity_id: UUID, *, expected_revision: int
    ) -> LifecycleMutationResult:
        async with self._session_factory() as session, session.begin():
            row = await self._lock_one(session, opportunity_id)
            if row is None:
                raise LookupError("merged opportunity does not exist")
            self._require_revision(row, expected_revision)
            if row["lifecycle_status"] != "merged" or row["merge_target_id"] is None:
                raise InvalidLifecycleTransition("only a current merge can be reversed")
            target_id = row["merge_target_id"]
            revision = int(row["revision"]) + 1
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == opportunity_id)
                .values(
                    lifecycle_status="active",
                    merge_target_id=None,
                    revision=revision,
                    updated_at=sa.func.now(),
                )
            )
            await self._insert_event(
                session,
                opportunity_id=opportunity_id,
                version_id=row["current_version_id"],
                event_type="merge_reversed",
                event_data={"actor": "operator", "former_survivor_id": str(target_id)},
            )
            self._after_step("merge_reversal")
            return LifecycleMutationResult(
                opportunity_id=opportunity_id,
                revision=revision,
                classification=row["classification"],
                lifecycle_status="active",
                target_opportunity_id=target_id,
            )

    async def delete_permanently(
        self, opportunity_id: UUID, *, expected_revision: int
    ) -> DeletionResult:
        audit_id = self._id_factory()
        async with self._session_factory() as session, session.begin():
            row = await self._lock_one(session, opportunity_id)
            if row is None:
                raise LookupError("opportunity does not exist")
            self._require_revision(row, expected_revision)
            incoming_merge = await session.scalar(
                sa.select(schema.opportunities.c.id)
                .where(schema.opportunities.c.merge_target_id == opportunity_id)
                .limit(1)
            )
            if incoming_merge is not None:
                raise InvalidLifecycleTransition(
                    "reverse dependent merges before deleting their survivor"
                )
            support = await self._retention.support_ids(session, opportunity_id)
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.rediscovery_target_id == opportunity_id)
                .values(
                    classification="related",
                    rediscovery_target_id=None,
                    revision=schema.opportunities.c.revision + 1,
                    updated_at=sa.func.now(),
                )
            )
            await session.execute(
                sa.delete(schema.opportunities).where(schema.opportunities.c.id == opportunity_id)
            )
            self._after_step("opportunity_deleted")
            await self._retention.remove_candidates(session, support)
            self._after_step("orphan_cleanup")
            await session.execute(
                sa.insert(schema.operational_audit).values(
                    id=audit_id,
                    action="permanent_delete",
                    target_id=opportunity_id,
                    status="completed",
                )
            )
            self._after_step("deletion_audit")
        return DeletionResult(opportunity_id=opportunity_id, deleted=True, audit_id=audit_id)

    async def _copy_current_version(
        self,
        session: AsyncSession,
        *,
        source: RowMapping,
        target: RowMapping,
        version_id: UUID,
    ) -> None:
        source_version = (
            (
                await session.execute(
                    sa.select(schema.opportunity_versions).where(
                        schema.opportunity_versions.c.id == source["current_version_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        next_number = (
            int(
                await session.scalar(
                    sa.select(sa.func.max(schema.opportunity_versions.c.version_number)).where(
                        schema.opportunity_versions.c.opportunity_id == target["id"]
                    )
                )
                or 0
            )
            + 1
        )
        values = dict(source_version)
        for key in ("id", "opportunity_id", "version_number", "created_at"):
            values.pop(key, None)
        values.update(id=version_id, opportunity_id=target["id"], version_number=next_number)
        await session.execute(sa.insert(schema.opportunity_versions).values(**values))
        for join, related_column in (
            (schema.opportunity_version_evidence, "evidence_id"),
            (schema.opportunity_version_signals, "signal_id"),
            (schema.opportunity_version_patterns, "pattern_id"),
        ):
            await session.execute(
                sa.insert(join).from_select(
                    ["version_id", related_column],
                    sa.select(sa.literal(version_id), join.c[related_column]).where(
                        join.c.version_id == source["current_version_id"]
                    ),
                )
            )
        await session.execute(
            sa.update(schema.opportunities)
            .where(schema.opportunities.c.id == target["id"])
            .values(
                current_version_id=version_id,
                primary_industry=source["primary_industry"],
                revision=schema.opportunities.c.revision + 1,
                updated_at=sa.func.now(),
            )
        )

    async def _lock_opportunities(
        self, session: AsyncSession, *opportunity_ids: UUID
    ) -> dict[UUID, RowMapping]:
        rows = (
            await session.execute(
                sa.select(schema.opportunities)
                .where(schema.opportunities.c.id.in_(sorted(opportunity_ids, key=str)))
                .order_by(schema.opportunities.c.id)
                .with_for_update()
            )
        ).mappings()
        return {row["id"]: row for row in rows}

    async def _lock_one(self, session: AsyncSession, opportunity_id: UUID) -> RowMapping | None:
        return (
            (
                await session.execute(
                    sa.select(schema.opportunities)
                    .where(schema.opportunities.c.id == opportunity_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _reject_merge_cycle(
        self, session: AsyncSession, *, source_id: UUID, target_id: UUID
    ) -> None:
        visited: set[UUID] = set()
        cursor: UUID | None = target_id
        while cursor is not None:
            if cursor == source_id or cursor in visited:
                raise InvalidLifecycleTransition("merge would create a lifecycle cycle")
            visited.add(cursor)
            cursor = await session.scalar(
                sa.select(schema.opportunities.c.merge_target_id).where(
                    schema.opportunities.c.id == cursor
                )
            )

    async def _insert_event(
        self,
        session: AsyncSession,
        *,
        opportunity_id: UUID,
        version_id: UUID,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        await session.execute(
            sa.insert(schema.lifecycle_events).values(
                id=self._id_factory(),
                opportunity_id=opportunity_id,
                version_id=version_id,
                event_type=event_type,
                event_data=event_data,
            )
        )

    @staticmethod
    def _require_revision(row: RowMapping, expected: int) -> None:
        if int(row["revision"]) != expected:
            raise LifecycleConflict("opportunity revision changed; refresh before retrying")

    @staticmethod
    def _require_active(row: RowMapping, label: str) -> None:
        if row["lifecycle_status"] != "active" or row["merge_target_id"] is not None:
            raise InvalidLifecycleTransition(f"{label} must be an independent active opportunity")
