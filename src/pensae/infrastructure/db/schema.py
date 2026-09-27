"""Durable SQLAlchemy Core schema; PostgreSQL remains authoritative."""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData()

settings = sa.Table(
    "settings",
    metadata,
    sa.Column("id", sa.SmallInteger(), primary_key=True),
    sa.Column("values", JSONB(), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("id = 1", name="ck_settings_singleton"),
    sa.CheckConstraint("revision >= 1", name="ck_settings_revision"),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("state", sa.Text(), nullable=False),
    sa.Column("effective_config", JSONB(), nullable=False),
    sa.Column("workflow_version", sa.Text(), nullable=False),
    sa.Column("schema_version", sa.Text(), nullable=False),
    sa.Column("current_stage", sa.Text(), nullable=False, server_default="created"),
    sa.Column("work_counters", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("warning_codes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("model_usage", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("committed_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "state IN ('running','stopping','completed','completed_with_warnings','stopped','failed')",
        name="ck_runs_state",
    ),
    sa.CheckConstraint("committed_count >= 0", name="ck_runs_committed_count"),
)

sources = sa.Table(
    "sources",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("url", sa.Text(), nullable=False),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column("publisher", sa.Text()),
    sa.Column("publication_date", sa.Date()),
    sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("credibility_note", sa.Text(), nullable=False),
    sa.Column("limitation", sa.Text()),
    sa.Column("content_fingerprint", sa.String(64), nullable=False),
    sa.CheckConstraint(
        "char_length(content_fingerprint) = 64", name="ck_sources_fingerprint_length"
    ),
)

evidence_items = sa.Table(
    "evidence_items",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "source_id",
        UUID(as_uuid=True),
        sa.ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("excerpt", sa.String(500), nullable=False),
    sa.Column("supported_claim", sa.Text(), nullable=False),
    sa.Column("evidence_kind", sa.Text(), nullable=False),
    sa.Column("material_conflict", sa.Text()),
    sa.CheckConstraint(
        "evidence_kind IN ('supporting','negative','conflicting')",
        name="ck_evidence_items_kind",
    ),
    sa.CheckConstraint(
        "char_length(excerpt) BETWEEN 1 AND 500", name="ck_evidence_items_excerpt_length"
    ),
)

problem_signals = sa.Table(
    "problem_signals",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("affected_user", sa.Text(), nullable=False),
    sa.Column("recurring_workflow", sa.Text(), nullable=False),
    sa.Column("current_workaround", sa.Text(), nullable=False),
    sa.Column("business_consequence", sa.Text(), nullable=False),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
    sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_problem_signals_confidence"),
)

problem_signal_evidence = sa.Table(
    "problem_signal_evidence",
    metadata,
    sa.Column(
        "signal_id",
        UUID(as_uuid=True),
        sa.ForeignKey("problem_signals.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "evidence_id",
        UUID(as_uuid=True),
        sa.ForeignKey("evidence_items.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

problem_patterns = sa.Table(
    "problem_patterns",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("summary", sa.Text(), nullable=False),
    sa.Column("fingerprint", sa.String(64), nullable=False),
    sa.Column("embedding", VECTOR(1024), nullable=False),
    sa.Column("embedding_model_id", sa.Text(), nullable=False),
    sa.Column("fingerprint_version", sa.Text(), nullable=False),
    sa.CheckConstraint(
        "char_length(fingerprint) = 64", name="ck_problem_patterns_fingerprint_length"
    ),
)

problem_pattern_signals = sa.Table(
    "problem_pattern_signals",
    metadata,
    sa.Column(
        "pattern_id",
        UUID(as_uuid=True),
        sa.ForeignKey("problem_patterns.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "signal_id",
        UUID(as_uuid=True),
        sa.ForeignKey("problem_signals.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

opportunities = sa.Table(
    "opportunities",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("current_version_id", UUID(as_uuid=True)),
    sa.Column("identity_fingerprint", sa.String(64), nullable=False, unique=True),
    sa.Column("primary_industry", sa.Text(), nullable=False),
    sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("note", sa.Text()),
    sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("lifecycle_status", sa.Text(), nullable=False, server_default="active"),
    sa.Column("classification", sa.Text(), nullable=False, server_default="new"),
    sa.Column("merge_target_id", UUID(as_uuid=True)),
    sa.Column("rediscovery_target_id", UUID(as_uuid=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "char_length(identity_fingerprint) = 64",
        name="ck_opportunities_identity_fingerprint_length",
    ),
    sa.CheckConstraint(
        "note IS NULL OR char_length(note) <= 4000",
        name="ck_opportunities_note_length",
    ),
    sa.CheckConstraint("revision >= 1", name="ck_opportunities_revision"),
    sa.CheckConstraint(
        "lifecycle_status IN ('active','merged')",
        name="ck_opportunities_lifecycle_status",
    ),
    sa.CheckConstraint(
        "classification IN ('new','related','possible_rediscovery','rediscovered','updated')",
        name="ck_opportunities_classification",
    ),
    sa.CheckConstraint(
        "merge_target_id IS NULL OR merge_target_id <> id",
        name="ck_opportunities_merge_not_self",
    ),
    sa.CheckConstraint(
        "rediscovery_target_id IS NULL OR rediscovery_target_id <> id",
        name="ck_opportunities_rediscovery_not_self",
    ),
)

opportunity_versions = sa.Table(
    "opportunity_versions",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "opportunity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "run_id",
        UUID(as_uuid=True),
        sa.ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("version_number", sa.Integer(), nullable=False),
    sa.Column("report", JSONB(), nullable=False),
    sa.Column("commercial_score", sa.SmallInteger(), nullable=False),
    sa.Column("evidence_score", sa.SmallInteger(), nullable=False),
    sa.Column("feasibility_score", sa.SmallInteger(), nullable=False),
    sa.Column("differentiation_score", sa.SmallInteger(), nullable=False),
    sa.Column("weighted_score", sa.Numeric(3, 2), nullable=False),
    sa.Column("verdict", sa.Text(), nullable=False),
    sa.Column("embedding", VECTOR(1024), nullable=False),
    sa.Column("chat_model_id", sa.Text(), nullable=False),
    sa.Column("embedding_model_id", sa.Text(), nullable=False),
    sa.Column("workflow_version", sa.Text(), nullable=False),
    sa.Column("prompt_versions", JSONB(), nullable=False),
    sa.Column("schema_version", sa.Text(), nullable=False),
    sa.Column("fingerprint_version", sa.Text(), nullable=False),
    sa.Column("threshold_version", sa.Text(), nullable=False),
    sa.Column("model_parameter_version", sa.Text(), nullable=False),
    sa.Column("source_fingerprints", JSONB(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("opportunity_id", "version_number", name="uq_version_number"),
    sa.CheckConstraint(
        "commercial_score BETWEEN 1 AND 5 AND evidence_score BETWEEN 1 AND 5 "
        "AND feasibility_score BETWEEN 1 AND 5 AND differentiation_score BETWEEN 1 AND 5",
        name="ck_opportunity_versions_scores",
    ),
    sa.CheckConstraint(
        "weighted_score BETWEEN 1 AND 5", name="ck_opportunity_versions_weighted_score"
    ),
    sa.CheckConstraint(
        "verdict IN ('promising','needs_more_evidence','do_not_pursue')",
        name="ck_opportunity_versions_verdict",
    ),
)

opportunities.append_constraint(
    sa.ForeignKeyConstraint(
        [opportunities.c.current_version_id],
        [opportunity_versions.c.id],
        name="fk_opportunities_current_version",
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
)
opportunities.append_constraint(
    sa.ForeignKeyConstraint(
        [opportunities.c.rediscovery_target_id],
        [opportunities.c.id],
        name="fk_opportunities_rediscovery_target",
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
)
opportunities.append_constraint(
    sa.ForeignKeyConstraint(
        [opportunities.c.merge_target_id],
        [opportunities.c.id],
        name="fk_opportunities_merge_target",
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
)

opportunity_version_evidence = sa.Table(
    "opportunity_version_evidence",
    metadata,
    sa.Column(
        "version_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunity_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "evidence_id",
        UUID(as_uuid=True),
        sa.ForeignKey("evidence_items.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)

opportunity_version_signals = sa.Table(
    "opportunity_version_signals",
    metadata,
    sa.Column(
        "version_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunity_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "signal_id",
        UUID(as_uuid=True),
        sa.ForeignKey("problem_signals.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)

opportunity_version_patterns = sa.Table(
    "opportunity_version_patterns",
    metadata,
    sa.Column(
        "version_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunity_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "pattern_id",
        UUID(as_uuid=True),
        sa.ForeignKey("problem_patterns.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)

opportunity_relations = sa.Table(
    "opportunity_relations",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "from_opportunity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "to_opportunity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("similarity", sa.Numeric(6, 5), nullable=False),
    sa.Column("relation_kind", sa.Text(), nullable=False, server_default=sa.text("'related'")),
    sa.Column("threshold_version", sa.Text(), nullable=False),
    sa.UniqueConstraint(
        "from_opportunity_id", "to_opportunity_id", name="uq_opportunity_relation_pair"
    ),
    sa.CheckConstraint("from_opportunity_id <> to_opportunity_id", name="ck_relation_not_self"),
    sa.CheckConstraint("similarity BETWEEN 0 AND 1", name="ck_relation_similarity"),
    sa.CheckConstraint(
        "relation_kind IN ('related','possible_rediscovery')", name="ck_relation_kind"
    ),
)

lifecycle_events = sa.Table(
    "lifecycle_events",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "opportunity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "version_id",
        UUID(as_uuid=True),
        sa.ForeignKey("opportunity_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("event_data", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "event_type IN "
        "('new','related','possible_rediscovery','rediscovered','related_confirmed',"
        "'updated','updated_source','merged','merge_reversed')",
        name="ck_lifecycle_events_type",
    ),
)

operational_audit = sa.Table(
    "operational_audit",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("action", sa.Text(), nullable=False),
    sa.Column("target_id", UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("action IN ('permanent_delete')", name="ck_operational_audit_action"),
    sa.CheckConstraint("status IN ('completed')", name="ck_operational_audit_status"),
)

sa.Index("ix_opportunities_primary_industry", opportunities.c.primary_industry)
sa.Index(
    "ix_opportunity_versions_portfolio_order",
    opportunity_versions.c.verdict,
    opportunity_versions.c.weighted_score,
    opportunity_versions.c.evidence_score,
    opportunity_versions.c.created_at,
)
