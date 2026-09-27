import asyncio
import os
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import scripts.linux.launcher as launcher
from scripts.linux.launcher import (
    AppOwnershipRecord,
    AppOwnershipStore,
    LauncherLock,
    prepare_infrastructure,
    safe_launcher_ownership_status,
)

from pensae.config.settings import BootstrapSettings
from pensae.infrastructure.model_runtime import SupervisorResult


def test_app_ownership_record_is_atomic_and_private(tmp_path: Path) -> None:
    path = (tmp_path / "ownership" / "app.json").resolve()
    store = AppOwnershipStore(path)
    record = AppOwnershipRecord(
        pid=123,
        start_identity="boot:ticks",
        executable="/usr/bin/python3.13",
        launcher_instance="instance",
    )

    store.replace(record)

    assert store.load() == record
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_launcher_lock_refuses_a_second_mutator(tmp_path: Path) -> None:
    path = (tmp_path / "launcher.lock").resolve()

    with LauncherLock(path):
        try:
            with LauncherLock(path):
                raise AssertionError("second lock unexpectedly succeeded")
        except RuntimeError as error:
            assert "another Pensae Signal launcher" in str(error)

    assert os.stat(path).st_mode & 0o777 == 0o600


def test_safe_launcher_ownership_status_reports_absent_and_malformed_records(
    tmp_path: Path,
) -> None:
    settings = BootstrapSettings.model_validate({"ownership_dir": tmp_path / "ownership"})
    absent = safe_launcher_ownership_status(settings)
    assert absent.model_dump(mode="json") == {
        "app": "none",
        "chat": "none",
        "embedding": "none",
        "failure_code": None,
    }

    app_record = settings.ownership_dir / "app-process.json"
    app_record.parent.mkdir(parents=True)
    app_record.write_text("not-json", encoding="utf-8")
    malformed = safe_launcher_ownership_status(settings)
    assert malformed.app.value == "invalid"
    assert malformed.failure_code == "app_record_invalid"


class RecordingRunner:
    def __init__(self, outcomes: tuple[int | BaseException, ...]) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[tuple[str, ...], bool, float]] = []

    async def __call__(
        self,
        argv: Any,
        *,
        required: bool = True,
        timeout_seconds: float = 60,
    ) -> int:
        self.calls.append((tuple(argv), required, timeout_seconds))
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class HangingProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.wait_calls = 0
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            await asyncio.Event().wait()
        return -15

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@pytest.mark.anyio
async def test_run_fixed_terminates_its_owned_child_on_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HangingProcess()

    async def create_process(*_: object, **__: object) -> HangingProcess:
        return process

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(TimeoutError, match="exceeded"):
        await launcher.run_fixed(("fixed-command",), required=False, timeout_seconds=0.001)

    assert process.terminated
    assert not process.killed
    assert process.wait_calls == 2


@pytest.mark.anyio
async def test_infrastructure_waits_boundedly_before_migration() -> None:
    runner = RecordingRunner((0, 0, 0))

    assert await prepare_infrastructure(
        readiness_seconds=45,
        command_timeout_seconds=90,
        migration_timeout_seconds=20,
        runner=runner,
    )
    assert runner.calls[0] == (
        (
            "docker",
            "compose",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "45",
        ),
        False,
        90,
    )
    assert runner.calls[1][0][1:4] == ("-m", "pensae.operations.database", "backup")
    assert runner.calls[1][0][-1] == "--if-nonempty"
    assert runner.calls[2][0][1:4] == ("-m", "alembic", "upgrade")
    assert runner.calls[2][2] == 20


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", [OSError("docker unavailable")])
async def test_infrastructure_timeout_or_launch_error_degrades_without_migration(
    outcome: int | BaseException,
) -> None:
    runner = RecordingRunner((outcome,))

    assert not await prepare_infrastructure(
        readiness_seconds=45,
        command_timeout_seconds=90,
        migration_timeout_seconds=20,
        runner=runner,
    )
    assert len(runner.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", [1, TimeoutError("bounded timeout")])
async def test_infrastructure_failed_or_timed_out_compose_start_cleans_partial_resources(
    outcome: int | BaseException,
) -> None:
    runner = RecordingRunner((outcome, 0))

    assert not await prepare_infrastructure(
        readiness_seconds=45,
        command_timeout_seconds=90,
        migration_timeout_seconds=20,
        runner=runner,
    )
    assert runner.calls[1] == (
        ("docker", "compose", "down", "--remove-orphans"),
        False,
        90,
    )


@pytest.mark.anyio
async def test_migration_failure_degrades_after_successful_health_wait() -> None:
    runner = RecordingRunner((0, 0, 2))

    assert not await prepare_infrastructure(
        readiness_seconds=45,
        command_timeout_seconds=90,
        migration_timeout_seconds=20,
        runner=runner,
    )
    assert len(runner.calls) == 3


@pytest.mark.anyio
async def test_pre_migration_backup_failure_prevents_migration() -> None:
    runner = RecordingRunner((0, 2))

    assert not await prepare_infrastructure(
        readiness_seconds=45,
        command_timeout_seconds=90,
        migration_timeout_seconds=20,
        runner=runner,
    )
    assert len(runner.calls) == 2


class FakeAppStore:
    def __init__(self, *, fail_replace: bool) -> None:
        self.record: AppOwnershipRecord | None = None
        self.fail_replace = fail_replace
        self.removed = False

    def load(self) -> AppOwnershipRecord | None:
        return self.record

    def replace(self, record: AppOwnershipRecord) -> None:
        if self.fail_replace:
            raise RuntimeError("injected ownership persistence failure")
        self.record = record

    def remove(self) -> None:
        self.record = None
        self.removed = True


class FakeSupervisor:
    def __init__(self) -> None:
        self.stop_attempt_calls = 0
        self.stop_calls = 0

    async def reconcile(self) -> SupervisorResult:
        return SupervisorResult(roles=())

    async def cleanup_failed_start(self) -> tuple[()]:
        self.stop_attempt_calls += 1
        return ()

    async def stop(self) -> tuple[()]:
        self.stop_calls += 1
        return ()


class FakeResearchEngine:
    async def dispose(self) -> None:
        return None


class FakeResearchService:
    async def aclose(self) -> None:
        return None


class FakeSavedSettingsStore:
    async def get(self) -> SimpleNamespace:
        return SimpleNamespace(
            values=SimpleNamespace(logging=SimpleNamespace(rotation_size_mib=10, retained_files=5))
        )


async def _fake_research_runtime(
    *_: object, **__: object
) -> tuple[object, FakeResearchEngine, object, object, object]:
    return (
        FakeResearchService(),
        FakeResearchEngine(),
        object(),
        FakeSavedSettingsStore(),
        object(),
    )


async def _successful_prepare(
    *,
    readiness_seconds: int,
    command_timeout_seconds: int,
    migration_timeout_seconds: int,
) -> bool:
    assert readiness_seconds == 30
    assert command_timeout_seconds == 90
    assert migration_timeout_seconds == 20
    return True


async def _successful_command(*_: object, **__: object) -> int:
    return 0


def _patch_startup_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    store: FakeAppStore,
    supervisor: FakeSupervisor,
) -> None:
    settings = BootstrapSettings.model_validate({"ownership_dir": tmp_path / "ownership"})
    protected = SimpleNamespace(
        policy=SimpleNamespace(
            compose_readiness_seconds=30,
            compose_command_timeout_seconds=90,
            migration_timeout_seconds=20,
        )
    )
    monkeypatch.setattr(launcher, "BootstrapSettings", lambda: settings)
    monkeypatch.setattr(
        launcher, "configure_operational_logging", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(launcher.ProtectedConfig, "load", lambda _: protected)
    monkeypatch.setattr(launcher, "AppOwnershipStore", lambda _: store)
    monkeypatch.setattr(launcher, "prepare_infrastructure", _successful_prepare)
    monkeypatch.setattr(launcher, "build_supervisor", lambda *_args, **_kwargs: supervisor)
    monkeypatch.setattr(
        launcher,
        "build_research_runtime",
        _fake_research_runtime,
    )
    monkeypatch.setattr(launcher, "run_fixed", _successful_command)


@pytest.mark.anyio
async def test_app_ownership_persistence_failure_cleans_only_attempt_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FakeAppStore(fail_replace=True)
    supervisor = FakeSupervisor()
    _patch_startup_dependencies(monkeypatch, tmp_path, store=store, supervisor=supervisor)

    with pytest.raises(RuntimeError, match="injected ownership"):
        await launcher.start()

    assert supervisor.stop_attempt_calls == 1
    assert store.record is None


@pytest.mark.anyio
async def test_pre_uvicorn_failure_removes_app_record_and_cleans_attempt_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FakeAppStore(fail_replace=False)
    supervisor = FakeSupervisor()
    _patch_startup_dependencies(monkeypatch, tmp_path, store=store, supervisor=supervisor)
    monkeypatch.setattr(
        launcher,
        "create_app",
        lambda **_: (_ for _ in ()).throw(RuntimeError("injected app construction failure")),
    )

    with pytest.raises(RuntimeError, match="injected app construction"):
        await launcher.start()

    assert supervisor.stop_attempt_calls == 1
    assert store.record is None
    assert store.removed


@pytest.mark.anyio
async def test_start_propagates_failed_infrastructure_and_saved_logging_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FakeAppStore(fail_replace=False)
    supervisor = FakeSupervisor()
    _patch_startup_dependencies(monkeypatch, tmp_path, store=store, supervisor=supervisor)
    logging_calls: list[dict[str, object]] = []
    app_kwargs: dict[str, object] = {}

    async def failed_prepare(**_: object) -> bool:
        return False

    class NondefaultSavedSettingsStore:
        async def get(self) -> SimpleNamespace:
            return SimpleNamespace(
                values=SimpleNamespace(
                    logging=SimpleNamespace(rotation_size_mib=7, retained_files=3)
                )
            )

    async def runtime(*_: object, **__: object):
        return (
            FakeResearchService(),
            FakeResearchEngine(),
            object(),
            NondefaultSavedSettingsStore(),
            object(),
        )

    def configure(*_: object, **kwargs: object) -> object:
        logging_calls.append(kwargs)
        return object()

    def fail_create_app(**kwargs: object) -> None:
        app_kwargs.update(kwargs)
        raise RuntimeError("stop after startup wiring")

    monkeypatch.setattr(launcher, "prepare_infrastructure", failed_prepare)
    monkeypatch.setattr(launcher, "build_research_runtime", runtime)
    monkeypatch.setattr(launcher, "configure_operational_logging", configure)
    monkeypatch.setattr(launcher, "create_app", fail_create_app)

    with pytest.raises(RuntimeError, match="startup wiring"):
        await launcher.start()

    assert app_kwargs["infrastructure_ready"] is False
    assert logging_calls[-1]["rotation_size_mib"] == 7
    assert logging_calls[-1]["retained_files"] == 3


class ImmediateServer:
    async def serve(self) -> None:
        return None


def _patch_immediate_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "create_app", lambda **_: object())
    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda _config: ImmediateServer())


@pytest.mark.anyio
async def test_foreground_cancellation_waits_for_complete_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FakeAppStore(fail_replace=False)
    supervisor = FakeSupervisor()
    _patch_startup_dependencies(monkeypatch, tmp_path, store=store, supervisor=supervisor)
    _patch_immediate_server(monkeypatch)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    engine_disposed = False
    command_calls: list[tuple[str, ...]] = []

    class BlockingResearchService:
        async def aclose(self) -> None:
            cleanup_started.set()
            await release_cleanup.wait()

    class RecordingResearchEngine:
        async def dispose(self) -> None:
            nonlocal engine_disposed
            engine_disposed = True

    async def runtime(*_: object, **__: object):
        return (
            BlockingResearchService(),
            RecordingResearchEngine(),
            object(),
            FakeSavedSettingsStore(),
            object(),
        )

    async def command(argv: Any, **_: object) -> int:
        command_calls.append(tuple(argv))
        return 0

    monkeypatch.setattr(launcher, "build_research_runtime", runtime)
    monkeypatch.setattr(launcher, "run_fixed", command)

    start_task = asyncio.create_task(launcher.start())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    start_task.cancel()
    await asyncio.sleep(0)

    assert not start_task.done()
    assert supervisor.stop_calls == 0

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert engine_disposed
    assert store.record is None
    assert store.removed
    assert supervisor.stop_calls == 1
    assert command_calls == [("docker", "compose", "down", "--remove-orphans")]


@pytest.mark.anyio
async def test_resource_close_failure_does_not_skip_owned_or_compose_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FakeAppStore(fail_replace=False)
    supervisor = FakeSupervisor()
    _patch_startup_dependencies(monkeypatch, tmp_path, store=store, supervisor=supervisor)
    _patch_immediate_server(monkeypatch)
    engine_disposed = False
    command_calls: list[tuple[str, ...]] = []

    class FailingResearchService:
        async def aclose(self) -> None:
            raise RuntimeError("injected close failure")

    class RecordingResearchEngine:
        async def dispose(self) -> None:
            nonlocal engine_disposed
            engine_disposed = True

    async def runtime(*_: object, **__: object):
        return (
            FailingResearchService(),
            RecordingResearchEngine(),
            object(),
            FakeSavedSettingsStore(),
            object(),
        )

    async def command(argv: Any, **_: object) -> int:
        command_calls.append(tuple(argv))
        return 0

    monkeypatch.setattr(launcher, "build_research_runtime", runtime)
    monkeypatch.setattr(launcher, "run_fixed", command)

    with pytest.raises(RuntimeError, match="incomplete: research service"):
        await launcher.start()

    assert engine_disposed
    assert store.record is None
    assert store.removed
    assert supervisor.stop_calls == 1
    assert command_calls == [("docker", "compose", "down", "--remove-orphans")]


def test_main_reports_foreground_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted_run(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["launcher.py", "start"])
    monkeypatch.setattr(launcher.asyncio, "run", interrupted_run)

    assert launcher.main() == 130
    captured = capsys.readouterr()
    assert captured.out == "Pensae Signal foreground interrupt completed after owned cleanup.\n"
    assert captured.err == ""
