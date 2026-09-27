"""Reference-aware cleanup for durable opportunity support records."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from pensae.infrastructure.db import schema

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class OrphanSweepResult:
    patterns: int
    signals: int
    evidence: int
    sources: int

    @property
    def total(self) -> int:
        return self.patterns + self.signals + self.evidence + self.sources


class SupportRetention:
    """Delete support only after proving no retained record references it."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def sweep(self) -> OrphanSweepResult:
        async with self._session_factory() as session, session.begin():
            return await self._remove(session)

    async def support_ids(
        self, session: AsyncSession, opportunity_id: UUID
    ) -> dict[str, tuple[UUID, ...]]:
        version_ids = sa.select(schema.opportunity_versions.c.id).where(
            schema.opportunity_versions.c.opportunity_id == opportunity_id
        )
        evidence = tuple(
            (
                await session.scalars(
                    sa.select(schema.opportunity_version_evidence.c.evidence_id)
                    .where(schema.opportunity_version_evidence.c.version_id.in_(version_ids))
                    .distinct()
                )
            ).all()
        )
        signals = tuple(
            (
                await session.scalars(
                    sa.select(schema.opportunity_version_signals.c.signal_id)
                    .where(schema.opportunity_version_signals.c.version_id.in_(version_ids))
                    .distinct()
                )
            ).all()
        )
        patterns = tuple(
            (
                await session.scalars(
                    sa.select(schema.opportunity_version_patterns.c.pattern_id)
                    .where(schema.opportunity_version_patterns.c.version_id.in_(version_ids))
                    .distinct()
                )
            ).all()
        )
        source_ids = (
            tuple(
                (
                    await session.scalars(
                        sa.select(schema.evidence_items.c.source_id)
                        .where(schema.evidence_items.c.id.in_(evidence))
                        .distinct()
                    )
                ).all()
            )
            if evidence
            else ()
        )
        return {
            "evidence": evidence,
            "signals": signals,
            "patterns": patterns,
            "sources": source_ids,
        }

    async def remove_candidates(
        self, session: AsyncSession, support: dict[str, tuple[UUID, ...]]
    ) -> OrphanSweepResult:
        return await self._remove(session, support)

    async def _remove(
        self,
        session: AsyncSession,
        support: dict[str, tuple[UUID, ...]] | None = None,
    ) -> OrphanSweepResult:
        patterns = await session.execute(
            sa.delete(schema.problem_patterns).where(
                self._within(schema.problem_patterns.c.id, support, "patterns"),
                ~sa.exists(
                    sa.select(schema.opportunity_version_patterns.c.version_id).where(
                        schema.opportunity_version_patterns.c.pattern_id
                        == schema.problem_patterns.c.id
                    )
                ),
            )
        )
        signals = await session.execute(
            sa.delete(schema.problem_signals).where(
                self._within(schema.problem_signals.c.id, support, "signals"),
                ~sa.exists(
                    sa.select(schema.opportunity_version_signals.c.version_id).where(
                        schema.opportunity_version_signals.c.signal_id
                        == schema.problem_signals.c.id
                    )
                ),
                ~sa.exists(
                    sa.select(schema.problem_pattern_signals.c.pattern_id).where(
                        schema.problem_pattern_signals.c.signal_id == schema.problem_signals.c.id
                    )
                ),
            )
        )
        evidence = await session.execute(
            sa.delete(schema.evidence_items).where(
                self._within(schema.evidence_items.c.id, support, "evidence"),
                ~sa.exists(
                    sa.select(schema.opportunity_version_evidence.c.version_id).where(
                        schema.opportunity_version_evidence.c.evidence_id
                        == schema.evidence_items.c.id
                    )
                ),
                ~sa.exists(
                    sa.select(schema.problem_signal_evidence.c.signal_id).where(
                        schema.problem_signal_evidence.c.evidence_id == schema.evidence_items.c.id
                    )
                ),
            )
        )
        sources = await session.execute(
            sa.delete(schema.sources).where(
                self._within(schema.sources.c.id, support, "sources"),
                ~sa.exists(
                    sa.select(schema.evidence_items.c.id).where(
                        schema.evidence_items.c.source_id == schema.sources.c.id
                    )
                ),
            )
        )
        return OrphanSweepResult(
            patterns=int(getattr(patterns, "rowcount", 0)),
            signals=int(getattr(signals, "rowcount", 0)),
            evidence=int(getattr(evidence, "rowcount", 0)),
            sources=int(getattr(sources, "rowcount", 0)),
        )

    @staticmethod
    def _within(
        column: sa.Column[UUID],
        support: dict[str, tuple[UUID, ...]] | None,
        key: str,
    ) -> sa.ColumnElement[bool]:
        return sa.true() if support is None else column.in_(support[key])
