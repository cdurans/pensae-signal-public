"""Atomic writer for complete validated opportunity aggregates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pensae.domain.opportunity import ScoreCard, Verdict, VerdictContext, assign_verdict
from pensae.infrastructure.db import schema
from pensae.opportunities.records import OpportunityAggregate, SimilarityRelationInput
from pensae.research.schemas import OpportunityAnalysis


class ImmutableSupportConflict(RuntimeError):
    """A durable support identifier already exists with different immutable content."""


def score_opportunity_report(report: OpportunityAnalysis) -> tuple[ScoreCard, Verdict]:
    """Return the protected deterministic score and verdict for a complete report."""

    scores = ScoreCard(
        commercial_attractiveness=report.proposed_scores.commercial_attractiveness,
        evidence_strength=report.proposed_scores.evidence_strength,
        pensae_feasibility=report.proposed_scores.pensae_feasibility,
        differentiation=report.proposed_scores.differentiation,
    )
    verdict = assign_verdict(
        scores,
        VerdictContext(
            strong_evidence=report.strong_evidence,
            plausible_buyer=report.plausible_buyer,
            payment_or_value_path=report.payment_or_value_path,
            critical_blocker=report.critical_blocker,
            strong_negative_evidence=report.strong_negative_evidence,
            implausible_economics=report.implausible_economics,
            excessive_customization_or_operations=(report.excessive_customization_or_operations),
        ),
    )
    return scores, verdict


class OpportunityWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        after_step: Callable[[str], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._after_step = after_step or (lambda _step: None)

    async def commit(self, aggregate: OpportunityAggregate) -> UUID:
        """Commit a complete opportunity; validation/network/model work has already finished."""

        scores, verdict = score_opportunity_report(aggregate.report)
        source_fingerprints = {str(item.id): item.content_fingerprint for item in aggregate.sources}
        async with self._session_factory() as session, session.begin():
            await self._insert_sources(session, aggregate)
            self._after_step("sources")
            await self._insert_evidence(session, aggregate)
            self._after_step("evidence")
            await self._insert_signals(session, aggregate)
            self._after_step("signals")
            await self._insert_pattern(session, aggregate)
            self._after_step("pattern")
            await session.execute(
                sa.insert(schema.opportunities).values(
                    id=aggregate.opportunity_id,
                    identity_fingerprint=aggregate.identity_fingerprint,
                    primary_industry=aggregate.primary_industry,
                    classification=aggregate.classification,
                )
            )
            self._after_step("opportunity")
            await session.execute(
                sa.insert(schema.opportunity_versions).values(
                    id=aggregate.version_id,
                    opportunity_id=aggregate.opportunity_id,
                    run_id=aggregate.run_id,
                    version_number=1,
                    report=aggregate.report.model_dump(mode="json"),
                    commercial_score=scores.commercial_attractiveness,
                    evidence_score=scores.evidence_strength,
                    feasibility_score=scores.pensae_feasibility,
                    differentiation_score=scores.differentiation,
                    weighted_score=scores.weighted_total(),
                    verdict=verdict.value,
                    embedding=list(aggregate.opportunity_embedding),
                    chat_model_id=aggregate.provenance.chat_model_id,
                    embedding_model_id=aggregate.provenance.embedding_model_id,
                    workflow_version=aggregate.provenance.workflow_version,
                    prompt_versions=aggregate.provenance.prompt_versions,
                    schema_version=aggregate.provenance.schema_version,
                    fingerprint_version=aggregate.provenance.fingerprint_version,
                    threshold_version=aggregate.provenance.threshold_version,
                    model_parameter_version=aggregate.provenance.model_parameter_version,
                    source_fingerprints=source_fingerprints,
                )
            )
            self._after_step("version")
            await self._insert_version_joins(session, aggregate)
            self._after_step("joins")
            relations = aggregate.relations
            if not relations and aggregate.classification != "new":
                relations = (
                    SimilarityRelationInput(
                        id=aggregate.relation_id,  # type: ignore[arg-type]
                        opportunity_id=aggregate.related_opportunity_id,  # type: ignore[arg-type]
                        similarity=aggregate.relation_similarity,  # type: ignore[arg-type]
                        relation_kind=aggregate.classification,
                    ),
                )
            if relations:
                await session.execute(
                    sa.insert(schema.opportunity_relations),
                    [
                        {
                            "id": relation.id,
                            "from_opportunity_id": aggregate.opportunity_id,
                            "to_opportunity_id": relation.opportunity_id,
                            "similarity": relation.similarity,
                            "relation_kind": relation.relation_kind,
                            "threshold_version": aggregate.provenance.threshold_version,
                        }
                        for relation in relations
                    ],
                )
            await session.execute(
                sa.insert(schema.lifecycle_events).values(
                    id=aggregate.lifecycle_event_id,
                    opportunity_id=aggregate.opportunity_id,
                    version_id=aggregate.version_id,
                    event_type=aggregate.classification,
                    event_data={
                        "classification": aggregate.classification,
                        "related_opportunity_id": (
                            str(relations[0].opportunity_id) if relations else None
                        ),
                        "related_opportunity_ids": [
                            str(relation.opportunity_id) for relation in relations
                        ],
                    },
                )
            )
            await session.execute(
                sa.update(schema.opportunities)
                .where(schema.opportunities.c.id == aggregate.opportunity_id)
                .values(current_version_id=aggregate.version_id, updated_at=sa.func.now())
            )
            self._after_step("event_and_current_version")
        return aggregate.opportunity_id

    async def _insert_sources(self, session: AsyncSession, aggregate: OpportunityAggregate) -> None:
        await self._insert_or_verify_immutable(
            session,
            table=schema.sources,
            values=[item.model_dump(mode="python") for item in aggregate.sources],
            record_kind="source",
        )

    async def _insert_evidence(
        self, session: AsyncSession, aggregate: OpportunityAggregate
    ) -> None:
        await self._insert_or_verify_immutable(
            session,
            table=schema.evidence_items,
            values=[item.model_dump(mode="python") for item in aggregate.evidence],
            record_kind="evidence",
        )

    @staticmethod
    async def _insert_or_verify_immutable(
        session: AsyncSession,
        *,
        table: sa.Table,
        values: Sequence[Mapping[str, object]],
        record_kind: str,
    ) -> None:
        """Reuse identical support IDs and fail the whole commit on any content conflict."""

        if not values:
            return
        identifiers = tuple(item["id"] for item in values)
        rows = (
            await session.execute(
                sa.select(table).where(table.c.id.in_(identifiers)).with_for_update()
            )
        ).mappings()
        existing = {row["id"]: dict(row) for row in rows}
        missing: list[Mapping[str, object]] = []
        for item in values:
            stored = existing.get(item["id"])
            if stored is None:
                missing.append(item)
                continue
            if stored != dict(item):
                raise ImmutableSupportConflict(
                    f"{record_kind} identifier conflicts with retained immutable content"
                )
        if missing:
            await session.execute(sa.insert(table), missing)

    async def _insert_signals(self, session: AsyncSession, aggregate: OpportunityAggregate) -> None:
        await session.execute(
            sa.insert(schema.problem_signals),
            [
                item.model_dump(mode="python", exclude={"evidence_ids"})
                for item in aggregate.signals
            ],
        )
        await session.execute(
            sa.insert(schema.problem_signal_evidence),
            [
                {"signal_id": signal.id, "evidence_id": evidence_id}
                for signal in aggregate.signals
                for evidence_id in signal.evidence_ids
            ],
        )

    async def _insert_pattern(self, session: AsyncSession, aggregate: OpportunityAggregate) -> None:
        pattern_values = aggregate.pattern.model_dump(mode="python", exclude={"signal_ids"})
        pattern_values["embedding"] = list(aggregate.pattern.embedding)
        await session.execute(sa.insert(schema.problem_patterns).values(**pattern_values))
        await session.execute(
            sa.insert(schema.problem_pattern_signals),
            [
                {"pattern_id": aggregate.pattern.id, "signal_id": signal_id}
                for signal_id in aggregate.pattern.signal_ids
            ],
        )

    async def _insert_version_joins(
        self, session: AsyncSession, aggregate: OpportunityAggregate
    ) -> None:
        await session.execute(
            sa.insert(schema.opportunity_version_evidence),
            [
                {"version_id": aggregate.version_id, "evidence_id": item.id}
                for item in aggregate.evidence
            ],
        )
        await session.execute(
            sa.insert(schema.opportunity_version_signals),
            [
                {"version_id": aggregate.version_id, "signal_id": item.id}
                for item in aggregate.signals
            ],
        )
        await session.execute(
            sa.insert(schema.opportunity_version_patterns).values(
                version_id=aggregate.version_id,
                pattern_id=aggregate.pattern.id,
            )
        )
