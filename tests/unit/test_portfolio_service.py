from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from pensae.opportunities.portfolio import (
    OPPORTUNITY_NOTE_MAX_LENGTH,
    PORTFOLIO_DEFAULT_LIMIT,
    PORTFOLIO_MAX_LIMIT,
    PortfolioQuery,
    PortfolioSort,
    build_favorite_update,
    build_note_update,
    build_portfolio_statement,
    normalize_note,
)

OPPORTUNITY_ID = UUID("00000000-0000-0000-0000-000000000123")


def _postgres_sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_portfolio_query_exposes_only_the_supported_filter_sorts_and_bound() -> None:
    assert {item.value for item in PortfolioSort} == {
        "default",
        "newest",
        "weighted_score_desc",
        "evidence_score_desc",
    }
    query = PortfolioQuery(industry="  Property\t management  ")
    assert query.industry == "Property management"
    assert query.limit == PORTFOLIO_DEFAULT_LIMIT

    with pytest.raises(ValidationError, match="extra_forbidden"):
        PortfolioQuery.model_validate({"target_segment": "Small firms"})
    with pytest.raises(ValidationError, match="Input should be"):
        PortfolioQuery.model_validate({"sort": "favorite"})
    with pytest.raises(ValidationError, match="less than or equal"):
        PortfolioQuery(limit=PORTFOLIO_MAX_LIMIT + 1)


def test_default_order_matches_the_frozen_priority_and_has_a_uuid_tie_break() -> None:
    sql = _postgres_sql(build_portfolio_statement(PortfolioQuery()))
    order = sql.split(" ORDER BY ", maxsplit=1)[1]

    expected_in_order = (
        "CASE",
        "opportunity_versions.weighted_score DESC",
        "opportunity_versions.evidence_score DESC",
        "opportunity_versions.created_at DESC",
        "opportunities.id ASC",
    )
    positions = [order.index(fragment) for fragment in expected_in_order]
    assert positions == sorted(positions)
    assert "opportunity_versions.verdict = 'promising'" in order
    assert "opportunity_versions.verdict = 'needs_more_evidence'" in order
    assert "opportunity_versions.verdict = 'do_not_pursue'" in order


@pytest.mark.parametrize(
    ("sort", "leading_order"),
    [
        (PortfolioSort.NEWEST, "opportunity_versions.created_at DESC"),
        (
            PortfolioSort.WEIGHTED_SCORE_DESC,
            "opportunity_versions.weighted_score DESC",
        ),
        (
            PortfolioSort.EVIDENCE_SCORE_DESC,
            "opportunity_versions.evidence_score DESC",
        ),
    ],
)
def test_non_default_sorts_are_deterministic(sort: PortfolioSort, leading_order: str) -> None:
    sql = _postgres_sql(build_portfolio_statement(PortfolioQuery(sort=sort)))
    order = sql.split(" ORDER BY ", maxsplit=1)[1]

    assert order.startswith(leading_order)
    assert "opportunities.id ASC" in order
    assert "favorite" not in order
    assert "note" not in order


def test_industry_is_the_only_predicate_and_limit_is_always_present() -> None:
    sql = _postgres_sql(
        build_portfolio_statement(
            PortfolioQuery(industry="Cafe\N{COMBINING ACUTE ACCENT} services", limit=7)
        )
    )

    assert "lower(opportunities.primary_industry) = 'café services'" in sql
    assert " LIMIT 7" in sql
    where = sql.split("WHERE ", maxsplit=1)[1].split("ORDER BY ", maxsplit=1)[0].strip()
    assert where == "lower(opportunities.primary_industry) = 'café services'"


def test_note_normalization_is_deterministic_and_enforces_the_durable_bound() -> None:
    assert normalize_note("  Cafe\N{COMBINING ACUTE ACCENT}\r\nsecond line\r  ") == (
        "Café\nsecond line"
    )
    assert normalize_note(" \r\n\t ") is None
    assert normalize_note(None) is None
    assert normalize_note("x" * OPPORTUNITY_NOTE_MAX_LENGTH) == ("x" * OPPORTUNITY_NOTE_MAX_LENGTH)
    with pytest.raises(ValueError, match="cannot exceed 4000"):
        normalize_note("x" * (OPPORTUNITY_NOTE_MAX_LENGTH + 1))
    with pytest.raises(TypeError, match="text or null"):
        normalize_note(1)  # type: ignore[arg-type]


def test_metadata_updates_target_only_allowlisted_stable_columns() -> None:
    favorite_sql = _postgres_sql(build_favorite_update(OPPORTUNITY_ID, favorite=True))
    note_sql = _postgres_sql(build_note_update(OPPORTUNITY_ID, note="  operator note  "))

    assert favorite_sql.startswith("UPDATE opportunities SET favorite=true, revision=")
    assert note_sql.startswith("UPDATE opportunities SET note='operator note', revision=")
    for sql, expected_column in ((favorite_sql, "favorite"), (note_sql, "note")):
        assigned = sql.split(" SET ", maxsplit=1)[1].split(" WHERE ", maxsplit=1)[0]
        assert expected_column in assigned
        assert "revision=(opportunities.revision + 1)" in assigned
        assert "updated_at" in assigned
        assert "opportunity_versions" not in sql
        assert "report" not in assigned
        assert "score" not in assigned
        assert "verdict" not in assigned
        assert "identity_fingerprint" not in assigned
        assert "embedding" not in assigned

    with pytest.raises(TypeError, match="must be a boolean"):
        build_favorite_update(OPPORTUNITY_ID, favorite=1)  # type: ignore[arg-type]
