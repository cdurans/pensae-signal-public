"""Public capability facade for opportunity and run persistence."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pensae.opportunities.reader import OpportunityReader
from pensae.opportunities.records import (
    EvidenceDetail,
    EvidenceInput,
    OpportunityAggregate,
    OpportunityDetail,
    OpportunityVersionSummary,
    PatternInput,
    PatternSimilarityMatch,
    ProblemPatternDetail,
    ProblemSignalDetail,
    ProvenanceInput,
    RecoverySummary,
    RelatedOpportunityDetail,
    RunDetail,
    RunSnapshot,
    SignalInput,
    SimilarityMatch,
    SimilarityRelationInput,
    SourceInput,
)
from pensae.opportunities.run_store import RunStore
from pensae.opportunities.similarity import SimilarityStore
from pensae.opportunities.writer import OpportunityWriter
from pensae.research.workflow import ModelCallUsage

RunState = Literal[
    "running", "stopping", "completed", "completed_with_warnings", "stopped", "failed"
]


class OpportunityAggregateStore:
    """Stable product facade over focused persistence capabilities."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        after_step: Callable[[str], None] | None = None,
    ) -> None:
        self._runs = RunStore(session_factory)
        self._writer = OpportunityWriter(session_factory, after_step=after_step)
        self._reader = OpportunityReader(session_factory)
        self._similarity = SimilarityStore(session_factory)

    async def create_run(self, snapshot: RunSnapshot) -> UUID:
        return await self._runs.create_run(snapshot)

    async def set_run_state(self, run_id: UUID, state: RunState) -> None:
        await self._runs.set_run_state(run_id, state)

    async def delete_empty_run(self, run_id: UUID) -> bool:
        return await self._runs.delete_empty_run(run_id)

    async def update_run_progress(
        self,
        run_id: UUID,
        *,
        state: RunState,
        stage: str,
        counters: Mapping[str, int],
        warning_codes: tuple[str, ...],
        committed_count: int,
        model_usage: tuple[ModelCallUsage, ...] = (),
    ) -> None:
        await self._runs.update_run_progress(
            run_id,
            state=state,
            stage=stage,
            counters=counters,
            warning_codes=warning_codes,
            committed_count=committed_count,
            model_usage=model_usage,
        )

    async def commit(self, aggregate: OpportunityAggregate) -> UUID:
        return await self._writer.commit(aggregate)

    async def get_detail(self, opportunity_id: UUID) -> OpportunityDetail | None:
        return await self._reader.get_detail(opportunity_id)

    async def get_version_detail(
        self, opportunity_id: UUID, version_id: UUID
    ) -> OpportunityDetail | None:
        return await self._reader.get_detail(opportunity_id, version_id=version_id)

    async def get_run(self, run_id: UUID) -> RunDetail | None:
        return await self._runs.get_run(run_id)

    async def resolve_abandoned_runs(self) -> RecoverySummary:
        return await self._runs.resolve_abandoned_runs()

    async def nearest_problem(
        self, embedding: Sequence[float], *, exclude: UUID | None = None
    ) -> SimilarityMatch | None:
        return await self._similarity.nearest_problem(embedding, exclude=exclude)

    async def find_identity(self, fingerprint: str) -> UUID | None:
        return await self._similarity.find_identity(fingerprint)

    async def record_rediscovery(
        self,
        *,
        run_id: UUID,
        opportunity_id: UUID,
        lifecycle_event_id: UUID,
    ) -> None:
        await self._similarity.record_rediscovery(
            run_id=run_id,
            opportunity_id=opportunity_id,
            lifecycle_event_id=lifecycle_event_id,
        )

    async def nearest_problems(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 3,
        minimum_similarity: float = 0.0,
        exclude: UUID | None = None,
    ) -> tuple[SimilarityMatch, ...]:
        return await self._similarity.nearest_problems(
            embedding,
            limit=limit,
            minimum_similarity=minimum_similarity,
            exclude=exclude,
        )

    async def find_pattern_fingerprint(self, fingerprint: str) -> UUID | None:
        return await self._similarity.find_pattern_fingerprint(fingerprint)

    async def nearest_patterns(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 3,
        minimum_similarity: float = 0.0,
    ) -> tuple[PatternSimilarityMatch, ...]:
        return await self._similarity.nearest_patterns(
            embedding,
            limit=limit,
            minimum_similarity=minimum_similarity,
        )


__all__ = [
    "EvidenceDetail",
    "EvidenceInput",
    "OpportunityAggregate",
    "OpportunityAggregateStore",
    "OpportunityDetail",
    "OpportunityVersionSummary",
    "PatternInput",
    "PatternSimilarityMatch",
    "ProblemPatternDetail",
    "ProblemSignalDetail",
    "ProvenanceInput",
    "RecoverySummary",
    "RelatedOpportunityDetail",
    "RunDetail",
    "RunSnapshot",
    "SignalInput",
    "SimilarityMatch",
    "SimilarityRelationInput",
    "SourceInput",
]
