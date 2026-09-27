"""Bounded portfolio queries and stable operator metadata mutations."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select, Update

from pensae.infrastructure.db import schema

PORTFOLIO_DEFAULT_LIMIT = 50
PORTFOLIO_MAX_LIMIT = 100
PORTFOLIO_INDUSTRY_MAX_LENGTH = 240
OPPORTUNITY_NOTE_MAX_LENGTH = 4_000
LIFECYCLE_TARGET_DEFAULT_LIMIT = 50
LIFECYCLE_TARGET_MAX_LIMIT = 100


class PortfolioSort(StrEnum):
    """The complete supported portfolio sort surface."""

    DEFAULT = "default"
    NEWEST = "newest"
    WEIGHTED_SCORE_DESC = "weighted_score_desc"
    EVIDENCE_SCORE_DESC = "evidence_score_desc"


class PortfolioQuery(BaseModel):
    """Validated portfolio query; industry is intentionally the only filter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    industry: str | None = Field(default=None, max_length=PORTFOLIO_INDUSTRY_MAX_LENGTH)
    sort: PortfolioSort = PortfolioSort.DEFAULT
    limit: int = Field(default=PORTFOLIO_DEFAULT_LIMIT, ge=1, le=PORTFOLIO_MAX_LIMIT)

    @field_validator("industry", mode="before")
    @classmethod
    def normalize_industry(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = " ".join(unicodedata.normalize("NFC", value).split())
        return normalized or None


class PortfolioItem(BaseModel):
    """Current immutable evaluation plus stable operator metadata for one row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    version_id: UUID
    opportunity_name: str
    concise_summary: str
    primary_industry: str
    favorite: bool
    note: str | None
    revision: int = Field(ge=1)
    lifecycle_status: str
    classification: str
    merge_target_id: UUID | None = None
    verdict: str
    weighted_score: Decimal
    evidence_score: int
    version_created_at: datetime


class LifecycleTarget(BaseModel):
    """Bounded survivor-search row with only lifecycle decision fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    opportunity_name: str
    primary_industry: str
    revision: int = Field(ge=1)
    lifecycle_status: str


class LifecycleTargetPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[LifecycleTarget, ...]
    has_more: bool
    search: str | None
    limit: int


class StableOpportunityMetadata(BaseModel):
    """The only operator-editable fields on the stable opportunity row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    revision: int = Field(ge=1)
    favorite: bool
    note: str | None = Field(default=None, max_length=OPPORTUNITY_NOTE_MAX_LENGTH)
    updated_at: datetime


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def normalize_note(note: str | None) -> str | None:
    """Normalize a free-text note before applying its durable character bound."""

    if note is None:
        return None
    if not isinstance(note, str):
        raise TypeError("opportunity note must be text or null")
    normalized = unicodedata.normalize("NFC", note).replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip()
    if not normalized:
        return None
    if len(normalized) > OPPORTUNITY_NOTE_MAX_LENGTH:
        raise ValueError(f"opportunity note cannot exceed {OPPORTUNITY_NOTE_MAX_LENGTH} characters")
    return normalized


def build_portfolio_statement(query: PortfolioQuery) -> Select[tuple[Any, ...]]:
    """Build the bounded current-version list query with deterministic ordering."""

    version = schema.opportunity_versions
    opportunity = schema.opportunities
    statement = (
        sa.select(
            opportunity.c.id,
            opportunity.c.current_version_id.label("version_id"),
            version.c.report["opportunity_name"].astext.label("opportunity_name"),
            version.c.report["concise_summary"].astext.label("concise_summary"),
            opportunity.c.primary_industry,
            opportunity.c.favorite,
            opportunity.c.note,
            opportunity.c.revision,
            opportunity.c.lifecycle_status,
            opportunity.c.classification,
            opportunity.c.merge_target_id,
            version.c.verdict,
            version.c.weighted_score,
            version.c.evidence_score,
            version.c.created_at.label("version_created_at"),
        )
        .join(version, version.c.id == opportunity.c.current_version_id)
        .limit(query.limit)
    )
    if query.industry is not None:
        statement = statement.where(
            sa.func.lower(opportunity.c.primary_industry) == query.industry.lower()
        )
    return statement.order_by(*_portfolio_order(query.sort))


def build_favorite_update(opportunity_id: UUID, *, favorite: bool) -> Update:
    """Build an allowlisted stable-row favorite update."""

    if type(favorite) is not bool:
        raise TypeError("favorite must be a boolean")
    return (
        sa.update(schema.opportunities)
        .where(schema.opportunities.c.id == opportunity_id)
        .values(
            favorite=favorite,
            revision=schema.opportunities.c.revision + 1,
            updated_at=sa.func.now(),
        )
        .returning(
            schema.opportunities.c.id,
            schema.opportunities.c.revision,
            schema.opportunities.c.favorite,
            schema.opportunities.c.note,
            schema.opportunities.c.updated_at,
        )
    )


def build_note_update(opportunity_id: UUID, *, note: str | None) -> Update:
    """Build an allowlisted stable-row note update after deterministic normalization."""

    return (
        sa.update(schema.opportunities)
        .where(schema.opportunities.c.id == opportunity_id)
        .values(
            note=normalize_note(note),
            revision=schema.opportunities.c.revision + 1,
            updated_at=sa.func.now(),
        )
        .returning(
            schema.opportunities.c.id,
            schema.opportunities.c.revision,
            schema.opportunities.c.favorite,
            schema.opportunities.c.note,
            schema.opportunities.c.updated_at,
        )
    )


class PortfolioService:
    """Query current opportunities and mutate only stable favorite/note metadata."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def list(self, query: PortfolioQuery) -> tuple[PortfolioItem, ...]:
        async with self._session_factory() as session:
            result = await session.execute(build_portfolio_statement(query))
            return tuple(PortfolioItem.model_validate(row) for row in result.mappings())

    async def lifecycle_targets(
        self,
        source_id: UUID,
        *,
        search: str | None,
        limit: int = LIFECYCLE_TARGET_DEFAULT_LIMIT,
    ) -> LifecycleTargetPage:
        if not 1 <= limit <= LIFECYCLE_TARGET_MAX_LIMIT:
            raise ValueError("lifecycle target limit is out of bounds")
        normalized = (
            " ".join(unicodedata.normalize("NFC", search).split()) if search is not None else ""
        )
        normalized = normalized or None
        if normalized is not None and len(normalized) > PORTFOLIO_INDUSTRY_MAX_LENGTH:
            raise ValueError("lifecycle target search is too long")
        opportunity = schema.opportunities
        version = schema.opportunity_versions
        name = version.c.report["opportunity_name"].astext
        statement = (
            sa.select(
                opportunity.c.id,
                name.label("opportunity_name"),
                opportunity.c.primary_industry,
                opportunity.c.revision,
                opportunity.c.lifecycle_status,
            )
            .join(version, version.c.id == opportunity.c.current_version_id)
            .where(
                opportunity.c.id != source_id,
                opportunity.c.lifecycle_status == "active",
            )
            .order_by(version.c.created_at.desc(), opportunity.c.id)
            .limit(limit + 1)
        )
        if normalized is not None:
            statement = statement.where(
                sa.or_(
                    sa.func.strpos(sa.func.lower(name), normalized.casefold()) > 0,
                    sa.cast(opportunity.c.id, sa.Text()) == normalized,
                )
            )
        async with self._session_factory() as session:
            rows = tuple((await session.execute(statement)).mappings())
        return LifecycleTargetPage(
            items=tuple(LifecycleTarget.model_validate(row) for row in rows[:limit]),
            has_more=len(rows) > limit,
            search=normalized,
            limit=limit,
        )

    async def set_favorite(
        self, opportunity_id: UUID, *, favorite: bool
    ) -> StableOpportunityMetadata:
        return await self._update_metadata(build_favorite_update(opportunity_id, favorite=favorite))

    async def set_note(
        self, opportunity_id: UUID, *, note: str | None
    ) -> StableOpportunityMetadata:
        return await self._update_metadata(build_note_update(opportunity_id, note=note))

    async def _update_metadata(self, statement: Update) -> StableOpportunityMetadata:
        async with self._session_factory() as session, session.begin():
            row = (await session.execute(statement)).mappings().one_or_none()
            if row is None:
                raise LookupError("opportunity does not exist")
            return StableOpportunityMetadata.model_validate(row)


def _portfolio_order(sort: PortfolioSort) -> tuple[Any, ...]:
    version = schema.opportunity_versions
    opportunity = schema.opportunities
    stable_tie_break = opportunity.c.id.asc()
    if sort is PortfolioSort.DEFAULT:
        verdict_rank = sa.case(
            (version.c.verdict == "promising", 0),
            (version.c.verdict == "needs_more_evidence", 1),
            (version.c.verdict == "do_not_pursue", 2),
            else_=3,
        )
        return (
            verdict_rank.asc(),
            version.c.weighted_score.desc(),
            version.c.evidence_score.desc(),
            version.c.created_at.desc(),
            stable_tie_break,
        )
    if sort is PortfolioSort.NEWEST:
        return (version.c.created_at.desc(), stable_tie_break)
    if sort is PortfolioSort.WEIGHTED_SCORE_DESC:
        return (version.c.weighted_score.desc(), version.c.created_at.desc(), stable_tie_break)
    if sort is PortfolioSort.EVIDENCE_SCORE_DESC:
        return (version.c.evidence_score.desc(), version.c.created_at.desc(), stable_tie_break)
    raise AssertionError(f"unsupported portfolio sort: {sort}")
