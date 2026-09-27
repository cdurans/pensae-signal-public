"""Dependency-injected Fedora model endpoint reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .ownership import OwnershipStore
from .process import ProcessController
from .types import (
    STARTUP_ORDER,
    EndpointObservation,
    EndpointState,
    LaunchSpec,
    ModelRole,
    OwnershipRecord,
    ProcessIdentity,
    RoleAction,
    RoleResult,
    StopDisposition,
    StopResult,
    SupervisorResult,
    TerminationOutcome,
)


class EndpointProbe(Protocol):
    """Perform health, identity, API, and role-capability inspection."""

    async def inspect(self, spec: LaunchSpec) -> EndpointObservation: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class AsyncioClock:
    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class ReadinessPolicy:
    timeout_seconds: float
    poll_seconds: float
    termination_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("readiness timeout must be positive")
        if self.poll_seconds <= 0:
            raise ValueError("readiness polling interval must be positive")
        if self.poll_seconds > self.timeout_seconds:
            raise ValueError("readiness polling interval cannot exceed timeout")
        if self.termination_timeout_seconds <= 0:
            raise ValueError("termination timeout must be positive")


class ModelRuntimeSupervisor:
    """Reconcile fixed chat and embedding roles without touching unknown PIDs."""

    def __init__(
        self,
        *,
        specs: Mapping[ModelRole, LaunchSpec],
        endpoint_probe: EndpointProbe,
        processes: ProcessController,
        ownership: OwnershipStore,
        clock: Clock,
        readiness: ReadinessPolicy,
        launcher_instance: str,
    ) -> None:
        if set(specs) != set(STARTUP_ORDER):
            raise ValueError("exactly one chat and one embedding spec are required")
        if any(spec.role is not role for role, spec in specs.items()):
            raise ValueError("launch spec mapping keys must match their roles")
        if not launcher_instance.strip():
            raise ValueError("launcher instance must not be empty")
        self._specs = dict(specs)
        self._endpoint_probe = endpoint_probe
        self._processes = processes
        self._ownership = ownership
        self._clock = clock
        self._readiness = readiness
        self._launcher_instance = launcher_instance
        self._operation_lock = asyncio.Lock()

    async def reconcile(self) -> SupervisorResult:
        """Reuse compatible endpoints or start only free missing roles."""

        async with self._operation_lock:
            records = self._ownership.load()
            attempt_children: list[tuple[ModelRole, ProcessIdentity]] = []
            results: list[RoleResult] = []

            for role in STARTUP_ORDER:
                spec = self._specs[role]
                observation = await self._endpoint_probe.inspect(spec)
                if observation.state is EndpointState.COMPATIBLE:
                    record = records.get(role)
                    current = self._processes.inspect(record.pid) if record is not None else None
                    if record is not None and current is not None and record.matches(current):
                        results.append(
                            RoleResult(
                                role=role,
                                action=RoleAction.ALREADY_OWNED,
                                detail=observation.detail,
                                pid=record.pid,
                            )
                        )
                    else:
                        results.append(
                            RoleResult(
                                role=role,
                                action=RoleAction.REUSED,
                                detail=observation.detail,
                            )
                        )
                    continue

                if observation.state in {
                    EndpointState.INCOMPATIBLE,
                    EndpointState.UNKNOWN_LISTENER,
                    EndpointState.STARTING,
                }:
                    results.append(
                        RoleResult(
                            role=role,
                            action=RoleAction.BLOCKED,
                            detail=observation.detail,
                        )
                    )
                    cleaned_roles = await self._cleanup_attempt(attempt_children, records)
                    self._mark_cleaned_results(results, cleaned_roles)
                    return self._complete_failure_results(results)

                previous_record = records.get(role)
                if previous_record is not None:
                    current = self._processes.inspect(previous_record.pid)
                    if current is not None:
                        record_detail = (
                            "recorded owned process is not serving a compatible endpoint"
                            if previous_record.matches(current)
                            else "stale or recycled PID record blocks safe replacement"
                        )
                        results.append(
                            RoleResult(
                                role=role,
                                action=RoleAction.BLOCKED,
                                detail=record_detail,
                                pid=previous_record.pid,
                            )
                        )
                        cleaned_roles = await self._cleanup_attempt(attempt_children, records)
                        self._mark_cleaned_results(results, cleaned_roles)
                        return self._complete_failure_results(results)
                    del records[role]
                    self._ownership.replace(records)

                try:
                    identity = await self._processes.spawn(spec)
                    attempt_children.append((role, identity))
                    record = OwnershipRecord.from_child(
                        spec=spec,
                        identity=identity,
                        launcher_instance=self._launcher_instance,
                    )
                    records[role] = record
                    self._ownership.replace(records)
                    ready_observation = await self._wait_until_ready(spec)
                except Exception as error:
                    results.append(
                        RoleResult(
                            role=role,
                            action=RoleAction.FAILED,
                            detail=f"{role.value} launch failed safely: {error}",
                        )
                    )
                    cleaned_roles = await self._cleanup_attempt(attempt_children, records)
                    self._mark_cleaned_results(results, cleaned_roles)
                    return self._complete_failure_results(results)

                if ready_observation.state is not EndpointState.COMPATIBLE:
                    results.append(
                        RoleResult(
                            role=role,
                            action=RoleAction.FAILED,
                            detail=ready_observation.detail,
                        )
                    )
                    cleaned_roles = await self._cleanup_attempt(attempt_children, records)
                    self._mark_cleaned_results(results, cleaned_roles)
                    return self._complete_failure_results(results)

                results.append(
                    RoleResult(
                        role=role,
                        action=RoleAction.STARTED,
                        detail=ready_observation.detail,
                        pid=identity.pid,
                    )
                )

            return SupervisorResult(roles=tuple(results))

    async def status(self) -> SupervisorResult:
        """Inspect endpoint and recorded ownership without mutation or signaling."""

        async with self._operation_lock:
            records = self._ownership.load()
            results: list[RoleResult] = []
            for role in STARTUP_ORDER:
                observation = await self._endpoint_probe.inspect(self._specs[role])
                if observation.state is EndpointState.COMPATIBLE:
                    record = records.get(role)
                    current = self._processes.inspect(record.pid) if record is not None else None
                    if record is not None and current is not None and record.matches(current):
                        results.append(
                            RoleResult(
                                role=role,
                                action=RoleAction.ALREADY_OWNED,
                                detail=observation.detail,
                                pid=record.pid,
                            )
                        )
                    else:
                        results.append(
                            RoleResult(
                                role=role,
                                action=RoleAction.REUSED,
                                detail=observation.detail,
                            )
                        )
                else:
                    action = (
                        RoleAction.OFFLINE
                        if observation.state is EndpointState.FREE
                        else RoleAction.BLOCKED
                    )
                    results.append(RoleResult(role=role, action=action, detail=observation.detail))
            return SupervisorResult(roles=tuple(results))

    async def stop(self) -> tuple[StopResult, ...]:
        """Stop only recorded children whose identities still match."""

        return await self._stop_records(launcher_instance=None)

    async def cleanup_failed_start(self) -> tuple[StopResult, ...]:
        """Stop only children created by this supervisor's launcher attempt."""

        return await self._stop_records(launcher_instance=self._launcher_instance)

    async def _stop_records(self, *, launcher_instance: str | None) -> tuple[StopResult, ...]:
        """Stop matching owned records, optionally limited to one launch attempt."""

        async with self._operation_lock:
            records = self._ownership.load()
            results: list[StopResult] = []
            changed = False
            for role in reversed(STARTUP_ORDER):
                record = records.get(role)
                if record is None:
                    continue
                if launcher_instance is not None and record.launcher_instance != launcher_instance:
                    continue
                current = self._processes.inspect(record.pid)
                if current is None:
                    results.append(
                        StopResult(
                            role=role,
                            disposition=StopDisposition.ALREADY_EXITED,
                            detail="recorded child is no longer running",
                            pid=record.pid,
                        )
                    )
                    del records[role]
                    changed = True
                    continue
                if not record.matches(current):
                    results.append(
                        StopResult(
                            role=role,
                            disposition=StopDisposition.REFUSED_IDENTITY_MISMATCH,
                            detail="refused stale or recycled PID ownership record",
                            pid=record.pid,
                        )
                    )
                    continue
                termination = await self._processes.terminate(
                    current, self._readiness.termination_timeout_seconds
                )
                if termination is TerminationOutcome.EXITED:
                    results.append(
                        StopResult(
                            role=role,
                            disposition=StopDisposition.STOPPED,
                            detail="signaled revalidated launcher-owned child",
                            pid=record.pid,
                        )
                    )
                    del records[role]
                    changed = True
                elif termination is TerminationOutcome.REFUSED_IDENTITY:
                    results.append(
                        StopResult(
                            role=role,
                            disposition=StopDisposition.SIGNAL_REFUSED,
                            detail="process identity changed before signal; no signal sent",
                            pid=record.pid,
                        )
                    )
                else:
                    results.append(
                        StopResult(
                            role=role,
                            disposition=StopDisposition.STOP_TIMEOUT,
                            detail="owned child did not exit before the bounded deadline",
                            pid=record.pid,
                        )
                    )
            if changed:
                self._ownership.replace(records)
            return tuple(results)

    async def _wait_until_ready(self, spec: LaunchSpec) -> EndpointObservation:
        deadline = self._clock.monotonic() + self._readiness.timeout_seconds
        while True:
            observation = await self._endpoint_probe.inspect(spec)
            if observation.state is EndpointState.COMPATIBLE:
                return observation
            if observation.state in {
                EndpointState.INCOMPATIBLE,
                EndpointState.UNKNOWN_LISTENER,
            }:
                return observation
            now = self._clock.monotonic()
            if now >= deadline:
                return EndpointObservation(
                    EndpointState.STARTING,
                    f"{spec.role.value} readiness timed out after "
                    f"{self._readiness.timeout_seconds:g}s",
                )
            await self._clock.sleep(min(self._readiness.poll_seconds, max(0.0, deadline - now)))

    async def _cleanup_attempt(
        self,
        attempt_children: list[tuple[ModelRole, ProcessIdentity]],
        records: dict[ModelRole, OwnershipRecord],
    ) -> set[ModelRole]:
        changed = False
        cleaned_roles: set[ModelRole] = set()
        for role, created_identity in reversed(attempt_children):
            current = self._processes.inspect(created_identity.pid)
            removable = current != created_identity
            if current is not None and current == created_identity:
                outcome = await self._processes.terminate(
                    current, self._readiness.termination_timeout_seconds
                )
                removable = outcome is TerminationOutcome.EXITED
            record = records.get(role)
            if removable and record is not None and record.matches(created_identity):
                del records[role]
                changed = True
            if removable:
                cleaned_roles.add(role)
        if changed:
            self._ownership.replace(records)
        return cleaned_roles

    @staticmethod
    def _mark_cleaned_results(results: list[RoleResult], cleaned_roles: set[ModelRole]) -> None:
        for index, result in enumerate(results):
            if result.role in cleaned_roles and result.action is RoleAction.STARTED:
                results[index] = RoleResult(
                    role=result.role,
                    action=RoleAction.OFFLINE,
                    detail="owned partial startup was cleaned after a later failure",
                )

    def _complete_failure_results(self, results: list[RoleResult]) -> SupervisorResult:
        completed_roles = {result.role for result in results}
        for role in STARTUP_ORDER:
            if role not in completed_roles:
                results.append(
                    RoleResult(
                        role=role,
                        action=RoleAction.OFFLINE,
                        detail="not reconciled after earlier model-runtime failure",
                    )
                )
        return SupervisorResult(roles=tuple(results))
