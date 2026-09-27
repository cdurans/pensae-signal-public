from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DependencyName(StrEnum):
    POSTGRESQL = "postgresql"
    REDIS = "redis"
    SEARXNG = "searxng"
    CHAT = "chat"
    EMBEDDING = "embedding"


DEPENDENCY_ORDER = tuple(DependencyName)


class HealthState(StrEnum):
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"
    STARTING = "starting"
    READY = "ready"
    UNKNOWN_LISTENER = "unknown_listener"


class DependencyHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    dependency: DependencyName
    state: HealthState
    summary: str = Field(min_length=1, max_length=240)
    action: str | None = Field(default=None, max_length=500)

    @property
    def blocks_run(self) -> bool:
        return self.state is not HealthState.READY


class PreflightBlocker(BaseModel):
    model_config = ConfigDict(frozen=True)

    dependency: DependencyName
    state: HealthState
    reason: str
    action: str | None = None


class CapabilityPreflight(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    checks: tuple[DependencyHealth, ...]
    blockers: tuple[PreflightBlocker, ...]

    @classmethod
    def from_checks(cls, checks: Iterable[DependencyHealth]) -> CapabilityPreflight:
        by_name = {item.dependency: item for item in checks}
        ordered = tuple(by_name[name] for name in DEPENDENCY_ORDER)
        blockers = tuple(
            PreflightBlocker(
                dependency=item.dependency,
                state=item.state,
                reason=item.summary,
                action=item.action,
            )
            for item in ordered
            if item.blocks_run
        )
        return cls(ready=not blockers, checks=ordered, blockers=blockers)
