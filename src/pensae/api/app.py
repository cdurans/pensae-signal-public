from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from pensae.api.middleware import LocalSecurityMiddleware, RequestCorrelationMiddleware
from pensae.api.opportunities import install_opportunity_routes
from pensae.config.settings import BootstrapSettings, Environment
from pensae.diagnostics import LauncherOwnershipStatus, OperationalLogger
from pensae.domain.health import (
    CapabilityPreflight,
    DependencyHealth,
    DependencyName,
    HealthState,
    PreflightBlocker,
)
from pensae.infrastructure.health.service import HealthService, unavailable_health_service
from pensae.opportunities import OpportunityLifecycleService, PortfolioService, RunDetail
from pensae.runs.control import ActiveRunError
from pensae.runs.service import ResearchService
from pensae.settings import (
    FutureRunSettingsSnapshot,
    ResetSettingsRequest,
    SavedSettings,
    SavedSettingsStore,
    SettingsFieldMetadata,
)


class BootstrapResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_nonce: str
    canonical_origin: str


class StartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    state: str
    opportunity_id: UUID | None
    warnings: tuple[str, ...]


class StartBlockedDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str
    blockers: tuple[PreflightBlocker, ...]


class StartRunConflictResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: str | StartBlockedDetail
    run_id: UUID | None = None


class ApiErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: str


class ActiveRunUnavailableResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: str
    run_id: UUID | None = None


class StopRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StopRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    accepted: bool


class SavedSettingsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int
    values: SavedSettings
    fields: tuple[SettingsFieldMetadata, ...]


class OperationalStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ready: bool
    checks: tuple[DependencyHealth, ...]
    blockers: tuple[PreflightBlocker, ...]
    launcher: LauncherOwnershipStatus


async def get_health_service(request: Request) -> HealthService:
    factory: Callable[[BootstrapSettings], HealthService] | None = getattr(
        request.app.state, "health_service_factory", None
    )
    settings_store: SavedSettingsStore | None = getattr(request.app.state, "settings_store", None)
    if factory is not None and settings_store is not None:
        try:
            current = await settings_store.get()
        except (OSError, RuntimeError, SQLAlchemyError):
            service: HealthService = request.app.state.health_service
            return service
        return _health_service_for_snapshot(
            request,
            FutureRunSettingsSnapshot(
                settings_revision=current.revision,
                values=current.values,
            ),
        )
    service: HealthService = request.app.state.health_service
    return service


def _health_service_for_snapshot(
    request: Request, snapshot: FutureRunSettingsSnapshot
) -> HealthService:
    factory: Callable[[BootstrapSettings], HealthService] | None = getattr(
        request.app.state, "health_service_factory", None
    )
    if factory is None:
        service: HealthService = request.app.state.health_service
        return service
    endpoints = snapshot.values.endpoints
    runtime_settings = request.app.state.bootstrap_settings.model_copy(
        update={
            "searxng_url": endpoints.searxng_url,
            "chat_url": endpoints.chat_url,
            "embedding_url": endpoints.embedding_url,
        }
    )
    return factory(runtime_settings)


HealthServiceDependency = Annotated[HealthService, Depends(get_health_service)]


def _apply_infrastructure_gate(
    capability: CapabilityPreflight, *, infrastructure_ready: bool
) -> CapabilityPreflight:
    if infrastructure_ready:
        return capability
    checks = tuple(
        DependencyHealth(
            dependency=DependencyName.POSTGRESQL,
            state=HealthState.INCOMPATIBLE,
            summary="PostgreSQL backup or migration readiness has not passed",
            action="Resolve the launcher-reported database operation, then restart Pensae Signal.",
        )
        if check.dependency is DependencyName.POSTGRESQL
        else check
        for check in capability.checks
    )
    return CapabilityPreflight.from_checks(checks)


def create_app(
    *,
    settings: BootstrapSettings | None = None,
    health_service: HealthService | None = None,
    research_service: ResearchService | None = None,
    portfolio_service: PortfolioService | None = None,
    settings_store: SavedSettingsStore | None = None,
    lifecycle_service: OpportunityLifecycleService | None = None,
    ownership_status_provider: Callable[[], LauncherOwnershipStatus] | None = None,
    operational_logger: OperationalLogger | None = None,
    infrastructure_ready: bool = True,
    health_service_factory: Callable[[BootstrapSettings], HealthService] | None = None,
    frontend_dist: Path | None = None,
) -> FastAPI:
    resolved_settings = settings or BootstrapSettings()
    resolved_health = health_service or unavailable_health_service()
    nonce = secrets.token_urlsafe(32)
    docs_enabled = resolved_settings.environment is not Environment.RELEASE
    app = FastAPI(
        title="Pensae Signal",
        version="0.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.bootstrap_settings = resolved_settings
    app.state.health_service = resolved_health
    app.state.research_service = research_service
    app.state.portfolio_service = portfolio_service
    app.state.settings_store = settings_store
    app.state.lifecycle_service = lifecycle_service
    app.state.ownership_status_provider = ownership_status_provider
    app.state.health_service_factory = health_service_factory
    app.state.session_nonce = nonce
    app.add_middleware(LocalSecurityMiddleware, settings=resolved_settings, nonce=nonce)
    app.add_middleware(RequestCorrelationMiddleware, logger=operational_logger)

    @app.get("/api/bootstrap", response_model=BootstrapResponse)
    async def bootstrap() -> BootstrapResponse:
        return BootstrapResponse(
            session_nonce=nonce,
            canonical_origin=resolved_settings.canonical_origin,
        )

    @app.get("/api/runs/preflight", response_model=CapabilityPreflight)
    async def preflight(service: HealthServiceDependency) -> CapabilityPreflight:
        return _apply_infrastructure_gate(
            await service.preflight(), infrastructure_ready=infrastructure_ready
        )

    @app.get("/api/status", response_model=OperationalStatus)
    async def operational_status(service: HealthServiceDependency) -> OperationalStatus:
        capability = _apply_infrastructure_gate(
            await service.preflight(), infrastructure_ready=infrastructure_ready
        )
        launcher = (
            ownership_status_provider()
            if ownership_status_provider is not None
            else LauncherOwnershipStatus()
        )
        return OperationalStatus(
            ready=capability.ready,
            checks=capability.checks,
            blockers=capability.blockers,
            launcher=launcher,
        )

    @app.post(
        "/api/runs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=StartRunResponse,
        responses={
            409: {
                "model": StartRunConflictResponse,
                "description": "Capability preflight blocked start or another run is active",
            },
            501: {
                "model": ApiErrorResponse,
                "description": "Research service unavailable",
            },
        },
    )
    async def start_run(
        request: StartRunRequest, raw_request: Request
    ) -> JSONResponse | StartRunResponse:
        del request
        settings_snapshot: FutureRunSettingsSnapshot | None = None
        if research_service is not None:
            settings_snapshot = await research_service.snapshot_future_settings()
            service = _health_service_for_snapshot(raw_request, settings_snapshot)
        else:
            service = await get_health_service(raw_request)
        result = _apply_infrastructure_gate(
            await service.preflight(), infrastructure_ready=infrastructure_ready
        )
        if not result.ready:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": {
                        "message": "research start is blocked by capability preflight",
                        "blockers": [
                            blocker.model_dump(mode="json") for blocker in result.blockers
                        ],
                    }
                },
            )
        if research_service is None:
            return JSONResponse(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                content={"detail": "research service is unavailable"},
            )
        try:
            if settings_snapshot is None:
                raise RuntimeError("research settings snapshot is unavailable")
            outcome = await research_service.start(settings_snapshot=settings_snapshot)
        except ActiveRunError as exc:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "a research run is already active", "run_id": str(exc.run_id)},
            )
        return StartRunResponse(
            run_id=outcome.run_id,
            state=outcome.state,
            opportunity_id=outcome.opportunity_id,
            warnings=outcome.warnings,
        )

    @app.get(
        "/api/runs/active",
        response_model=RunDetail,
        responses={
            204: {"description": "No active research run"},
            503: {
                "model": ActiveRunUnavailableResponse,
                "description": "Research service or active snapshot unavailable",
            },
        },
    )
    async def active_run_snapshot() -> RunDetail | Response | JSONResponse:
        if research_service is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "research service unavailable"},
            )
        run_id = research_service.active_run_id
        if run_id is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        result = await research_service.get_run(run_id)
        if result is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "active run snapshot unavailable", "run_id": str(run_id)},
            )
        return result

    @app.post("/api/runs/{run_id}/stop", response_model=StopRunResponse)
    async def stop_run(run_id: UUID, request: StopRunRequest) -> StopRunResponse | JSONResponse:
        del request
        if research_service is None:
            return JSONResponse(status_code=404, content={"detail": "run not active"})
        accepted = await research_service.stop(run_id)
        return StopRunResponse(run_id=run_id, accepted=accepted)

    @app.get("/api/runs/{run_id}/events", response_model=None)
    async def run_events(
        run_id: UUID,
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID", max_length=48, pattern=r"^[0-9]+-[0-9]+$"),
        ] = None,
    ) -> StreamingResponse | JSONResponse:
        if research_service is None:
            return JSONResponse(status_code=404, content={"detail": "run not found"})
        service = research_service
        if await service.get_run(run_id) is None:
            return JSONResponse(status_code=404, content={"detail": "run not found"})

        async def events() -> AsyncIterator[str]:
            async for record in service.stream_events(run_id, last_event_id):
                if record is None:
                    yield "event: snapshot_required\ndata: {}\n\n"
                else:
                    yield (
                        f"id: {record.event_id}\n"
                        f"event: {record.event.kind}\n"
                        f"data: {record.event.model_dump_json()}\n\n"
                    )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs/{run_id}", response_model=RunDetail)
    async def run_snapshot(run_id: UUID) -> RunDetail | JSONResponse:
        if research_service is None:
            return JSONResponse(status_code=503, content={"detail": "research service unavailable"})
        result = await research_service.get_run(run_id)
        if result is None:
            return JSONResponse(status_code=404, content={"detail": "run not found"})
        return result

    install_opportunity_routes(
        app,
        research_service=research_service,
        portfolio_service=portfolio_service,
        lifecycle_service=lifecycle_service,
    )

    @app.get("/api/settings", response_model=SavedSettingsResponse)
    async def get_saved_settings() -> SavedSettingsResponse | JSONResponse:
        if settings_store is None:
            return JSONResponse(status_code=503, content={"detail": "settings unavailable"})
        current = await settings_store.get()
        return SavedSettingsResponse(
            revision=current.revision,
            values=current.values,
            fields=settings_store.field_metadata,
        )

    @app.put("/api/settings", response_model=SavedSettingsResponse)
    async def save_settings(request: SavedSettings) -> SavedSettingsResponse | JSONResponse:
        if settings_store is None:
            return JSONResponse(status_code=503, content={"detail": "settings unavailable"})
        current = await settings_store.save(request)
        return SavedSettingsResponse(
            revision=current.revision,
            values=current.values,
            fields=settings_store.field_metadata,
        )

    @app.post("/api/settings/reset", response_model=SavedSettingsResponse)
    async def reset_settings(request: ResetSettingsRequest) -> SavedSettingsResponse | JSONResponse:
        if settings_store is None:
            return JSONResponse(status_code=503, content={"detail": "settings unavailable"})
        current = await settings_store.reset(request)
        return SavedSettingsResponse(
            revision=current.revision,
            values=current.values,
            fields=settings_store.field_metadata,
        )

    dist = frontend_dist or Path("frontend/dist")
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False, response_model=None)
    async def index() -> FileResponse | HTMLResponse:
        index_file = dist / "index.html"
        if index_file.is_file():
            return HTMLResponse(index_file.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<main><h1>Pensae Signal</h1><p>The local UI build is unavailable. "
            "Run make bootstrap.</p></main>",
            status_code=503,
        )

    @app.get("/{spa_path:path}", include_in_schema=False, response_model=None)
    async def spa_fallback(spa_path: str) -> FileResponse | HTMLResponse | JSONResponse:
        if spa_path in {"api", "docs", "redoc", "openapi.json"} or spa_path.startswith(
            ("api/", "assets/", "docs/")
        ):
            return JSONResponse(status_code=404, content={"detail": "not found"})
        index_file = dist / "index.html"
        if index_file.is_file():
            return HTMLResponse(index_file.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<main><h1>Pensae Signal</h1><p>The local UI build is unavailable. "
            "Run make bootstrap.</p></main>",
            status_code=503,
        )

    return app


app = create_app()
