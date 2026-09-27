"""Short PostgreSQL transactions for the singleton saved-settings value."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from pensae.infrastructure.db import schema
from pensae.settings.models import (
    DurableSettingsValue,
    FutureRunSettingsSnapshot,
    LocalEndpointSettings,
    LoggingSettings,
    ResearchSettings,
    ResetSettingsRequest,
    SavedSettings,
    SettingsFieldMetadata,
)
from pensae.settings.service import SavedSettingsService

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_LOGGER = logging.getLogger(__name__)


class _LegacyWorkflowSettings(BaseModel):
    """Exact accepted legacy browser-saved workflow shape; never accepts unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery_queries: int = Field(default=8, ge=1, le=20)
    results_per_query: int = Field(default=10, ge=1, le=10)
    unique_urls: int = Field(default=60, ge=1, le=60)
    first_pass_pages: int = Field(default=24, ge=1, le=48)
    run_queries: int = Field(default=20, ge=1, le=20)
    run_pages: int = Field(default=48, ge=1, le=48)
    signals: int = Field(default=30, ge=1, le=30)
    patterns: int = Field(default=10, ge=1, le=10)
    segments_per_pattern: int = Field(default=3, ge=1, le=3)
    preliminary_survivors: int = Field(default=6, ge=1, le=6)
    concepts: int = Field(default=4, ge=1, le=4)
    focused_queries_per_concept: int = Field(default=3, ge=1, le=3)
    focused_pages_per_concept: int = Field(default=6, ge=1, le=6)
    opportunities: int = Field(default=4, ge=1, le=4)
    search_retries: int = Field(default=1, ge=0, le=1)
    model_calls: int = Field(default=32, ge=1, le=64)
    total_run_tokens: int = Field(default=300_000, ge=1, le=600_000)
    search_timeout_seconds: int = Field(default=30, ge=1, le=30)
    model_timeout_seconds: int = Field(default=120, ge=1, le=120)
    embedding_timeout_seconds: int = Field(default=60, ge=1, le=60)
    planner_output_max_tokens: int = Field(default=2_048, ge=1, le=2_048)
    problem_analyst_output_max_tokens: int = Field(default=4_096, ge=1, le=4_096)
    product_strategist_output_max_tokens: int = Field(default=4_096, ge=1, le=4_096)
    opportunity_analyst_output_max_tokens: int = Field(default=6_144, ge=1, le=6_144)

    @model_validator(mode="after")
    def validate_legacy_relationships(self) -> _LegacyWorkflowSettings:
        if self.discovery_queries > self.run_queries:
            raise ValueError("discovery queries cannot exceed total run queries")
        if self.first_pass_pages > self.run_pages:
            raise ValueError("first-pass pages cannot exceed total run pages")
        if self.opportunities > self.concepts:
            raise ValueError("final opportunities cannot exceed solution concepts")
        return self


class _LegacySavedSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    research: ResearchSettings
    endpoints: LocalEndpointSettings
    workflow: _LegacyWorkflowSettings
    logging: LoggingSettings


class SavedSettingsStore:
    """Persist revisions without mutating active or historical run snapshots."""

    def __init__(
        self,
        session_factory: SessionFactory,
        policy: SavedSettingsService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._policy = policy or SavedSettingsService()

    @property
    def field_metadata(self) -> tuple[SettingsFieldMetadata, ...]:
        return self._policy.baseline.field_metadata

    async def get(self) -> DurableSettingsValue:
        async with self._session_factory() as session:
            row = await self._read(session)
            if row is None:
                raise RuntimeError("saved settings are not initialized; run migrations")
            return row

    async def save(self, candidate: SavedSettings | Mapping[str, object]) -> DurableSettingsValue:
        async with self._session_factory() as session, session.begin():
            current = await self._read(session, for_update=True)
            if current is None:
                raise RuntimeError("saved settings are not initialized; run migrations")
            updated = self._policy.save(current, candidate)
            await self._write(session, updated)
            return updated

    async def reset(
        self, request: ResetSettingsRequest | Mapping[str, object]
    ) -> DurableSettingsValue:
        async with self._session_factory() as session, session.begin():
            current = await self._read(session, for_update=True)
            if current is None:
                raise RuntimeError("saved settings are not initialized; run migrations")
            updated = self._policy.reset(current, request)
            await self._write(session, updated)
            return updated

    async def snapshot_for_future_run(self) -> FutureRunSettingsSnapshot:
        return self._policy.snapshot_for_future_run(await self.get())

    async def _read(
        self, session: AsyncSession, *, for_update: bool = False
    ) -> DurableSettingsValue | None:
        statement = sa.select(
            schema.settings.c.revision,
            schema.settings.c["values"],
        ).where(schema.settings.c.id == 1)
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        return _validate_or_upgrade_legacy(row, self._policy)

    @staticmethod
    async def _write(session: AsyncSession, value: DurableSettingsValue) -> None:
        await session.execute(
            sa.update(schema.settings)
            .where(schema.settings.c.id == 1)
            .values(
                values=value.values.model_dump(mode="json"),
                revision=value.revision,
                updated_at=sa.func.now(),
            )
        )


def _validate_or_upgrade_legacy(
    row: Mapping[Any, object], policy: SavedSettingsService
) -> DurableSettingsValue:
    """Apply the current workflow baseline only to the exact accepted legacy saved value."""

    try:
        return DurableSettingsValue.model_validate(row)
    except ValidationError as current_error:
        calibrated = _upgrade_precalibration_tokens(row, policy)
        if calibrated is not None:
            return calibrated
        try:
            legacy = _LegacySavedSettings.model_validate(row["values"])
        except (KeyError, ValidationError):
            raise current_error from None
        revision = row.get("revision")
        if not isinstance(revision, int):
            raise current_error from None
        _LOGGER.warning("saved_settings_legacy_compatibility_applied")
        return DurableSettingsValue(
            revision=revision,
            values=SavedSettings(
                research=legacy.research.model_copy(deep=True),
                endpoints=legacy.endpoints.model_copy(deep=True),
                workflow=policy.baseline.values.workflow.model_copy(deep=True),
                logging=legacy.logging.model_copy(deep=True),
            ),
        )


def _upgrade_precalibration_tokens(
    row: Mapping[Any, object], policy: SavedSettingsService
) -> DurableSettingsValue | None:
    """Raise only the accepted 900k value and preserve every other valid setting."""

    revision = row.get("revision")
    values = row.get("values")
    if not isinstance(revision, int) or not isinstance(values, Mapping):
        return None
    if set(values) != set(SavedSettings.model_fields):
        return None
    workflow = values.get("workflow")
    if not isinstance(workflow, Mapping) or set(workflow) != set(
        type(policy.baseline.values.workflow).model_fields
    ):
        return None
    if workflow.get("total_run_tokens") != 900_000:
        return None
    calibrated_workflow = dict(workflow)
    calibrated_workflow["total_run_tokens"] = policy.baseline.values.workflow.total_run_tokens
    candidate = dict(values)
    candidate["workflow"] = calibrated_workflow
    try:
        calibrated = SavedSettings.model_validate(candidate)
    except ValidationError:
        return None
    _LOGGER.warning("saved_settings_token_calibration_applied")
    return DurableSettingsValue(revision=revision, values=calibrated)
