"""Exact identity, rediscovery, and cosine-similarity persistence."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pensae.infrastructure.db import schema
from pensae.opportunities.records import PatternSimilarityMatch, SimilarityMatch


class SimilarityStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def nearest_problem(
        self, embedding: Sequence[float], *, exclude: UUID | None = None
    ) -> SimilarityMatch | None:
        if len(embedding) != 1024:
            raise ValueError("similarity embeddings must contain exactly 1024 dimensions")
        distance = schema.opportunity_versions.c.embedding.cosine_distance(list(embedding))
        query = (
            sa.select(schema.opportunities.c.id.label("opportunity_id"), distance.label("distance"))
            .select_from(
                schema.opportunities.join(
                    schema.opportunity_versions,
                    schema.opportunity_versions.c.id == schema.opportunities.c.current_version_id,
                )
            )
            .order_by(distance)
            .limit(1)
        )
        if exclude is not None:
            query = query.where(schema.opportunities.c.id != exclude)
        async with self._session_factory() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            return None
        return SimilarityMatch(opportunity_id=row.opportunity_id, similarity=1.0 - row.distance)

    async def find_identity(self, fingerprint: str) -> UUID | None:
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("identity fingerprint must be lowercase SHA-256")
        async with self._session_factory() as session:
            return await session.scalar(
                sa.select(schema.opportunities.c.id).where(
                    schema.opportunities.c.identity_fingerprint == fingerprint
                )
            )

    async def record_rediscovery(
        self,
        *,
        run_id: UUID,
        opportunity_id: UUID,
        lifecycle_event_id: UUID,
    ) -> None:
        """Record an exact match without creating or overwriting an opportunity version."""

        async with self._session_factory() as session, session.begin():
            current_version = await session.scalar(
                sa.select(schema.opportunities.c.current_version_id)
                .where(schema.opportunities.c.id == opportunity_id)
                .with_for_update()
            )
            if current_version is None:
                raise LookupError("rediscovered opportunity has no current version")
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == opportunity_id)
                .values(
                    classification="rediscovered",
                    revision=schema.opportunities.c.revision + 1,
                    updated_at=sa.func.now(),
                )
            )
            await session.execute(
                sa.insert(schema.lifecycle_events).values(
                    id=lifecycle_event_id,
                    opportunity_id=opportunity_id,
                    version_id=current_version,
                    event_type="rediscovered",
                    event_data={
                        "classification": "rediscovered",
                        "run_id": str(run_id),
                        "actor": "application",
                        "match_kind": "exact_identity",
                    },
                )
            )

    async def nearest_problems(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 3,
        minimum_similarity: float = 0.0,
        exclude: UUID | None = None,
    ) -> tuple[SimilarityMatch, ...]:
        """Return the strongest exact cosine comparisons, never an ANN approximation."""

        if len(embedding) != 1024:
            raise ValueError("similarity embeddings must contain exactly 1024 dimensions")
        if not 1 <= limit <= 3:
            raise ValueError("at most three semantic relations may be retained")
        if not 0 <= minimum_similarity <= 1:
            raise ValueError("minimum similarity must be within zero and one")
        distance = schema.opportunity_versions.c.embedding.cosine_distance(list(embedding))
        similarity = (1.0 - distance).label("similarity")
        query = (
            sa.select(schema.opportunities.c.id.label("opportunity_id"), similarity)
            .select_from(
                schema.opportunities.join(
                    schema.opportunity_versions,
                    schema.opportunity_versions.c.id == schema.opportunities.c.current_version_id,
                )
            )
            .where(similarity >= minimum_similarity)
            .order_by(distance, schema.opportunities.c.id)
            .limit(limit)
        )
        if exclude is not None:
            query = query.where(schema.opportunities.c.id != exclude)
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return tuple(
            SimilarityMatch(opportunity_id=row.opportunity_id, similarity=float(row.similarity))
            for row in rows
        )

    async def find_pattern_fingerprint(self, fingerprint: str) -> UUID | None:
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("pattern fingerprint must be lowercase SHA-256")
        async with self._session_factory() as session:
            return await session.scalar(
                sa.select(schema.problem_patterns.c.id)
                .where(schema.problem_patterns.c.fingerprint == fingerprint)
                .limit(1)
            )

    async def nearest_patterns(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 3,
        minimum_similarity: float = 0.0,
    ) -> tuple[PatternSimilarityMatch, ...]:
        """Compare a canonical problem vector to retained patterns using exact cosine."""

        if len(embedding) != 1024:
            raise ValueError("pattern embeddings must contain exactly 1024 dimensions")
        if not 1 <= limit <= 3:
            raise ValueError("at most three retained patterns may be compared")
        if not 0 <= minimum_similarity <= 1:
            raise ValueError("minimum similarity must be within zero and one")
        distance = schema.problem_patterns.c.embedding.cosine_distance(list(embedding))
        similarity = (1.0 - distance).label("similarity")
        query = (
            sa.select(schema.problem_patterns.c.id.label("pattern_id"), similarity)
            .where(similarity >= minimum_similarity)
            .order_by(distance, schema.problem_patterns.c.id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return tuple(
            PatternSimilarityMatch(pattern_id=row.pattern_id, similarity=float(row.similarity))
            for row in rows
        )
