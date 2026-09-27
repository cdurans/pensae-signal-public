"""Exec-only subprocess and Linux procfs identity adapters."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .types import LaunchSpec, ProcessIdentity, TerminationOutcome

_FIXED_MODEL_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}
_FEDORA_CUDA_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CUDA_HOME",
        "CUDA_PATH",
        "CUDA_VISIBLE_DEVICES",
        "HIP_VISIBLE_DEVICES",
        "LD_LIBRARY_PATH",
        "NVIDIA_DRIVER_CAPABILITIES",
        "NVIDIA_VISIBLE_DEVICES",
        "ROCR_VISIBLE_DEVICES",
    }
)


def protected_model_environment(
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the only environment allowed to reach launcher-created model children."""

    source = os.environ if ambient is None else ambient
    result = dict(_FIXED_MODEL_ENVIRONMENT)
    result.update({key: source[key] for key in _FEDORA_CUDA_ENVIRONMENT_ALLOWLIST if key in source})
    return result


class ProcessController(Protocol):
    """Process operations available to the supervisor.

    There is intentionally no shell-command method.
    """

    async def spawn(self, spec: LaunchSpec) -> ProcessIdentity: ...

    def inspect(self, pid: int) -> ProcessIdentity | None: ...

    async def terminate(
        self, expected: ProcessIdentity, timeout_seconds: float
    ) -> TerminationOutcome: ...


class ProcfsProcessController:
    """Fedora process adapter using exec argv and recycled-PID protection."""

    def __init__(self, *, proc_root: Path = Path("/proc")) -> None:
        self._proc_root = proc_root

    async def spawn(self, spec: LaunchSpec) -> ProcessIdentity:
        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            env=protected_model_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        identity = self.inspect(process.pid)
        if identity is None:
            raise RuntimeError("created model process exited before identity capture")
        return identity

    def inspect(self, pid: int) -> ProcessIdentity | None:
        if pid <= 0:
            return None
        process_dir = self._proc_root / str(pid)
        try:
            stat_text = (process_dir / "stat").read_text(encoding="utf-8")
            executable = str((process_dir / "exe").resolve(strict=True))
            boot_identity = (
                (self._proc_root / "sys" / "kernel" / "random" / "boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
        except OSError:
            return None
        closing_parenthesis = stat_text.rfind(")")
        if closing_parenthesis < 0:
            return None
        fields_after_name = stat_text[closing_parenthesis + 1 :].split()
        # proc(5): field 3 (state) is index 0 here; starttime is field 22.
        if len(fields_after_name) <= 19 or not fields_after_name[19].isdigit():
            return None
        return ProcessIdentity(
            pid=pid,
            start_identity=f"{boot_identity}:{fields_after_name[19]}",
            executable=executable,
        )

    async def terminate(
        self, expected: ProcessIdentity, timeout_seconds: float
    ) -> TerminationOutcome:
        """Revalidate, signal once, and wait boundedly for the owned PID to exit."""

        current = self.inspect(expected.pid)
        if current != expected:
            return TerminationOutcome.REFUSED_IDENTITY
        try:
            os.kill(expected.pid, signal.SIGTERM)
        except OSError:
            return TerminationOutcome.REFUSED_IDENTITY
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self.inspect(expected.pid) == expected:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return TerminationOutcome.TIMED_OUT
            await asyncio.sleep(min(0.05, remaining))
        return TerminationOutcome.EXITED
