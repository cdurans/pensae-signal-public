import asyncio

from tests.fakes.health import fake_health_service

from pensae.domain.health import DependencyHealth, DependencyName, HealthState
from pensae.infrastructure.health.service import HealthService, StaticHealthProbe


def test_preflight_collects_every_blocker_in_stable_order() -> None:
    service = fake_health_service(
        {
            DependencyName.SEARXNG: HealthState.UNAVAILABLE,
            DependencyName.CHAT: HealthState.UNKNOWN_LISTENER,
        }
    )

    result = asyncio.run(service.preflight())

    assert not result.ready
    assert [item.dependency for item in result.blockers] == [
        DependencyName.SEARXNG,
        DependencyName.CHAT,
    ]


def test_preflight_ready_only_when_all_dependencies_are_ready() -> None:
    result = asyncio.run(fake_health_service().preflight())

    assert result.ready
    assert not result.blockers


def test_probe_identity_mismatch_fails_closed() -> None:
    probes = {
        name: StaticHealthProbe(
            DependencyHealth(
                dependency=name,
                state=HealthState.READY,
                summary="ready",
            )
        )
        for name in DependencyName
    }
    probes[DependencyName.CHAT] = StaticHealthProbe(
        DependencyHealth(
            dependency=DependencyName.EMBEDDING,
            state=HealthState.READY,
            summary="wrong identity",
        )
    )

    result = asyncio.run(HealthService(probes).preflight())

    assert result.blockers[0].dependency is DependencyName.CHAT
    assert result.blockers[0].state is HealthState.INCOMPATIBLE
