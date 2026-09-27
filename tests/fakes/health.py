from pensae.domain.health import DependencyHealth, DependencyName, HealthState
from pensae.infrastructure.health.service import HealthService, StaticHealthProbe


def fake_health_service(
    overrides: dict[DependencyName, HealthState] | None = None,
) -> HealthService:
    states = {name: HealthState.READY for name in DependencyName}
    states.update(overrides or {})
    return HealthService(
        {
            name: StaticHealthProbe(
                DependencyHealth(
                    dependency=name,
                    state=state,
                    summary=f"{name.value} is {state.value}",
                    action=None if state is HealthState.READY else f"Repair local {name.value}.",
                )
            )
            for name, state in states.items()
        }
    )
