"""Add constrained lifecycle state and content-free deletion audit metadata.

Revision ID: 20260722_0005
Revises: 20260722_0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260722_0005"
down_revision: str | None = "20260722_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "opportunities",
        sa.Column("lifecycle_status", sa.Text(), server_default="active", nullable=False),
    )
    op.add_column(
        "opportunities",
        sa.Column("classification", sa.Text(), server_default="new", nullable=False),
    )
    op.add_column(
        "opportunities",
        sa.Column("rediscovery_target_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint("ck_opportunities_revision", "opportunities", "revision >= 1")
    op.create_check_constraint(
        "ck_opportunities_lifecycle_status",
        "opportunities",
        "lifecycle_status IN ('active','merged')",
    )
    op.create_check_constraint(
        "ck_opportunities_classification",
        "opportunities",
        "classification IN ('new','related','possible_rediscovery','rediscovered','updated')",
    )
    op.create_check_constraint(
        "ck_opportunities_merge_not_self",
        "opportunities",
        "merge_target_id IS NULL OR merge_target_id <> id",
    )
    op.create_check_constraint(
        "ck_opportunities_rediscovery_not_self",
        "opportunities",
        "rediscovery_target_id IS NULL OR rediscovery_target_id <> id",
    )
    op.create_foreign_key(
        "fk_opportunities_rediscovery_target",
        "opportunities",
        "opportunities",
        ["rediscovery_target_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        sa.text(
            """
            UPDATE opportunities AS opportunity
            SET classification = COALESCE(
                (
                    SELECT event_type
                    FROM lifecycle_events
                    WHERE opportunity_id = opportunity.id
                      AND event_type IN ('rediscovered','possible_rediscovery','related','new')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ),
                'new'
            )
            """
        )
    )
    op.drop_constraint("ck_lifecycle_events_type", "lifecycle_events", type_="check")
    op.create_check_constraint(
        "ck_lifecycle_events_type",
        "lifecycle_events",
        "event_type IN "
        "('new','related','possible_rediscovery','rediscovered','related_confirmed',"
        "'updated','updated_source','merged','merge_reversed')",
    )
    op.create_table(
        "operational_audit",
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


def downgrade() -> None:
    op.drop_table("operational_audit")
    op.drop_constraint("ck_lifecycle_events_type", "lifecycle_events", type_="check")
    op.create_check_constraint(
        "ck_lifecycle_events_type",
        "lifecycle_events",
        "event_type IN ('new','related','possible_rediscovery','rediscovered')",
    )
    op.drop_constraint("fk_opportunities_rediscovery_target", "opportunities", type_="foreignkey")
    op.drop_constraint("ck_opportunities_rediscovery_not_self", "opportunities", type_="check")
    op.drop_constraint("ck_opportunities_merge_not_self", "opportunities", type_="check")
    op.drop_constraint("ck_opportunities_classification", "opportunities", type_="check")
    op.drop_constraint("ck_opportunities_lifecycle_status", "opportunities", type_="check")
    op.drop_constraint("ck_opportunities_revision", "opportunities", type_="check")
    op.drop_column("opportunities", "rediscovery_target_id")
    op.drop_column("opportunities", "classification")
    op.drop_column("opportunities", "lifecycle_status")
    op.drop_column("opportunities", "revision")
