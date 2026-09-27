"""Deterministic contract matrix for the Fedora model supervisor."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from pensae.infrastructure.model_runtime import (
    STARTUP_ORDER,
    EndpointObservation,
    EndpointState,
    HttpxEndpointProbe,
    LaunchSpec,
    ModelRole,
    ModelRuntimeSupervisor,
    OwnershipRecord,
    ProcessIdentity,
    ReadinessPolicy,
    RoleAction,
    StopDisposition,
    TerminationOutcome,
)
from pensae.infrastructure.model_runtime.process import ProcfsProcessController


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=lambda: list[float]())

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeEndpointProbe:
    def __init__(self, observations: Mapping[ModelRole, list[EndpointObservation]]) -> None:
        self._observations = {
            role: list(role_observations) for role, role_observations in observations.items()
        }
        self.calls: list[ModelRole] = []

    async def inspect(self, spec: LaunchSpec) -> EndpointObservation:
        self.calls.append(spec.role)
        observations = self._observations[spec.role]
        if len(observations) > 1:
            return observations.pop(0)
        return observations[0]


class FakeProcessController:
    def __init__(self) -> None:
        self.next_pid = 4000
        self.spawned: list[LaunchSpec] = []
        self.current: dict[int, ProcessIdentity] = {}
        self.terminate_calls: list[ProcessIdentity] = []
        self.change_identity_during_terminate: set[int] = set()
        self.termination_timeouts: set[int] = set()

    async def spawn(self, spec: LaunchSpec) -> ProcessIdentity:
        identity = ProcessIdentity(
            pid=self.next_pid,
            start_identity=f"boot-a:{self.next_pid * 10}",
            executable=str(spec.executable),
        )
        self.next_pid += 1
        self.spawned.append(spec)
        self.current[identity.pid] = identity
        return identity

    def inspect(self, pid: int) -> ProcessIdentity | None:
        return self.current.get(pid)

    async def terminate(
        self, expected: ProcessIdentity, timeout_seconds: float
    ) -> TerminationOutcome:
        assert timeout_seconds > 0
        current = self.current.get(expected.pid)
        if expected.pid in self.change_identity_during_terminate:
            self.current[expected.pid] = ProcessIdentity(
                pid=expected.pid,
                start_identity="boot-a:recycled",
                executable=expected.executable,
            )
            return TerminationOutcome.REFUSED_IDENTITY
        if current != expected:
            return TerminationOutcome.REFUSED_IDENTITY
        self.terminate_calls.append(expected)
        if expected.pid in self.termination_timeouts:
            return TerminationOutcome.TIMED_OUT
        del self.current[expected.pid]
        return TerminationOutcome.EXITED


class MemoryOwnershipStore:
    def __init__(self, records: Mapping[ModelRole, OwnershipRecord] | None = None) -> None:
        self.records = dict(records or {})
        self.snapshots: list[dict[ModelRole, OwnershipRecord]] = []

    def load(self) -> dict[ModelRole, OwnershipRecord]:
        return dict(self.records)

    def replace(self, records: Mapping[ModelRole, OwnershipRecord]) -> None:
        self.records = dict(records)
        self.snapshots.append(dict(records))


def _observation(state: EndpointState, detail: str | None = None) -> EndpointObservation:
    return EndpointObservation(state, detail or state.value)


def _specs() -> dict[ModelRole, LaunchSpec]:
    return {
        ModelRole.CHAT: LaunchSpec(
            role=ModelRole.CHAT,
            executable=Path("/opt/pensae/llama-server"),
            model_path=Path("/models/chat.gguf"),
            port=8085,
            expected_model="Qwen3.6-35B-A3B-UD-IQ4_XS",
            fixed_arguments=(
                "--ctx-size",
                "32768",
                "--n-gpu-layers",
                "999",
                "--flash-attn",
                "on",
                "--parallel",
                "1",
                "--jinja",
            ),
        ),
        ModelRole.EMBEDDING: LaunchSpec(
            role=ModelRole.EMBEDDING,
            executable=Path("/opt/pensae/llama-server"),
            model_path=Path("/models/embedding.gguf"),
            port=8086,
            expected_model="Qwen3-Embedding-0.6B-GGUF-Q8_0",
            fixed_arguments=(
                "--embedding",
                "--ctx-size",
                "8192",
                "--pooling",
                "last",
            ),
        ),
    }


def _supervisor(
    observations: Mapping[ModelRole, list[EndpointObservation]],
    *,
    specs: Mapping[ModelRole, LaunchSpec] | None = None,
    processes: FakeProcessController | None = None,
    ownership: MemoryOwnershipStore | None = None,
    clock: FakeClock | None = None,
    timeout: float = 3.0,
    poll: float = 1.0,
) -> tuple[
    ModelRuntimeSupervisor,
    FakeProcessController,
    MemoryOwnershipStore,
    FakeClock,
    FakeEndpointProbe,
]:
    fake_processes = processes or FakeProcessController()
    fake_ownership = ownership or MemoryOwnershipStore()
    fake_clock = clock or FakeClock()
    probe = FakeEndpointProbe(observations)
    supervisor = ModelRuntimeSupervisor(
        specs=specs or _specs(),
        endpoint_probe=probe,
        processes=fake_processes,
        ownership=fake_ownership,
        clock=fake_clock,
        readiness=ReadinessPolicy(timeout_seconds=timeout, poll_seconds=poll),
        launcher_instance="launcher-test-1",
    )
    return supervisor, fake_processes, fake_ownership, fake_clock, probe


def test_reuses_both_compatible_endpoints_and_owns_neither() -> None:
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        }
    )

    result = asyncio.run(supervisor.reconcile())

    assert result.ready
    assert [role.action for role in result.roles] == [
        RoleAction.REUSED,
        RoleAction.REUSED,
    ]
    assert processes.spawned == []
    assert processes.terminate_calls == []
    assert ownership.records == {}


@pytest.mark.parametrize("missing_role", STARTUP_ORDER)
def test_starts_only_the_missing_role(missing_role: ModelRole) -> None:
    other_role = next(role for role in STARTUP_ORDER if role is not missing_role)
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            missing_role: [
                _observation(EndpointState.FREE),
                _observation(EndpointState.STARTING),
                _observation(EndpointState.COMPATIBLE),
            ],
            other_role: [_observation(EndpointState.COMPATIBLE)],
        }
    )

    result = asyncio.run(supervisor.reconcile())

    assert result.ready
    assert [spec.role for spec in processes.spawned] == [missing_role]
    assert set(ownership.records) == {missing_role}
    assert ownership.records[missing_role].launcher_instance == "launcher-test-1"
    missing_result = next(role for role in result.roles if role.role is missing_role)
    assert missing_result.action is RoleAction.STARTED


def test_starts_both_missing_roles_in_deterministic_order() -> None:
    reversed_specs = dict(reversed(tuple(_specs().items())))
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            role: [
                _observation(EndpointState.FREE),
                _observation(EndpointState.COMPATIBLE),
            ]
            for role in STARTUP_ORDER
        },
        specs=reversed_specs,
    )

    result = asyncio.run(supervisor.reconcile())

    assert result.ready
    assert [spec.role for spec in processes.spawned] == list(STARTUP_ORDER)
    assert list(ownership.records) == list(STARTUP_ORDER)
    for role, record in ownership.records.items():
        spec = _specs()[role]
        assert record.role is role
        assert record.pid > 0
        assert record.start_identity.startswith("boot-a:")
        assert record.executable == str(spec.executable)
        assert record.port == spec.port
        assert record.launcher_instance == "launcher-test-1"


def test_bounded_readiness_succeeds_before_deadline() -> None:
    clock = FakeClock()
    supervisor, _, _, _, _ = _supervisor(
        {
            ModelRole.CHAT: [
                _observation(EndpointState.FREE),
                _observation(EndpointState.STARTING),
                _observation(EndpointState.STARTING),
                _observation(EndpointState.COMPATIBLE),
            ],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        clock=clock,
        timeout=3.0,
        poll=1.0,
    )

    result = asyncio.run(supervisor.reconcile())

    assert result.ready
    assert clock.sleeps == [1.0, 1.0]
    assert clock.now == 2.0


@pytest.mark.parametrize(
    "blocked_state",
    [EndpointState.UNKNOWN_LISTENER, EndpointState.INCOMPATIBLE],
)
def test_occupied_unknown_or_incompatible_port_is_never_signaled(
    blocked_state: EndpointState,
) -> None:
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(blocked_state, "occupied by unsafe listener")],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        }
    )

    result = asyncio.run(supervisor.reconcile())

    assert not result.ready
    assert result.roles[0].action is RoleAction.BLOCKED
    assert "unsafe listener" in result.blocking_reasons[0]
    assert processes.spawned == []
    assert processes.terminate_calls == []
    assert ownership.records == {}


def test_readiness_timeout_cleans_only_child_created_in_this_attempt() -> None:
    external_identity = ProcessIdentity(1234, "boot-a:123", "/usr/bin/external")
    processes = FakeProcessController()
    processes.current[external_identity.pid] = external_identity
    supervisor, processes, ownership, clock, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [
                _observation(EndpointState.FREE),
                _observation(EndpointState.STARTING),
            ],
        },
        processes=processes,
        timeout=2.0,
        poll=1.0,
    )

    result = asyncio.run(supervisor.reconcile())

    assert not result.ready
    assert result.roles[1].action is RoleAction.FAILED
    assert "timed out" in result.roles[1].detail
    assert clock.now == 2.0
    assert len(processes.terminate_calls) == 1
    assert processes.terminate_calls[0].pid == 4000
    assert external_identity.pid in processes.current
    assert ownership.records == {}


def test_failure_after_first_role_starts_cleans_only_that_owned_partial_start() -> None:
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [
                _observation(EndpointState.FREE),
                _observation(EndpointState.COMPATIBLE),
            ],
            ModelRole.EMBEDDING: [
                _observation(EndpointState.UNKNOWN_LISTENER, "embedding port occupied")
            ],
        }
    )

    result = asyncio.run(supervisor.reconcile())

    assert not result.ready
    assert result.roles[0].action is RoleAction.OFFLINE
    assert [spec.role for spec in processes.spawned] == [ModelRole.CHAT]
    assert [identity.pid for identity in processes.terminate_calls] == [4000]
    assert ownership.records == {}


def test_free_port_does_not_overwrite_stale_live_ownership_record() -> None:
    spec = _specs()[ModelRole.CHAT]
    stale = OwnershipRecord(
        role=ModelRole.CHAT,
        pid=755,
        start_identity="boot-a:old",
        executable=str(spec.executable),
        port=spec.port,
        launcher_instance="launcher-old",
    )
    processes = FakeProcessController()
    processes.current[755] = ProcessIdentity(
        pid=755,
        start_identity="boot-a:recycled",
        executable=str(spec.executable),
    )
    ownership = MemoryOwnershipStore({ModelRole.CHAT: stale})
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.FREE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        processes=processes,
        ownership=ownership,
    )

    result = asyncio.run(supervisor.reconcile())

    assert not result.ready
    assert result.roles[0].action is RoleAction.BLOCKED
    assert "stale or recycled" in result.roles[0].detail
    assert processes.spawned == []
    assert processes.terminate_calls == []
    assert ownership.records == {ModelRole.CHAT: stale}


def test_stale_or_recycled_pid_record_is_refused_without_signal() -> None:
    spec = _specs()[ModelRole.CHAT]
    stale = OwnershipRecord(
        role=ModelRole.CHAT,
        pid=777,
        start_identity="boot-a:old",
        executable=str(spec.executable),
        port=spec.port,
        launcher_instance="launcher-old",
    )
    processes = FakeProcessController()
    processes.current[777] = ProcessIdentity(
        pid=777,
        start_identity="boot-a:new",
        executable=str(spec.executable),
    )
    ownership = MemoryOwnershipStore({ModelRole.CHAT: stale})
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        processes=processes,
        ownership=ownership,
    )

    results = asyncio.run(supervisor.stop())

    assert results[0].disposition is StopDisposition.REFUSED_IDENTITY_MISMATCH
    assert processes.terminate_calls == []
    assert ownership.records == {ModelRole.CHAT: stale}


def test_shutdown_signals_only_immediately_revalidated_owned_processes() -> None:
    specs = _specs()
    processes = FakeProcessController()
    chat_identity = ProcessIdentity(801, "boot-a:801", str(specs[ModelRole.CHAT].executable))
    recycled_embedding = ProcessIdentity(
        802,
        "boot-a:recycled",
        str(specs[ModelRole.EMBEDDING].executable),
    )
    processes.current = {801: chat_identity, 802: recycled_embedding}
    chat_record = OwnershipRecord.from_child(
        spec=specs[ModelRole.CHAT], identity=chat_identity, launcher_instance="old"
    )
    embedding_record = OwnershipRecord(
        role=ModelRole.EMBEDDING,
        pid=802,
        start_identity="boot-a:original",
        executable=str(specs[ModelRole.EMBEDDING].executable),
        port=8086,
        launcher_instance="old",
    )
    ownership = MemoryOwnershipStore(
        {
            ModelRole.CHAT: chat_record,
            ModelRole.EMBEDDING: embedding_record,
        }
    )
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        processes=processes,
        ownership=ownership,
    )

    results = asyncio.run(supervisor.stop())

    assert [result.disposition for result in results] == [
        StopDisposition.REFUSED_IDENTITY_MISMATCH,
        StopDisposition.STOPPED,
    ]
    assert processes.terminate_calls == [chat_identity]
    assert ownership.records == {ModelRole.EMBEDDING: embedding_record}


def test_identity_change_between_validation_and_signal_is_safely_refused() -> None:
    spec = _specs()[ModelRole.CHAT]
    identity = ProcessIdentity(901, "boot-a:901", str(spec.executable))
    processes = FakeProcessController()
    processes.current[identity.pid] = identity
    processes.change_identity_during_terminate.add(identity.pid)
    record = OwnershipRecord.from_child(spec=spec, identity=identity, launcher_instance="old")
    ownership = MemoryOwnershipStore({ModelRole.CHAT: record})
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        processes=processes,
        ownership=ownership,
    )

    results = asyncio.run(supervisor.stop())

    assert results[0].disposition is StopDisposition.SIGNAL_REFUSED
    assert processes.terminate_calls == []
    assert ownership.records == {ModelRole.CHAT: record}


def test_bounded_stop_timeout_retains_ownership_for_safe_retry() -> None:
    spec = _specs()[ModelRole.CHAT]
    identity = ProcessIdentity(902, "boot-a:902", str(spec.executable))
    processes = FakeProcessController()
    processes.current[identity.pid] = identity
    processes.termination_timeouts.add(identity.pid)
    record = OwnershipRecord.from_child(spec=spec, identity=identity, launcher_instance="old")
    ownership = MemoryOwnershipStore({ModelRole.CHAT: record})
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        processes=processes,
        ownership=ownership,
    )

    results = asyncio.run(supervisor.stop())

    assert results[0].disposition is StopDisposition.STOP_TIMEOUT
    assert processes.terminate_calls == [identity]
    assert ownership.records == {ModelRole.CHAT: record}


def test_failed_outer_start_cleanup_stops_only_current_launcher_attempt() -> None:
    specs = _specs()
    current_chat = ProcessIdentity(910, "boot-a:910", str(specs[ModelRole.CHAT].executable))
    prior_embedding = ProcessIdentity(911, "boot-a:911", str(specs[ModelRole.EMBEDDING].executable))
    processes = FakeProcessController()
    processes.current = {910: current_chat, 911: prior_embedding}
    chat_record = OwnershipRecord.from_child(
        spec=specs[ModelRole.CHAT],
        identity=current_chat,
        launcher_instance="launcher-test-1",
    )
    embedding_record = OwnershipRecord.from_child(
        spec=specs[ModelRole.EMBEDDING],
        identity=prior_embedding,
        launcher_instance="prior-launcher",
    )
    ownership = MemoryOwnershipStore(
        {ModelRole.CHAT: chat_record, ModelRole.EMBEDDING: embedding_record}
    )
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            ModelRole.CHAT: [_observation(EndpointState.COMPATIBLE)],
            ModelRole.EMBEDDING: [_observation(EndpointState.COMPATIBLE)],
        },
        processes=processes,
        ownership=ownership,
    )

    results = asyncio.run(supervisor.cleanup_failed_start())

    assert [result.pid for result in results] == [current_chat.pid]
    assert processes.terminate_calls == [current_chat]
    assert ownership.records == {ModelRole.EMBEDDING: embedding_record}


def test_real_http_probe_waits_from_free_through_pinned_loading_to_compatible() -> None:
    specs = _specs()
    chat_health_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_health_calls
        role = ModelRole.CHAT if request.url.port == 8085 else ModelRole.EMBEDDING
        if request.url.path == "/health":
            if role is ModelRole.CHAT:
                chat_health_calls += 1
                if chat_health_calls == 1:
                    raise httpx.ConnectError("free", request=request)
                if chat_health_calls == 2:
                    return httpx.Response(
                        503,
                        json={
                            "error": {
                                "code": 503,
                                "message": "Loading model",
                                "type": "unavailable_error",
                            }
                        },
                    )
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": specs[role].expected_model}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
        if request.url.path == "/v1/embeddings":
            return httpx.Response(200, json={"data": [{"embedding": [1.0] + [0.0] * 1023}]})
        return httpx.Response(404)

    processes = FakeProcessController()
    ownership = MemoryOwnershipStore()
    clock = FakeClock()
    supervisor = ModelRuntimeSupervisor(
        specs=specs,
        endpoint_probe=HttpxEndpointProbe(
            embedding_dimension=1024,
            transport=httpx.MockTransport(handler),
        ),
        processes=processes,
        ownership=ownership,
        clock=clock,
        readiness=ReadinessPolicy(timeout_seconds=3, poll_seconds=1),
        launcher_instance="launcher-http-transition",
    )

    result = asyncio.run(supervisor.reconcile())

    assert result.ready
    assert [spec.role for spec in processes.spawned] == [ModelRole.CHAT]
    assert clock.sleeps == [1]
    assert processes.terminate_calls == []


def test_reconcile_status_and_stop_are_idempotent() -> None:
    supervisor, processes, ownership, _, _ = _supervisor(
        {
            role: [
                _observation(EndpointState.FREE),
                _observation(EndpointState.COMPATIBLE),
            ]
            for role in STARTUP_ORDER
        }
    )

    async def scenario() -> None:
        first = await supervisor.reconcile()
        second = await supervisor.reconcile()
        status_first = await supervisor.status()
        status_second = await supervisor.status()
        stop_first = await supervisor.stop()
        stop_second = await supervisor.stop()

        assert first.ready and second.ready and status_first.ready and status_second.ready
        assert [role.action for role in second.roles] == [
            RoleAction.ALREADY_OWNED,
            RoleAction.ALREADY_OWNED,
        ]
        assert [result.disposition for result in stop_first] == [
            StopDisposition.STOPPED,
            StopDisposition.STOPPED,
        ]
        assert stop_second == ()

    asyncio.run(scenario())

    assert [spec.role for spec in processes.spawned] == list(STARTUP_ORDER)
    assert len(processes.terminate_calls) == 2
    assert ownership.records == {}


def test_launch_uses_fixed_argument_vectors_and_exposes_no_shell_path() -> None:
    spec = _specs()[ModelRole.CHAT]

    assert spec.argv == (
        "/opt/pensae/llama-server",
        "--host",
        "127.0.0.1",
        "--port",
        "8085",
        "--model",
        "/models/chat.gguf",
        "--alias",
        "Qwen3.6-35B-A3B-UD-IQ4_XS",
        "--no-ui",
        "--cors-origins",
        "https://pensae.invalid",
        "--no-cors-credentials",
        "--ctx-size",
        "32768",
        "--n-gpu-layers",
        "999",
        "--flash-attn",
        "on",
        "--parallel",
        "1",
        "--jinja",
    )
    source = inspect.getsource(ProcfsProcessController.spawn)
    assert "create_subprocess_exec" in source
    assert "create_subprocess_shell" not in source
    assert "shell=" not in source


def test_supervisor_has_no_research_cancellation_or_arbitrary_signal_api() -> None:
    public_methods = {
        name
        for name, _member in inspect.getmembers(ModelRuntimeSupervisor, inspect.isfunction)
        if not name.startswith("_")
    }

    assert public_methods == {"cleanup_failed_start", "reconcile", "status", "stop"}
