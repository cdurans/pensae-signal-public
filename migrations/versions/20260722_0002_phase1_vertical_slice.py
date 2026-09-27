"""Create the minimum durable Phase 1 opportunity aggregate."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260722_0002"
down_revision: str | None = "20260721_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("effective_config", JSONB(), nullable=False),
        sa.Column("workflow_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('running','completed','completed_with_warnings','stopped','failed')",
            name="ck_runs_state",
        ),
    )
    op.create_table(
        "sources",
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
    op.create_table(
        "evidence_items",
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
            "char_length(excerpt) BETWEEN 1 AND 500",
            name="ck_evidence_items_excerpt_length",
        ),
    )
    op.create_table(
        "problem_signals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("affected_user", sa.Text(), nullable=False),
        sa.Column("recurring_workflow", sa.Text(), nullable=False),
        sa.Column("current_workaround", sa.Text(), nullable=False),
        sa.Column("business_consequence", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_problem_signals_confidence"),
    )
    op.create_table(
        "problem_signal_evidence",
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
    op.create_table(
        "problem_patterns",
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
    op.create_table(
        "problem_pattern_signals",
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
    op.create_table(
        "opportunities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("current_version_id", UUID(as_uuid=True)),
        sa.Column("identity_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("primary_industry", sa.Text(), nullable=False),
        sa.Column("favorite", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("merge_target_id", UUID(as_uuid=True)),
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
    )
    op.create_table(
        "opportunity_versions",
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
    op.create_foreign_key(
        "fk_opportunities_current_version",
        "opportunities",
        "opportunity_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_opportunities_merge_target",
        "opportunities",
        "opportunities",
        ["merge_target_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
    _create_version_join("opportunity_version_evidence", "evidence_id", "evidence_items")
    _create_version_join("opportunity_version_signals", "signal_id", "problem_signals")
    _create_version_join("opportunity_version_patterns", "pattern_id", "problem_patterns")
    op.create_table(
        "opportunity_relations",
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
        sa.Column("relation_kind", sa.Text(), server_default="related", nullable=False),
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
    op.create_table(
        "lifecycle_events",
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
        sa.Column("event_data", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "event_type IN ('new','related','possible_rediscovery','rediscovered')",
            name="ck_lifecycle_events_type",
        ),
    )


def _create_version_join(table_name: str, target_column: str, target_table: str) -> None:
    op.create_table(
        table_name,
        sa.Column(
            "version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("opportunity_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            target_column,
            UUID(as_uuid=True),
            sa.ForeignKey(f"{target_table}.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("lifecycle_events")
    op.drop_table("opportunity_relations")
    op.drop_table("opportunity_version_patterns")
    op.drop_table("opportunity_version_signals")
    op.drop_table("opportunity_version_evidence")
    op.drop_constraint("fk_opportunities_merge_target", "opportunities", type_="foreignkey")
    op.drop_constraint("fk_opportunities_current_version", "opportunities", type_="foreignkey")
    op.drop_table("opportunity_versions")
    op.drop_table("opportunities")
    op.drop_table("problem_pattern_signals")
    op.drop_table("problem_patterns")
    op.drop_table("problem_signal_evidence")
    op.drop_table("problem_signals")
    op.drop_table("evidence_items")
    op.drop_table("sources")
    op.drop_table("runs")
