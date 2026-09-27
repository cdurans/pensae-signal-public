"""Run snapshot, progress, and startup-recovery persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pensae.infrastructure.db import schema
from pensae.opportunities.records import (
    RecoverySummary,
    RunDetail,
    RunSnapshot,
    reject_forbidden_keys,
)
from pensae.research.workflow import ModelCallUsage

_INITIAL_CLASSIFICATIONS = ("new", "related", "possible_rediscovery")
_COUNTED_CLASSIFICATIONS = ("new", "related")
_ACHIEVED_COUNTER_KEY = "p7_achieved_count"
_POSSIBLE_REDISCOVERY_COUNTER_KEY = "p7_unresolved_possible_rediscovery"


class RunStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_run(self, snapshot: RunSnapshot) -> UUID:
        reject_forbidden_keys(snapshot.effective_config)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.insert(schema.runs).values(
                    id=snapshot.id,
                    state="running",
                    effective_config=snapshot.effective_config,
                    workflow_version=snapshot.workflow_version,
                    schema_version=snapshot.schema_version,
                )
            )
        return snapshot.id

    async def set_run_state(
        self,
        run_id: UUID,
        state: Literal[
            "running", "stopping", "completed", "completed_with_warnings", "stopped", "failed"
        ],
    ) -> None:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                sa.update(schema.runs)
                .where(schema.runs.c.id == run_id)
                .values(state=state, updated_at=sa.func.now())
                .returning(schema.runs.c.id)
            )
            if result.scalar_one_or_none() is None:
                raise LookupError("run does not exist")

    async def delete_empty_run(self, run_id: UUID) -> bool:
        """Delete a run only when no durable opportunity version references it."""

        committed = sa.exists(
            sa.select(schema.opportunity_versions.c.id).where(
                schema.opportunity_versions.c.run_id == schema.runs.c.id
            )
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                sa.delete(schema.runs)
                .where(schema.runs.c.id == run_id, ~committed)
                .returning(schema.runs.c.id)
            )
            return result.scalar_one_or_none() is not None

    async def update_run_progress(
        self,
        run_id: UUID,
        *,
        state: Literal[
            "running", "stopping", "completed", "completed_with_warnings", "stopped", "failed"
        ],
        stage: str,
        counters: Mapping[str, int],
        warning_codes: tuple[str, ...],
        committed_count: int,
        model_usage: tuple[ModelCallUsage, ...] = (),
    ) -> None:
        if not stage or len(stage) > 64 or not stage.replace("_", "").isalnum():
            raise ValueError("run stage must be a bounded identifier")
        if committed_count < 0 or any(value < 0 for value in counters.values()):
            raise ValueError("run counters cannot be negative")
        if any(
            not code
            or len(code) > 64
            or not code.replace("_", "").isalnum()
            or code.casefold() != code
            for code in warning_codes
        ):
            raise ValueError("warning codes must be bounded lowercase identifiers")
        if len(model_usage) > self._maximum_model_usage_records():
            raise ValueError("model usage exceeds the protected maximum")
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                sa.update(schema.runs)
                .where(schema.runs.c.id == run_id)
                .values(
                    state=state,
                    current_stage=stage,
                    work_counters=dict(counters),
                    warning_codes=list(warning_codes),
                    model_usage=[item.model_dump(mode="json") for item in model_usage],
                    committed_count=committed_count,
                    updated_at=sa.func.now(),
                )
                .returning(schema.runs.c.id)
            )
            if result.scalar_one_or_none() is None:
                raise LookupError("run does not exist")

    async def get_run(self, run_id: UUID) -> RunDetail | None:
        creation_event = schema.lifecycle_events.alias("run_creation_event")
        initial_version = schema.opportunity_versions.alias("run_initial_version")
        run_columns = (
            schema.runs.c.id,
            schema.runs.c.state,
            schema.runs.c.effective_config,
            schema.runs.c.workflow_version,
            schema.runs.c.schema_version,
            schema.runs.c.created_at,
            schema.runs.c.current_stage,
            schema.runs.c.work_counters,
            schema.runs.c.warning_codes,
            schema.runs.c.model_usage,
            schema.runs.c.committed_count,
            schema.runs.c.updated_at,
        )
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(
                            *run_columns,
                            initial_version.c.id.label("committed_version_id"),
                            initial_version.c.opportunity_id.label("committed_opportunity_id"),
                            creation_event.c.event_type.label("initial_classification"),
                        )
                        .select_from(
                            schema.runs.outerjoin(
                                initial_version,
                                sa.and_(
                                    initial_version.c.run_id == schema.runs.c.id,
                                    initial_version.c.version_number == 1,
                                ),
                            ).outerjoin(
                                creation_event,
                                sa.and_(
                                    creation_event.c.version_id == initial_version.c.id,
                                    creation_event.c.event_type.in_(_INITIAL_CLASSIFICATIONS),
                                ),
                            )
                        )
                        .where(schema.runs.c.id == run_id)
                        .order_by(
                            initial_version.c.created_at.nulls_last(),
                            initial_version.c.id.nulls_last(),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return None
            row = {column.key: rows[0][column.key] for column in run_columns}
            committed_ids = tuple(
                dict.fromkeys(
                    item["committed_opportunity_id"]
                    for item in rows
                    if item["committed_version_id"] is not None
                )
            )
            achieved_ids = {
                item["committed_opportunity_id"]
                for item in rows
                if item["initial_classification"] in _COUNTED_CLASSIFICATIONS
            }
            possible_rediscovery_ids = {
                item["committed_opportunity_id"]
                for item in rows
                if item["initial_classification"] == "possible_rediscovery"
            }
            counters, committed_count = self._reconcile_durable_progress(
                work_counters=row["work_counters"],
                committed_count=row["committed_count"],
                durable_committed_count=len(committed_ids),
                durable_achieved_count=len(achieved_ids),
                durable_possible_rediscovery_count=len(possible_rediscovery_ids),
            )
            row["work_counters"] = counters
            row["committed_count"] = committed_count
        return RunDetail.model_validate(
            {
                **row,
                "opportunity_id": committed_ids[-1] if committed_ids else None,
                "committed_opportunity_ids": committed_ids,
            }
        )

    @staticmethod
    def _maximum_model_usage_records() -> int:
        # The accepted protected ceiling is 256 calls. The run JSONB retains
        # every bounded usage record; it does not silently truncate accounting.
        return 256

    async def resolve_abandoned_runs(self) -> RecoverySummary:
        """Stop abandoned runs with commits and remove empty abandoned rows atomically."""

        async with self._session_factory() as session, session.begin():
            initial_version = schema.opportunity_versions.alias("recovery_initial_version")
            creation_event = schema.lifecycle_events.alias("recovery_creation_event")
            commit_count = (
                sa.select(sa.func.count(sa.distinct(initial_version.c.id)))
                .where(
                    initial_version.c.run_id == schema.runs.c.id,
                    initial_version.c.version_number == 1,
                )
                .correlate(schema.runs)
                .scalar_subquery()
            )
            achieved_count = (
                sa.select(sa.func.count(sa.distinct(initial_version.c.id)))
                .select_from(
                    initial_version.join(
                        creation_event,
                        creation_event.c.version_id == initial_version.c.id,
                    )
                )
                .where(
                    initial_version.c.run_id == schema.runs.c.id,
                    initial_version.c.version_number == 1,
                    creation_event.c.event_type.in_(_COUNTED_CLASSIFICATIONS),
                )
                .correlate(schema.runs)
                .scalar_subquery()
            )
            possible_rediscovery_count = (
                sa.select(sa.func.count(sa.distinct(initial_version.c.id)))
                .select_from(
                    initial_version.join(
                        creation_event,
                        creation_event.c.version_id == initial_version.c.id,
                    )
                )
                .where(
                    initial_version.c.run_id == schema.runs.c.id,
                    initial_version.c.version_number == 1,
                    creation_event.c.event_type == "possible_rediscovery",
                )
                .correlate(schema.runs)
                .scalar_subquery()
            )
            rows = (
                await session.execute(
                    sa.select(
                        schema.runs.c.id,
                        schema.runs.c.committed_count,
                        schema.runs.c.work_counters,
                        commit_count.label("commit_count"),
                        achieved_count.label("achieved_count"),
                        possible_rediscovery_count.label("possible_rediscovery_count"),
                    )
                    .where(schema.runs.c.state.in_(("running", "stopping")))
                    .with_for_update(of=schema.runs)
                )
            ).all()
            stopped_rows: list[tuple[UUID, dict[str, int], int]] = []
            deleted_rows: list[UUID] = []
            for row in rows:
                counters, reconciled_committed_count = self._reconcile_durable_progress(
                    work_counters=row.work_counters,
                    committed_count=row.committed_count,
                    durable_committed_count=row.commit_count,
                    durable_achieved_count=row.achieved_count,
                    durable_possible_rediscovery_count=row.possible_rediscovery_count,
                )
                if reconciled_committed_count:
                    stopped_rows.append((row.id, counters, reconciled_committed_count))
                else:
                    deleted_rows.append(row.id)
            for run_id, counters, reconciled_committed_count in stopped_rows:
                await session.execute(
                    sa.update(schema.runs)
                    .where(schema.runs.c.id == run_id)
                    .values(
                        state="stopped",
                        current_stage="startup_cleanup",
                        work_counters=counters,
                        warning_codes=["application_restart"],
                        committed_count=reconciled_committed_count,
                        updated_at=sa.func.now(),
                    )
                )
            if deleted_rows:
                await session.execute(
                    sa.delete(schema.runs).where(schema.runs.c.id.in_(deleted_rows))
                )
        stopped = tuple(row[0] for row in stopped_rows)
        deleted = tuple(deleted_rows)
        return RecoverySummary(stopped_run_ids=stopped, deleted_run_ids=deleted)

    @staticmethod
    def _reconcile_durable_progress(
        *,
        work_counters: Mapping[str, int],
        committed_count: int,
        durable_committed_count: int,
        durable_achieved_count: int,
        durable_possible_rediscovery_count: int,
    ) -> tuple[dict[str, int], int]:
        """Raise observer accounting to the authoritative durable creation floor."""

        counters = dict(work_counters)
        achieved_count = counters.get(_ACHIEVED_COUNTER_KEY, 0)
        possible_rediscovery_count = counters.get(_POSSIBLE_REDISCOVERY_COUNTER_KEY, 0)
        counters[_ACHIEVED_COUNTER_KEY] = max(achieved_count, durable_achieved_count)
        counters[_POSSIBLE_REDISCOVERY_COUNTER_KEY] = max(
            possible_rediscovery_count,
            durable_possible_rediscovery_count,
        )
        return counters, max(committed_count, durable_committed_count)
