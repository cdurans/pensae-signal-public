"""Add authoritative Phase 2 run progress snapshot fields.

Revision ID: 20260722_0003
Revises: 20260722_0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260722_0003"
down_revision: str | None = "20260722_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_runs_state", "runs", type_="check")
    op.add_column(
        "runs", sa.Column("current_stage", sa.Text(), server_default="created", nullable=False)
    )
    op.add_column(
        "runs",
        sa.Column("work_counters", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column(
        "runs",
        sa.Column("warning_codes", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column(
        "runs",
        sa.Column("model_usage", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column(
        "runs", sa.Column("committed_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "runs",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_runs_state",
        "runs",
        "state IN ('running','stopping','completed','completed_with_warnings','stopped','failed')",
    )
    op.create_check_constraint("ck_runs_committed_count", "runs", "committed_count >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_runs_committed_count", "runs", type_="check")
    op.drop_constraint("ck_runs_state", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_state",
        "runs",
        "state IN ('running','completed','completed_with_warnings','stopped','failed')",
    )
    op.drop_column("runs", "updated_at")
    op.drop_column("runs", "committed_count")
    op.drop_column("runs", "warning_codes")
    op.drop_column("runs", "model_usage")
    op.drop_column("runs", "work_counters")
    op.drop_column("runs", "current_stage")
