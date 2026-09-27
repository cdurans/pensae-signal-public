from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from pensae.domain.opportunity import (
    OpportunityIdentity,
    ProblemSimilarityInput,
    ScoreCard,
    VerdictContext,
)
from pensae.opportunities.aggregate import OpportunityAggregate
from pensae.opportunities.portfolio import (
    PortfolioQuery,
    PortfolioSort,
    build_favorite_update,
    build_note_update,
    build_portfolio_statement,
)
from pensae.research.schemas import OpportunityAnalysis

OPPORTUNITY_ID = UUID("00000000-0000-0000-0000-000000000456")
METADATA_FIELDS = {"favorite", "note"}


def _postgres_sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.parametrize(
    "protected_model",
    [
        OpportunityAnalysis,
        ScoreCard,
        VerdictContext,
        OpportunityIdentity,
        ProblemSimilarityInput,
        OpportunityAggregate,
    ],
)
def test_operator_metadata_is_absent_from_analysis_and_policy_inputs(
    protected_model: type,
) -> None:
    fields = set(protected_model.model_fields)

    assert fields.isdisjoint(METADATA_FIELDS)


@pytest.mark.parametrize("sort", list(PortfolioSort))
def test_operator_metadata_never_enters_any_ranking_order(sort: PortfolioSort) -> None:
    sql = _postgres_sql(build_portfolio_statement(PortfolioQuery(sort=sort)))
    order = sql.split(" ORDER BY ", maxsplit=1)[1]

    assert "favorite" not in order
    assert "note" not in order
    assert order.rstrip().endswith("opportunities.id ASC") or (
        "opportunities.id ASC" in order.split(" LIMIT ", maxsplit=1)[0]
    )


def test_metadata_mutations_cannot_write_immutable_or_research_inputs() -> None:
    statements = (
        build_favorite_update(OPPORTUNITY_ID, favorite=True),
        build_note_update(OPPORTUNITY_ID, note="Remember the buyer interview gap."),
    )
    prohibited_assignments = {
        "report",
        "commercial_score",
        "evidence_score",
        "feasibility_score",
        "differentiation_score",
        "weighted_score",
        "verdict",
        "identity_fingerprint",
        "embedding",
        "relation_kind",
        "event_type",
        "prompt_versions",
        "chat_model_id",
        "embedding_model_id",
    }

    for statement in statements:
        sql = _postgres_sql(statement)
        assigned = sql.split(" SET ", maxsplit=1)[1].split(" WHERE ", maxsplit=1)[0]
        assert sql.startswith("UPDATE opportunities SET ")
        assert not any(column in assigned for column in prohibited_assignments)
        assert "opportunity_versions" not in sql
        assert "opportunity_relations" not in sql
        assert "lifecycle_events" not in sql
