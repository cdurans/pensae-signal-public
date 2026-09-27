from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pensae.infrastructure.model_runtime import LaunchSpec, ModelRole, ProcessIdentity
from pensae.infrastructure.model_runtime.process import (
    ProcfsProcessController,
    protected_model_environment,
)


def _spec() -> LaunchSpec:
    return LaunchSpec(
        role=ModelRole.CHAT,
        executable=Path("/opt/pensae/llama-server"),
        model_path=Path("/models/chat.gguf"),
        port=8085,
        expected_model="chat-model",
        fixed_arguments=("--ctx-size", "32768"),
    )


def test_protected_model_environment_drops_hidden_llama_and_hf_overrides() -> None:
    legacy_distribution = "W" + "SL_DISTRO_NAME"
    legacy_interop = "W" + "SL_INTEROP"
    environment = protected_model_environment(
        {
            "LLAMA_ARG_TOOLS": "all",
            "LLAMA_ARG_AGENT": "1",
            "LLAMA_ARG_UI": "1",
            "LLAMA_ARG_CORS_ORIGINS": "*",
            "LLAMA_API_KEY": "secret",
            "HF_TOKEN": "secret",
            "GGML_CUDA_FORCE_MMQ": "1",
            "LD_LIBRARY_PATH": "/usr/lib64/nvidia",
            "CUDA_VISIBLE_DEVICES": "0",
            legacy_distribution: "FedoraLinux-44",
            legacy_interop: "/run/legacy-interop",
        }
    )

    assert environment["LD_LIBRARY_PATH"] == "/usr/lib64/nvidia"
    assert environment["CUDA_VISIBLE_DEVICES"] == "0"
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert legacy_distribution not in environment
    assert legacy_interop not in environment
    assert not any(key.startswith(("LLAMA_", "HF_", "GGML_")) for key in environment)


@pytest.mark.anyio
async def test_spawn_passes_only_the_protected_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(pid=7001)

    controller = ProcfsProcessController()
    identity = ProcessIdentity(7001, "boot:7001", "/opt/pensae/llama-server")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(controller, "inspect", lambda _pid: identity)
    monkeypatch.setenv("LLAMA_ARG_TOOLS", "all")
    monkeypatch.setenv("LLAMA_ARG_AGENT", "1")

    assert await controller.spawn(_spec()) == identity
    assert captured["argv"] == _spec().argv
    assert "LLAMA_ARG_TOOLS" not in captured["env"]
    assert "LLAMA_ARG_AGENT" not in captured["env"]
