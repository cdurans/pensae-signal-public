"""Add durable saved settings and bounded portfolio metadata constraints.

Revision ID: 20260722_0004
Revises: 20260722_0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260722_0004"
down_revision: str | None = "20260722_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("values", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_settings_singleton"),
        sa.CheckConstraint("revision >= 1", name="ck_settings_revision"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO settings (id, values, revision)
            VALUES (1, CAST(:values AS jsonb), 1)
            """
        ).bindparams(
            values="""{
              "research": {
                "focus": null,
                "country": "United States",
                "language": "English",
                "discovery_mode": "broad",
                "preferred_technologies": ["Python", "TypeScript", "C++"]
              },
              "endpoints": {
                "searxng_url": "http://127.0.0.1:8888",
                "chat_url": "http://127.0.0.1:8085",
                "embedding_url": "http://127.0.0.1:8086"
              },
              "workflow": {
                "discovery_queries": 8,
                "results_per_query": 10,
                "unique_urls": 60,
                "first_pass_pages": 24,
                "run_queries": 20,
                "run_pages": 48,
                "signals": 30,
                "patterns": 10,
                "segments_per_pattern": 3,
                "preliminary_survivors": 6,
                "concepts": 4,
                "focused_queries_per_concept": 3,
                "focused_pages_per_concept": 6,
                "opportunities": 4,
                "search_retries": 1,
                "model_calls": 32,
                "total_run_tokens": 300000,
                "search_timeout_seconds": 30,
                "model_timeout_seconds": 120,
                "embedding_timeout_seconds": 60,
                "planner_output_max_tokens": 2048,
                "problem_analyst_output_max_tokens": 4096,
                "product_strategist_output_max_tokens": 4096,
                "opportunity_analyst_output_max_tokens": 6144
              },
              "logging": {
                "rotation_size_mib": 10,
                "retained_files": 5
              }
            }"""
        )
    )
    op.create_check_constraint(
        "ck_opportunities_note_length",
        "opportunities",
        "note IS NULL OR char_length(note) <= 4000",
    )
    op.create_index("ix_opportunities_primary_industry", "opportunities", ["primary_industry"])
    op.create_index(
        "ix_opportunity_versions_portfolio_order",
        "opportunity_versions",
        ["verdict", "weighted_score", "evidence_score", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_versions_portfolio_order", table_name="opportunity_versions")
    op.drop_index("ix_opportunities_primary_industry", table_name="opportunities")
    op.drop_constraint("ck_opportunities_note_length", "opportunities", type_="check")
    op.drop_table("settings")
