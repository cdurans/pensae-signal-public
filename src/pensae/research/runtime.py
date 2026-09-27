"""Construct the direct Fedora research runtime from protected configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID

import redis.asyncio as redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.config.settings import BootstrapSettings
from pensae.infrastructure.db import create_engine, create_session_factory
from pensae.infrastructure.models import (
    ChatParameters,
    LlamaCppChatClient,
    LlamaCppEmbeddingClient,
)
from pensae.infrastructure.progress import RedisProgressStore
from pensae.infrastructure.progress.redis_store import RedisStreamClient
from pensae.infrastructure.retrieval import SafeSourceClient, SourceLimits
from pensae.infrastructure.search import SearxngSearchClient
from pensae.opportunities import (
    OpportunityAggregateStore,
    OpportunityLifecycleService,
    PortfolioService,
    RecoverySummary,
)
from pensae.research.executor import ResearchWorkflowExecutor
from pensae.research.operations import ResearchWorkflowOperations
from pensae.research.roles import StructuredRoleExecutor
from pensae.runs.control import CancellationController, RunManager, RunProgressObserver
from pensae.runs.service import ResearchService
from pensae.settings import SavedSettings, SavedSettingsStore


async def build_research_runtime(
    settings: BootstrapSettings, protected: ProtectedConfig
) -> tuple[
    ResearchService,
    AsyncEngine,
    PortfolioService,
    SavedSettingsStore,
    OpportunityLifecycleService,
]:
    engine = create_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)
    store = OpportunityAggregateStore(session_factory)
    portfolio = PortfolioService(session_factory)
    saved_settings_store = SavedSettingsStore(session_factory)
    lifecycle = OpportunityLifecycleService(session_factory)

    async def recover_abandoned_runs() -> RecoverySummary:
        async with asyncio.timeout(5):
            summary = await store.resolve_abandoned_runs()
            await lifecycle.sweep_orphan_support()
            return summary

    recovered = False
    try:
        await recover_abandoned_runs()
        recovered = True
    except (SQLAlchemyError, OSError, TimeoutError):
        # The diagnostic UI must still open while PostgreSQL is degraded. Preflight
        # blocks research, and start retries recovery once the dependency is healthy.
        recovered = False
    policy = protected.research
    bounds = policy.bounds
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    progress = RedisProgressStore(cast(RedisStreamClient, redis_client), bounds)
    cancellation = CancellationController(progress)

    def operations(
        run_id: UUID,
        industry: str,
        saved: SavedSettings,
        effective_bounds: ProtectedWorkflowBounds,
    ) -> ResearchWorkflowOperations:
        search = SearxngSearchClient(
            base_url=saved.endpoints.searxng_url,
            timeout_seconds=effective_bounds.search_timeout_seconds,
        )
        retrieval = SafeSourceClient(
            limits=SourceLimits(max_response_bytes=effective_bounds.source_bytes)
        )
        roles = StructuredRoleExecutor(
            chat=LlamaCppChatClient(
                base_url=saved.endpoints.chat_url,
                model_id=protected.policy.chat_model_id,
                parameters=ChatParameters(
                    temperature=policy.temperature,
                    top_p=policy.top_p,
                    top_k=policy.top_k,
                    min_p=policy.min_p,
                    presence_penalty=policy.presence_penalty,
                    repetition_penalty=policy.repetition_penalty,
                ),
            ),
            repair_max=policy.repair_max,
            prompt_input_max_tokens=policy.prompt_input_max_tokens,
            context_window_tokens=32_768,
            context_safety_tokens=policy.context_safety_tokens,
        )
        embeddings = LlamaCppEmbeddingClient(
            base_url=saved.endpoints.embedding_url,
            model_id=protected.policy.embedding_model_id,
            dimension=protected.policy.embedding_dimension,
        )
        return ResearchWorkflowOperations(
            run_id=run_id,
            industry=industry,
            protected=protected,
            search=search,
            retrieval=retrieval,
            roles=roles,
            embeddings=embeddings,
            store=store,
            cancellation=cancellation,
            saved_settings=saved,
            effective_bounds=effective_bounds,
            role_output_tokens={
                "research_planner": saved.workflow.planner_output_max_tokens,
                "problem_analyst": saved.workflow.problem_analyst_output_max_tokens,
                "product_strategist": saved.workflow.product_strategist_output_max_tokens,
                "opportunity_analyst": saved.workflow.opportunity_analyst_output_max_tokens,
            },
        )

    def observer(run_id: UUID) -> RunProgressObserver:
        return RunProgressObserver(
            run_id=run_id,
            state_store=store,
            progress=progress,
            cancellation=cancellation,
        )

    executor = ResearchWorkflowExecutor(
        store=store,
        cancellation=cancellation,
        observer_factory=observer,
        operation_factory=operations,
    )
    manager = RunManager(
        executor=executor,
        state_store=store,
        progress=progress,
        cancellation=cancellation,
    )
    close_redis: Callable[[], Awaitable[None]] = redis_client.aclose
    return (
        ResearchService(
            manager=manager,
            progress=progress,
            store=store,
            protected=protected,
            close_callback=close_redis,
            recovery_callback=recover_abandoned_runs,
            recovery_complete=recovered,
            settings_store=saved_settings_store,
        ),
        engine,
        portfolio,
        saved_settings_store,
        lifecycle,
    )
