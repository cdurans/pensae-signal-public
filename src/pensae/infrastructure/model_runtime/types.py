"""Typed contracts for the Fedora llama.cpp runtime supervisor.

Only protected bootstrap code should construct :class:`LaunchSpec` instances.
The supervisor deliberately accepts no command strings or user-provided
arguments; every process launch is an argument vector assembled here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ModelRole(StrEnum):
    """The two fixed llama.cpp endpoint roles."""

    CHAT = "chat"
    EMBEDDING = "embedding"


STARTUP_ORDER: tuple[ModelRole, ...] = (
    ModelRole.CHAT,
    ModelRole.EMBEDDING,
)

ROLE_PORTS: dict[ModelRole, int] = {
    ModelRole.CHAT: 8085,
    ModelRole.EMBEDDING: 8086,
}

MODEL_BROWSER_DENY_ORIGIN = "https://pensae.invalid"


class EndpointState(StrEnum):
    """Result of a complete endpoint health and capability inspection."""

    FREE = "free"
    STARTING = "starting"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN_LISTENER = "unknown_listener"


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    """One role-specific endpoint observation.

    ``COMPATIBLE`` means that health, model identity, API shape, and the
    role-specific capability probe have all passed. ``STARTING`` is reserved
    for a child created by this launcher attempt that has not become ready.
    """

    state: EndpointState
    detail: str


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Protected, role-specific process launch configuration.

    Paths must be absolute. Fixed arguments are individual argv elements and
    cannot replace the protected host, port, or model arguments.
    """

    role: ModelRole
    executable: Path
    model_path: Path
    port: int
    expected_model: str
    fixed_arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.executable.is_absolute():
            raise ValueError("llama.cpp executable path must be absolute")
        if not self.model_path.is_absolute():
            raise ValueError("model path must be absolute")
        if not 1 <= self.port <= 65_535:
            raise ValueError("model endpoint port must be between 1 and 65535")
        if self.port != ROLE_PORTS[self.role]:
            raise ValueError(
                f"protected {self.role.value} endpoint port must be {ROLE_PORTS[self.role]}"
            )
        if not self.expected_model.strip():
            raise ValueError("expected model identity must not be empty")
        if any("\x00" in argument for argument in self.fixed_arguments):
            raise ValueError("fixed argv elements must not contain NUL")
        protected_flags = {
            "--host",
            "--port",
            "-m",
            "--model",
            "-a",
            "--alias",
            "--ui",
            "--webui",
            "--no-ui",
            "--no-webui",
            "--path",
            "--cors-origins",
            "--cors-credentials",
            "--no-cors-credentials",
            "-ag",
            "--agent",
            "--tools",
            "--ui-mcp-proxy",
            "--webui-mcp-proxy",
        }
        if protected_flags.intersection(self.fixed_arguments):
            raise ValueError("fixed arguments must not override protected host, port, or model")

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the complete fixed argv used with exec-style process launch."""

        return (
            str(self.executable),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--model",
            str(self.model_path),
            "--alias",
            self.expected_model,
            "--no-ui",
            "--cors-origins",
            MODEL_BROWSER_DENY_ORIGIN,
            "--no-cors-credentials",
            *self.fixed_arguments,
        )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Kernel-derived process identity robust against PID recycling."""

    pid: int
    start_identity: str
    executable: str


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    """Durable evidence that a launcher created a model child."""

    role: ModelRole
    pid: int
    start_identity: str
    executable: str
    port: int
    launcher_instance: str

    @classmethod
    def from_child(
        cls,
        *,
        spec: LaunchSpec,
        identity: ProcessIdentity,
        launcher_instance: str,
    ) -> OwnershipRecord:
        return cls(
            role=spec.role,
            pid=identity.pid,
            start_identity=identity.start_identity,
            executable=identity.executable,
            port=spec.port,
            launcher_instance=launcher_instance,
        )

    def matches(self, identity: ProcessIdentity) -> bool:
        return (
            self.pid == identity.pid
            and self.start_identity == identity.start_identity
            and self.executable == identity.executable
        )


class RoleAction(StrEnum):
    """Observable reconciliation disposition for one role."""

    REUSED = "reused"
    ALREADY_OWNED = "already_owned"
    STARTED = "started"
    BLOCKED = "blocked"
    FAILED = "failed"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class RoleResult:
    role: ModelRole
    action: RoleAction
    detail: str
    pid: int | None = None

    @property
    def ready(self) -> bool:
        return self.action in {
            RoleAction.REUSED,
            RoleAction.ALREADY_OWNED,
            RoleAction.STARTED,
        }


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    roles: tuple[RoleResult, ...]

    @property
    def ready(self) -> bool:
        return len(self.roles) == len(STARTUP_ORDER) and all(role.ready for role in self.roles)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(role.detail for role in self.roles if not role.ready)


class StopDisposition(StrEnum):
    STOPPED = "stopped"
    ALREADY_EXITED = "already_exited"
    REFUSED_IDENTITY_MISMATCH = "refused_identity_mismatch"
    SIGNAL_REFUSED = "signal_refused"
    STOP_TIMEOUT = "stop_timeout"


class TerminationOutcome(StrEnum):
    """Result of revalidation, SIGTERM, and bounded exit waiting."""

    EXITED = "exited"
    REFUSED_IDENTITY = "refused_identity"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class StopResult:
    role: ModelRole
    disposition: StopDisposition
    detail: str
    pid: int
