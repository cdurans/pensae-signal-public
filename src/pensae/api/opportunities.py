"""Opportunity portfolio, version, metadata, and lifecycle API routes."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from pensae.opportunities import (
    DeletionResult,
    InvalidLifecycleTransition,
    LifecycleConflict,
    LifecycleMutationResult,
    LifecycleTargetPage,
    OpportunityDetail,
    OpportunityLifecycleService,
    PortfolioItem,
    PortfolioQuery,
    PortfolioService,
    PortfolioSort,
    RediscoveryDecision,
    StableOpportunityMetadata,
)
from pensae.runs.service import ResearchService


class PortfolioListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[PortfolioItem, ...]
    industry: str | None
    sort: PortfolioSort
    limit: int


class FavoriteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    favorite: bool


class NoteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    note: str | None = Field(default=None, max_length=4_000)


class RediscoveryDecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_opportunity_id: UUID
    decision: RediscoveryDecision
    expected_candidate_revision: int = Field(ge=1)
    expected_target_revision: int = Field(ge=1)


class MergeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    survivor_id: UUID
    expected_revision: int = Field(ge=1)
    expected_survivor_revision: int = Field(ge=1)
    confirmation: Literal["MERGE"]


class ReverseMergeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_revision: int = Field(ge=1)
    confirmation: Literal["REVERSE MERGE"]


class PermanentDeleteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_revision: int = Field(ge=1)
    confirmation: str = Field(min_length=43, max_length=43)


def _lifecycle_conflict(exc: Exception, code: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": code})


def install_opportunity_routes(
    app: FastAPI,
    *,
    research_service: ResearchService | None,
    portfolio_service: PortfolioService | None,
    lifecycle_service: OpportunityLifecycleService | None,
) -> None:
    @app.get("/api/opportunities/{opportunity_id}", response_model=OpportunityDetail)
    async def opportunity_detail(opportunity_id: UUID) -> OpportunityDetail | JSONResponse:
        if research_service is None:
            return JSONResponse(status_code=503, content={"detail": "research service unavailable"})
        result = await research_service.get_opportunity(opportunity_id)
        if result is None:
            return JSONResponse(status_code=404, content={"detail": "opportunity not found"})
        return result

    @app.get(
        "/api/opportunities/{opportunity_id}/versions/{version_id}",
        response_model=OpportunityDetail,
    )
    async def opportunity_version_detail(
        opportunity_id: UUID, version_id: UUID
    ) -> OpportunityDetail | JSONResponse:
        if research_service is None:
            return JSONResponse(status_code=503, content={"detail": "research service unavailable"})
        result = await research_service.get_opportunity_version(opportunity_id, version_id)
        if result is None:
            return JSONResponse(
                status_code=404, content={"detail": "opportunity version not found"}
            )
        return result

    @app.get("/api/opportunities", response_model=PortfolioListResponse)
    async def list_opportunities(
        raw_request: Request,
        industry: Annotated[str | None, Query(max_length=240)] = None,
        sort: PortfolioSort = PortfolioSort.DEFAULT,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PortfolioListResponse | JSONResponse:
        if set(raw_request.query_params) - {"industry", "sort", "limit"}:
            return JSONResponse(
                status_code=400,
                content={"detail": "only industry, sort, and limit are supported"},
            )
        if portfolio_service is None:
            return JSONResponse(status_code=503, content={"detail": "portfolio unavailable"})
        query = PortfolioQuery(industry=industry, sort=sort, limit=limit)
        return PortfolioListResponse(
            items=await portfolio_service.list(query),
            industry=query.industry,
            sort=query.sort,
            limit=query.limit,
        )

    @app.get(
        "/api/opportunities/{opportunity_id}/lifecycle-targets",
        response_model=LifecycleTargetPage,
    )
    async def opportunity_lifecycle_targets(
        opportunity_id: UUID,
        raw_request: Request,
        search: Annotated[str | None, Query(max_length=240)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> LifecycleTargetPage | JSONResponse:
        if set(raw_request.query_params) - {"search", "limit"}:
            return JSONResponse(
                status_code=400,
                content={"detail": "only search and limit are supported"},
            )
        if portfolio_service is None:
            return JSONResponse(status_code=503, content={"detail": "portfolio unavailable"})
        return await portfolio_service.lifecycle_targets(opportunity_id, search=search, limit=limit)

    @app.put(
        "/api/opportunities/{opportunity_id}/favorite",
        response_model=StableOpportunityMetadata,
    )
    async def set_opportunity_favorite(
        opportunity_id: UUID, request: FavoriteRequest
    ) -> StableOpportunityMetadata | JSONResponse:
        if portfolio_service is None:
            return JSONResponse(status_code=503, content={"detail": "portfolio unavailable"})
        try:
            return await portfolio_service.set_favorite(opportunity_id, favorite=request.favorite)
        except LookupError:
            return JSONResponse(status_code=404, content={"detail": "opportunity not found"})

    @app.put(
        "/api/opportunities/{opportunity_id}/note",
        response_model=StableOpportunityMetadata,
    )
    async def set_opportunity_note(
        opportunity_id: UUID, request: NoteRequest
    ) -> StableOpportunityMetadata | JSONResponse:
        if portfolio_service is None:
            return JSONResponse(status_code=503, content={"detail": "portfolio unavailable"})
        try:
            return await portfolio_service.set_note(opportunity_id, note=request.note)
        except LookupError:
            return JSONResponse(status_code=404, content={"detail": "opportunity not found"})

    @app.post(
        "/api/opportunities/{opportunity_id}/rediscovery-decision",
        response_model=LifecycleMutationResult,
    )
    async def decide_opportunity_rediscovery(
        opportunity_id: UUID, request: RediscoveryDecisionRequest
    ) -> LifecycleMutationResult | JSONResponse:
        if lifecycle_service is None:
            return JSONResponse(
                status_code=503, content={"detail": "lifecycle service unavailable"}
            )
        try:
            return await lifecycle_service.decide_rediscovery(
                opportunity_id,
                target_id=request.target_opportunity_id,
                decision=request.decision,
                expected_candidate_revision=request.expected_candidate_revision,
                expected_target_revision=request.expected_target_revision,
            )
        except LookupError:
            return JSONResponse(
                status_code=404, content={"detail": "candidate or target not found"}
            )
        except LifecycleConflict as exc:
            return _lifecycle_conflict(exc, "stale_revision")
        except InvalidLifecycleTransition as exc:
            return _lifecycle_conflict(exc, "invalid_transition")

    @app.post(
        "/api/opportunities/{opportunity_id}/merge",
        response_model=LifecycleMutationResult,
    )
    async def merge_opportunity(
        opportunity_id: UUID, request: MergeRequest
    ) -> LifecycleMutationResult | JSONResponse:
        if lifecycle_service is None:
            return JSONResponse(
                status_code=503, content={"detail": "lifecycle service unavailable"}
            )
        try:
            return await lifecycle_service.merge(
                opportunity_id,
                survivor_id=request.survivor_id,
                expected_revision=request.expected_revision,
                expected_survivor_revision=request.expected_survivor_revision,
            )
        except LookupError:
            return JSONResponse(
                status_code=404, content={"detail": "merge source or survivor not found"}
            )
        except LifecycleConflict as exc:
            return _lifecycle_conflict(exc, "stale_revision")
        except InvalidLifecycleTransition as exc:
            return _lifecycle_conflict(exc, "invalid_transition")

    @app.post(
        "/api/opportunities/{opportunity_id}/merge/reverse",
        response_model=LifecycleMutationResult,
    )
    async def reverse_opportunity_merge(
        opportunity_id: UUID, request: ReverseMergeRequest
    ) -> LifecycleMutationResult | JSONResponse:
        if lifecycle_service is None:
            return JSONResponse(
                status_code=503, content={"detail": "lifecycle service unavailable"}
            )
        try:
            return await lifecycle_service.reverse_merge(
                opportunity_id, expected_revision=request.expected_revision
            )
        except LookupError:
            return JSONResponse(status_code=404, content={"detail": "merged opportunity not found"})
        except LifecycleConflict as exc:
            return _lifecycle_conflict(exc, "stale_revision")
        except InvalidLifecycleTransition as exc:
            return _lifecycle_conflict(exc, "invalid_transition")

    @app.delete("/api/opportunities/{opportunity_id}", response_model=DeletionResult)
    async def delete_opportunity_permanently(
        opportunity_id: UUID, request: PermanentDeleteRequest
    ) -> DeletionResult | JSONResponse:
        if request.confirmation != f"DELETE {opportunity_id}":
            return JSONResponse(
                status_code=422,
                content={"detail": "confirmation must exactly match the displayed deletion phrase"},
            )
        if lifecycle_service is None:
            return JSONResponse(
                status_code=503, content={"detail": "lifecycle service unavailable"}
            )
        try:
            return await lifecycle_service.delete_permanently(
                opportunity_id, expected_revision=request.expected_revision
            )
        except LookupError:
            return JSONResponse(status_code=404, content={"detail": "opportunity not found"})
        except LifecycleConflict as exc:
            return _lifecycle_conflict(exc, "stale_revision")
        except InvalidLifecycleTransition as exc:
            return _lifecycle_conflict(exc, "invalid_transition")
