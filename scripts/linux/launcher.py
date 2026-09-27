from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import signal
import sys
import tempfile
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from pensae.api.app import create_app
from pensae.config.protected import ProtectedConfig
from pensae.config.settings import BootstrapSettings
from pensae.diagnostics import (
    LauncherOwnershipStatus,
    OwnershipState,
    configure_operational_logging,
)
from pensae.infrastructure.health.live import live_health_service
from pensae.infrastructure.model_runtime import (
    AsyncioClock,
    AtomicOwnershipStore,
    HttpxEndpointProbe,
    ModelRole,
    ModelRuntimeSupervisor,
    OwnershipFormatError,
    ProcfsProcessController,
    ReadinessPolicy,
)
from pensae.research.runtime import build_research_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class AppOwnershipRecord:
    pid: int
    start_identity: str
    executable: str
    launcher_instance: str


class AppOwnershipStore:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("app ownership path must be absolute")
        self._path = path

    def load(self) -> AppOwnershipRecord | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                "app ownership record is unreadable; refusing unsafe action"
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RuntimeError("app ownership record has an unsupported format")
        try:
            record = AppOwnershipRecord(
                pid=int(raw["pid"]),
                start_identity=str(raw["start_identity"]),
                executable=str(raw["executable"]),
                launcher_instance=str(raw["launcher_instance"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("app ownership record is invalid") from exc
        if record.pid <= 0 or not all(
            (record.start_identity, record.executable, record.launcher_instance)
        ):
            raise RuntimeError("app ownership record is invalid")
        return record

    def replace(self, record: AppOwnershipRecord) -> None:
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, **asdict(record)}, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def remove(self) -> None:
        with suppress(FileNotFoundError):
            self._path.unlink()


class LauncherLock(AbstractContextManager["LauncherLock"]):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> LauncherLock:
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("another Pensae Signal launcher operation is active") from exc
        self._descriptor = descriptor
        return self

    def __exit__(self, *_: object) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


async def run_fixed(
    argv: Sequence[str],
    *,
    required: bool = True,
    timeout_seconds: float = 60,
) -> int:
    if not argv or any(not argument for argument in argv):
        raise ValueError("fixed command argv must contain non-empty elements")
    if timeout_seconds <= 0:
        raise ValueError("fixed command timeout must be positive")
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=REPO_ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        # Compose interpolates the whole file even for teardown. These ephemeral
        # values are used only by this exact down command, which starts no service.
        env=(
            {
                **os.environ,
                "POSTGRES_PASSWORD": secrets.token_hex(32),
                "SEARXNG_SECRET": secrets.token_hex(32),
            }
            if tuple(argv) == ("docker", "compose", "down", "--remove-orphans")
            else None
        ),
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            return_code = await process.wait()
    except TimeoutError as exc:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(5):
                await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                pass
        raise TimeoutError(
            f"protected local command exceeded {timeout_seconds:g}s: {argv[0]}"
        ) from exc
    if required and return_code != 0:
        raise RuntimeError(f"protected local command failed with exit {return_code}: {argv[0]}")
    return return_code


class FixedCommandRunner(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        *,
        required: bool = True,
        timeout_seconds: float = 60,
    ) -> int: ...


async def prepare_infrastructure(
    *,
    readiness_seconds: int,
    command_timeout_seconds: int,
    migration_timeout_seconds: int,
    runner: FixedCommandRunner = run_fixed,
) -> bool:
    """Wait boundedly for Compose health, then migrate; degrade without blocking the UI."""

    async def cleanup_partial_compose_start() -> None:
        try:
            await runner(
                ("docker", "compose", "down", "--remove-orphans"),
                required=False,
                timeout_seconds=command_timeout_seconds,
            )
        except (OSError, TimeoutError) as cleanup_error:
            print(f"Compose partial-start cleanup failed safely: {cleanup_error}")

    try:
        compose_result = await runner(
            (
                "docker",
                "compose",
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                str(readiness_seconds),
            ),
            required=False,
            timeout_seconds=command_timeout_seconds,
        )
    except TimeoutError as exc:
        print(f"Compose infrastructure could not start: {exc}")
        await cleanup_partial_compose_start()
        print("FastAPI will start; capability preflight will block research.")
        return False
    except OSError as exc:
        print(f"Compose infrastructure could not start: {exc}")
        print("FastAPI will start; capability preflight will block research.")
        return False
    if compose_result != 0:
        print(
            "Compose infrastructure did not become healthy before the bounded deadline; "
            "FastAPI will start and research preflight will remain blocked."
        )
        await cleanup_partial_compose_start()
        return False
    try:
        backup_result = await runner(
            (
                sys.executable,
                "-m",
                "pensae.operations.database",
                "backup",
                "--if-nonempty",
            ),
            required=False,
            timeout_seconds=migration_timeout_seconds,
        )
    except (OSError, TimeoutError) as exc:
        print(f"Pre-migration backup failed within its protected deadline: {exc}")
        return False
    if backup_result != 0:
        print("Pre-migration backup failed; migration was not attempted.")
        return False
    try:
        migration_result = await runner(
            (sys.executable, "-m", "alembic", "upgrade", "head"),
            required=False,
            timeout_seconds=migration_timeout_seconds,
        )
    except (OSError, TimeoutError) as exc:
        print(f"Database migration command failed within its protected deadline: {exc}")
        return False
    if migration_result != 0:
        print(
            "Database migration is degraded; FastAPI will start and research preflight "
            "will remain blocked."
        )
        return False
    return True


def remove_current_app_ownership(
    store: AppOwnershipStore, process_controller: ProcfsProcessController
) -> None:
    """Remove only this process's immediately revalidated app ownership record."""

    current = process_controller.inspect(os.getpid())
    try:
        record = store.load()
    except RuntimeError:
        return
    if (
        current is not None
        and record is not None
        and current.pid == record.pid
        and current.start_identity == record.start_identity
        and current.executable == record.executable
    ):
        store.remove()


def build_supervisor(
    settings: BootstrapSettings,
    protected: ProtectedConfig,
    *,
    launcher_instance: str,
) -> ModelRuntimeSupervisor:
    specs = protected.launch_specs(settings)
    return ModelRuntimeSupervisor(
        specs=specs,
        endpoint_probe=HttpxEndpointProbe(
            embedding_dimension=protected.policy.embedding_dimension,
            timeout_seconds=10,
        ),
        processes=ProcfsProcessController(),
        ownership=AtomicOwnershipStore(settings.ownership_dir.resolve() / "model-processes.json"),
        clock=AsyncioClock(),
        readiness=ReadinessPolicy(
            timeout_seconds=protected.policy.launcher_readiness_seconds,
            poll_seconds=0.5,
            termination_timeout_seconds=protected.policy.launcher_stop_grace_seconds,
        ),
        launcher_instance=launcher_instance,
    )


def _runtime_paths(settings: BootstrapSettings) -> tuple[Path, Path, Path]:
    root = settings.ownership_dir.resolve()
    return root / "launcher.lock", root / "app-process.json", root / "model-processes.json"


def safe_launcher_ownership_status(settings: BootstrapSettings) -> LauncherOwnershipStatus:
    """Return revalidated ownership dispositions without process details or paths."""

    _, app_path, model_path = _runtime_paths(settings)
    processes = ProcfsProcessController()
    failure_code: str | None = None
    try:
        app_record = AppOwnershipStore(app_path).load()
        if app_record is None:
            app_state = OwnershipState.NONE
        else:
            identity = processes.inspect(app_record.pid)
            app_state = (
                OwnershipState.OWNED
                if identity is not None
                and identity.start_identity == app_record.start_identity
                and identity.executable == app_record.executable
                else OwnershipState.INVALID
            )
            if app_state is OwnershipState.INVALID:
                failure_code = "app_identity_mismatch"
    except RuntimeError:
        app_state = OwnershipState.INVALID
        failure_code = "app_record_invalid"
    role_states = {ModelRole.CHAT: OwnershipState.NONE, ModelRole.EMBEDDING: OwnershipState.NONE}
    try:
        for role, record in AtomicOwnershipStore(model_path).load().items():
            identity = processes.inspect(record.pid)
            role_states[role] = (
                OwnershipState.OWNED
                if identity is not None and record.matches(identity)
                else OwnershipState.INVALID
            )
            if role_states[role] is OwnershipState.INVALID and failure_code is None:
                failure_code = f"{role.value}_identity_mismatch"
    except OwnershipFormatError:
        role_states = {
            ModelRole.CHAT: OwnershipState.INVALID,
            ModelRole.EMBEDDING: OwnershipState.INVALID,
        }
        failure_code = failure_code or "model_record_invalid"
    return LauncherOwnershipStatus(
        app=app_state,
        chat=role_states[ModelRole.CHAT],
        embedding=role_states[ModelRole.EMBEDDING],
        failure_code=failure_code,
    )


async def start() -> int:
    settings = BootstrapSettings()
    operational_logger = configure_operational_logging(
        settings.log_dir,
        rotation_size_mib=10,
        retained_files=5,
        verbose=settings.verbose_logging,
    )
    protected = ProtectedConfig.load(REPO_ROOT / "config/protected.toml")
    launcher_instance = str(uuid.uuid4())
    lock_path, app_record_path, _ = _runtime_paths(settings)
    app_store = AppOwnershipStore(app_record_path)
    process_controller = ProcfsProcessController()
    supervisor: ModelRuntimeSupervisor | None = None
    server: uvicorn.Server | None = None
    research_service = None
    research_engine = None
    portfolio_service = None
    saved_settings_store = None
    lifecycle_service = None

    with LauncherLock(lock_path):
        existing = app_store.load()
        if existing is not None:
            current = process_controller.inspect(existing.pid)
            if current is not None:
                if (
                    current.start_identity == existing.start_identity
                    and current.executable == existing.executable
                ):
                    raise RuntimeError(
                        "Pensae Signal FastAPI is already launcher-owned and running"
                    )
                raise RuntimeError("stale or recycled app PID record blocks safe startup")
            app_store.remove()

        try:
            infrastructure_ready = await prepare_infrastructure(
                readiness_seconds=protected.policy.compose_readiness_seconds,
                command_timeout_seconds=protected.policy.compose_command_timeout_seconds,
                migration_timeout_seconds=protected.policy.migration_timeout_seconds,
            )
            try:
                supervisor = build_supervisor(
                    settings, protected, launcher_instance=launcher_instance
                )
                model_result = await supervisor.reconcile()
                for role in model_result.roles:
                    print(f"model {role.role.value}: {role.action.value} — {role.detail}")
                if not model_result.ready:
                    print(
                        "Model capability is degraded; FastAPI will start but research is blocked."
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"Model launcher configuration is degraded: {exc}")
                print("FastAPI will start; capability preflight will block research.")

            current = process_controller.inspect(os.getpid())
            if current is None:
                raise RuntimeError("could not capture FastAPI launcher process identity")
            app_store.replace(
                AppOwnershipRecord(
                    pid=current.pid,
                    start_identity=current.start_identity,
                    executable=current.executable,
                    launcher_instance=launcher_instance,
                )
            )

            (
                research_service,
                research_engine,
                portfolio_service,
                saved_settings_store,
                lifecycle_service,
            ) = await build_research_runtime(settings, protected)
            try:
                logging_settings = (await saved_settings_store.get()).values.logging
                operational_logger = configure_operational_logging(
                    settings.log_dir,
                    rotation_size_mib=logging_settings.rotation_size_mib,
                    retained_files=logging_settings.retained_files,
                    verbose=settings.verbose_logging,
                )
            except (OSError, RuntimeError, SQLAlchemyError):
                print("Saved logging settings are unavailable; protected logging defaults remain.")
            app = create_app(
                settings=settings,
                health_service=live_health_service(settings),
                research_service=research_service,
                portfolio_service=portfolio_service,
                settings_store=saved_settings_store,
                lifecycle_service=lifecycle_service,
                ownership_status_provider=lambda: safe_launcher_ownership_status(settings),
                operational_logger=operational_logger,
                infrastructure_ready=infrastructure_ready,
                health_service_factory=live_health_service,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=settings.app_port,
                    workers=1,
                    access_log=False,
                    proxy_headers=False,
                )
            )
        except BaseException:
            remove_current_app_ownership(app_store, process_controller)
            if supervisor is not None:
                try:
                    await supervisor.cleanup_failed_start()
                except Exception as cleanup_error:
                    print(f"Owned model startup cleanup failed safely: {cleanup_error}")
            try:
                await run_fixed(("docker", "compose", "down", "--remove-orphans"), required=False)
            except (OSError, TimeoutError) as cleanup_error:
                print(f"Compose startup cleanup failed: {cleanup_error}")
            raise

    if server is None:
        raise RuntimeError("FastAPI server was not constructed")

    async def cleanup_started_runtime() -> None:
        failures: list[str] = []
        if research_service is not None:
            try:
                await research_service.aclose()
            except Exception:
                failures.append("research service")
                print("Research service shutdown cleanup failed safely.")
        if research_engine is not None:
            try:
                await research_engine.dispose()
            except Exception:
                failures.append("research engine")
                print("Research engine shutdown cleanup failed safely.")
        try:
            remove_current_app_ownership(app_store, process_controller)
        except Exception:
            failures.append("FastAPI ownership")
            print("FastAPI ownership shutdown cleanup failed safely.")
        if supervisor is not None:
            try:
                await supervisor.stop()
            except Exception:
                failures.append("owned models")
                print("Owned model shutdown cleanup failed safely.")
        try:
            await run_fixed(("docker", "compose", "down", "--remove-orphans"), required=False)
        except (OSError, TimeoutError):
            failures.append("Compose")
            print("Compose shutdown cleanup failed safely.")
        if failures:
            raise RuntimeError(
                "Pensae Signal shutdown cleanup was incomplete: " + ", ".join(failures)
            )

    try:
        await server.serve()
    finally:
        cleanup_task = asyncio.create_task(
            cleanup_started_runtime(), name="pensae-launcher-shutdown"
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            await asyncio.shield(cleanup_task)
            raise
    return 0


async def status() -> int:
    settings = BootstrapSettings()
    result = await live_health_service(settings).preflight()
    print(f"Pensae Signal capability preflight: {'READY' if result.ready else 'DEGRADED'}")
    for check in result.checks:
        print(f"- {check.dependency.value}: {check.state.value} — {check.summary}")
        if check.action:
            print(f"  action: {check.action}")
    _, app_record_path, model_record_path = _runtime_paths(settings)
    app_record = AppOwnershipStore(app_record_path).load()
    print(f"- FastAPI ownership: {'recorded' if app_record is not None else 'none'}")
    print(f"- Model ownership file: {'present' if model_record_path.exists() else 'none'}")
    return 0


async def stop() -> int:
    settings = BootstrapSettings()
    protected = ProtectedConfig.load(REPO_ROOT / "config/protected.toml")
    lock_path, app_record_path, model_record_path = _runtime_paths(settings)
    app_store = AppOwnershipStore(app_record_path)
    process_controller = ProcfsProcessController()

    with LauncherLock(lock_path):
        record = app_store.load()
        if record is not None:
            current = process_controller.inspect(record.pid)
            if current is not None and not (
                current.start_identity == record.start_identity
                and current.executable == record.executable
            ):
                print("Refused stale or recycled FastAPI PID; no process or service was stopped.")
                return 2
            if current is not None:
                os.kill(current.pid, signal.SIGTERM)
                deadline = asyncio.get_running_loop().time() + 15
                while process_controller.inspect(current.pid) == current:
                    if asyncio.get_running_loop().time() >= deadline:
                        print("Owned FastAPI did not exit before the bounded deadline.")
                        return 2
                    await asyncio.sleep(0.05)
            app_store.remove()

        try:
            supervisor = build_supervisor(settings, protected, launcher_instance=str(uuid.uuid4()))
            for result in await supervisor.stop():
                print(f"model {result.role.value}: {result.disposition.value} — {result.detail}")
        except (OSError, ValueError, RuntimeError) as exc:
            if model_record_path.exists():
                print(f"Model ownership exists but protected config is unavailable: {exc}")
                print("Safe refusal: infrastructure remains running; no model PID was signaled.")
                return 2

        try:
            await run_fixed(("docker", "compose", "down", "--remove-orphans"), required=False)
        except (OSError, TimeoutError) as exc:
            print(f"Compose shutdown did not finish within its protected deadline: {exc}")
            return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pensae Signal native Fedora 44 launcher")
    parser.add_argument("command", choices=("start", "stop", "status"))
    args = parser.parse_args()
    if args.command == "start":
        try:
            return asyncio.run(start())
        except KeyboardInterrupt:
            print("Pensae Signal foreground interrupt completed after owned cleanup.")
            return 130
    if args.command == "stop":
        return asyncio.run(stop())
    return asyncio.run(status())


if __name__ == "__main__":
    raise SystemExit(main())
