from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol

from pensae.domain.health import (
    DEPENDENCY_ORDER,
    CapabilityPreflight,
    DependencyHealth,
    DependencyName,
    HealthState,
)


class HealthProbe(Protocol):
    async def check(self) -> DependencyHealth: ...


class StaticHealthProbe:
    def __init__(self, result: DependencyHealth) -> None:
        self._result = result

    async def check(self) -> DependencyHealth:
        return self._result


class HealthService:
    def __init__(
        self,
        probes: Mapping[DependencyName, HealthProbe],
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        missing = set(DEPENDENCY_ORDER).difference(probes)
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"missing required health probes: {names}")
        self._probes = dict(probes)
        self._timeout_seconds = timeout_seconds

    async def _safe_check(self, dependency: DependencyName) -> DependencyHealth:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._probes[dependency].check()
        except TimeoutError:
            return DependencyHealth(
                dependency=dependency,
                state=HealthState.UNAVAILABLE,
                summary="health check timed out",
                action="Check the local service and its configured timeout.",
            )
        except Exception:
            return DependencyHealth(
                dependency=dependency,
                state=HealthState.UNAVAILABLE,
                summary="health check failed safely",
                action="Inspect local service status; no research run was created.",
            )
        if result.dependency is not dependency:
            return DependencyHealth(
                dependency=dependency,
                state=HealthState.INCOMPATIBLE,
                summary="health adapter returned the wrong dependency identity",
                action="Correct the protected adapter configuration.",
            )
        return result

    async def preflight(self) -> CapabilityPreflight:
        checks = await asyncio.gather(*(self._safe_check(name) for name in DEPENDENCY_ORDER))
        return CapabilityPreflight.from_checks(checks)


def unavailable_health_service() -> HealthService:
    probes = {
        name: StaticHealthProbe(
            DependencyHealth(
                dependency=name,
                state=HealthState.UNAVAILABLE,
                summary=f"{name.value} has not passed its local capability check",
                action="Run `make status` and start or repair the local dependency.",
            )
        )
        for name in DEPENDENCY_ORDER
    }
    return HealthService(probes)
