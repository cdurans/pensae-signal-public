"""Complete opportunity-detail query capability."""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pensae.infrastructure.db import schema
from pensae.opportunities.records import (
    EvidenceDetail,
    LifecycleEventDetail,
    OpportunityDetail,
    OpportunityVersionSummary,
    ProblemPatternDetail,
    ProblemSignalDetail,
    RelatedOpportunityDetail,
)


class OpportunityReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_detail(
        self, opportunity_id: UUID, *, version_id: UUID | None = None
    ) -> OpportunityDetail | None:
        async with self._session_factory() as session:
            version_filter = (
                schema.opportunity_versions.c.id == version_id
                if version_id is not None
                else schema.opportunity_versions.c.id == schema.opportunities.c.current_version_id
            )
            header = (
                (
                    await session.execute(
                        sa.select(
                            schema.opportunities.c.id.label("opportunity_id"),
                            schema.opportunities.c.current_version_id,
                            schema.opportunity_versions.c.id.label("selected_version_id"),
                            schema.opportunities.c.primary_industry,
                            schema.opportunities.c.favorite,
                            schema.opportunities.c.note,
                            schema.opportunities.c.revision,
                            schema.opportunities.c.lifecycle_status,
                            schema.opportunities.c.classification,
                            schema.opportunities.c.merge_target_id,
                            schema.opportunities.c.rediscovery_target_id,
                            schema.opportunity_versions.c.version_number,
                            schema.opportunity_versions.c.report,
                            schema.opportunity_versions.c.commercial_score,
                            schema.opportunity_versions.c.evidence_score,
                            schema.opportunity_versions.c.feasibility_score,
                            schema.opportunity_versions.c.differentiation_score,
                            schema.opportunity_versions.c.weighted_score,
                            schema.opportunity_versions.c.verdict,
                            schema.opportunity_versions.c.chat_model_id,
                            schema.opportunity_versions.c.embedding_model_id,
                            schema.opportunity_versions.c.workflow_version,
                            schema.opportunity_versions.c.prompt_versions,
                            schema.opportunity_versions.c.schema_version,
                            schema.opportunity_versions.c.fingerprint_version,
                            schema.opportunity_versions.c.threshold_version,
                            schema.opportunity_versions.c.model_parameter_version,
                            schema.opportunity_versions.c.source_fingerprints,
                            schema.opportunity_versions.c.created_at,
                        )
                        .join(
                            schema.opportunity_versions,
                            schema.opportunity_versions.c.opportunity_id
                            == schema.opportunities.c.id,
                        )
                        .where(
                            schema.opportunities.c.id == opportunity_id,
                            version_filter,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if header is None:
                return None
            rows = (
                await session.execute(
                    sa.select(
                        schema.evidence_items.c.id,
                        schema.evidence_items.c.excerpt,
                        schema.evidence_items.c.supported_claim,
                        schema.evidence_items.c.evidence_kind,
                        schema.evidence_items.c.material_conflict,
                        schema.sources.c.url,
                        schema.sources.c.title,
                        schema.sources.c.publisher,
                        schema.sources.c.retrieved_at,
                    )
                    .join(
                        schema.opportunity_version_evidence,
                        schema.opportunity_version_evidence.c.evidence_id
                        == schema.evidence_items.c.id,
                    )
                    .join(schema.sources, schema.sources.c.id == schema.evidence_items.c.source_id)
                    .where(
                        schema.opportunity_version_evidence.c.version_id
                        == header["selected_version_id"]
                    )
                    .order_by(schema.evidence_items.c.id)
                )
            ).mappings()
            evidence = tuple(
                EvidenceDetail(
                    id=row["id"],
                    excerpt=row["excerpt"],
                    supported_claim=row["supported_claim"],
                    evidence_kind=row["evidence_kind"],
                    material_conflict=row["material_conflict"],
                    source_url=row["url"],
                    source_title=row["title"],
                    publisher=row["publisher"],
                    retrieved_at=row["retrieved_at"],
                )
                for row in rows
            )
            pattern_row = (
                (
                    await session.execute(
                        sa.select(
                            schema.problem_patterns.c.id,
                            schema.problem_patterns.c.summary,
                        )
                        .join(
                            schema.opportunity_version_patterns,
                            schema.opportunity_version_patterns.c.pattern_id
                            == schema.problem_patterns.c.id,
                        )
                        .where(
                            schema.opportunity_version_patterns.c.version_id
                            == header["selected_version_id"]
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            signal_rows = (
                await session.execute(
                    sa.select(
                        schema.problem_signals.c.id,
                        schema.problem_signals.c.affected_user,
                        schema.problem_signals.c.recurring_workflow,
                        schema.problem_signals.c.current_workaround,
                        schema.problem_signals.c.business_consequence,
                        schema.problem_signals.c.confidence,
                    )
                    .join(
                        schema.opportunity_version_signals,
                        schema.opportunity_version_signals.c.signal_id
                        == schema.problem_signals.c.id,
                    )
                    .where(
                        schema.opportunity_version_signals.c.version_id
                        == header["selected_version_id"]
                    )
                    .order_by(schema.problem_signals.c.id)
                )
            ).mappings()
            version_rows = (
                await session.execute(
                    sa.select(
                        schema.opportunity_versions.c.id,
                        schema.opportunity_versions.c.version_number,
                        schema.opportunity_versions.c.verdict,
                        schema.opportunity_versions.c.weighted_score,
                        schema.opportunity_versions.c.evidence_score,
                        schema.opportunity_versions.c.created_at,
                    )
                    .where(schema.opportunity_versions.c.opportunity_id == opportunity_id)
                    .order_by(
                        schema.opportunity_versions.c.version_number.desc(),
                        schema.opportunity_versions.c.id,
                    )
                )
            ).mappings()
            target_opportunities = schema.opportunities.alias("related_opportunities")
            target_versions = schema.opportunity_versions.alias("related_versions")
            relation_rows = (
                await session.execute(
                    sa.select(
                        schema.opportunity_relations.c.to_opportunity_id,
                        schema.opportunity_relations.c.similarity,
                        schema.opportunity_relations.c.relation_kind,
                        target_opportunities.c.primary_industry,
                        target_opportunities.c.revision,
                        target_opportunities.c.lifecycle_status,
                        target_versions.c.report,
                    )
                    .join(
                        target_opportunities,
                        target_opportunities.c.id
                        == schema.opportunity_relations.c.to_opportunity_id,
                    )
                    .join(
                        target_versions,
                        target_versions.c.id == target_opportunities.c.current_version_id,
                    )
                    .where(schema.opportunity_relations.c.from_opportunity_id == opportunity_id)
                    .order_by(
                        schema.opportunity_relations.c.similarity.desc(),
                        schema.opportunity_relations.c.to_opportunity_id,
                    )
                    .limit(3)
                )
            ).mappings()
            event_rows = (
                await session.execute(
                    sa.select(
                        schema.lifecycle_events.c.id,
                        schema.lifecycle_events.c.event_type,
                        schema.lifecycle_events.c.event_data,
                        schema.lifecycle_events.c.version_id,
                        schema.lifecycle_events.c.created_at,
                    )
                    .where(schema.lifecycle_events.c.opportunity_id == opportunity_id)
                    .order_by(
                        schema.lifecycle_events.c.created_at.desc(),
                        schema.lifecycle_events.c.id.desc(),
                    )
                )
            ).mappings()
            return OpportunityDetail(
                id=header["opportunity_id"],
                version_id=header["selected_version_id"],
                version_number=header["version_number"],
                primary_industry=header["primary_industry"],
                revision=header["revision"],
                lifecycle_status=header["lifecycle_status"],
                classification=header["classification"],
                merge_target_id=header["merge_target_id"],
                rediscovery_target_id=header["rediscovery_target_id"],
                report=header["report"],
                scores={
                    "commercial_attractiveness": header["commercial_score"],
                    "evidence_strength": header["evidence_score"],
                    "pensae_feasibility": header["feasibility_score"],
                    "differentiation": header["differentiation_score"],
                    "weighted": header["weighted_score"],
                },
                verdict=header["verdict"],
                evidence=evidence,
                favorite=header["favorite"],
                note=header["note"],
                origin_pattern=(
                    ProblemPatternDetail.model_validate(pattern_row)
                    if pattern_row is not None
                    else None
                ),
                origin_signals=tuple(
                    ProblemSignalDetail.model_validate(row) for row in signal_rows
                ),
                related=tuple(
                    RelatedOpportunityDetail(
                        opportunity_id=row["to_opportunity_id"],
                        opportunity_name=str(row["report"]["opportunity_name"]),
                        primary_industry=row["primary_industry"],
                        similarity=row["similarity"],
                        relation_kind=row["relation_kind"],
                        revision=row["revision"],
                        lifecycle_status=row["lifecycle_status"],
                    )
                    for row in relation_rows
                ),
                versions=tuple(
                    OpportunityVersionSummary(
                        **row,
                        is_current=row["id"] == header["current_version_id"],
                    )
                    for row in version_rows
                ),
                lifecycle_events=tuple(
                    LifecycleEventDetail.model_validate(row) for row in event_rows
                ),
                provenance={
                    "chat_model_id": header["chat_model_id"],
                    "embedding_model_id": header["embedding_model_id"],
                    "workflow_version": header["workflow_version"],
                    "prompt_versions": header["prompt_versions"],
                    "schema_version": header["schema_version"],
                    "fingerprint_version": header["fingerprint_version"],
                    "threshold_version": header["threshold_version"],
                    "model_parameter_version": header["model_parameter_version"],
                    "source_fingerprints": header["source_fingerprints"],
                    "created_at": header["created_at"],
                },
            )
