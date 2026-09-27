"""Protected launch configuration validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from pensae.infrastructure.model_runtime import LaunchSpec, ModelRole, ReadinessPolicy


def _spec(**overrides: object) -> LaunchSpec:
    values: dict[str, object] = {
        "role": ModelRole.CHAT,
        "executable": Path("/opt/pensae/llama-server"),
        "model_path": Path("/models/chat.gguf"),
        "port": 8085,
        "expected_model": "chat-model",
        "fixed_arguments": ("--ctx-size", "32768"),
    }
    values.update(overrides)
    return LaunchSpec(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["executable", "model_path"])
def test_protected_paths_must_be_absolute(field: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        _spec(**{field: Path("relative")})


@pytest.mark.parametrize("port", [0, 65_536])
def test_port_must_be_valid(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        _spec(port=port)


def test_role_port_is_fixed_by_protected_runtime_contract() -> None:
    with pytest.raises(ValueError, match="must be 8085"):
        _spec(port=8086)


@pytest.mark.parametrize(
    "flag",
    [
        "--host",
        "--port",
        "-m",
        "--model",
        "--alias",
        "--ui",
        "--cors-origins",
        "--agent",
        "--tools",
    ],
)
def test_fixed_arguments_cannot_override_protected_endpoint_fields(flag: str) -> None:
    with pytest.raises(ValueError, match="override"):
        _spec(fixed_arguments=(flag, "unsafe"))


def test_exec_argv_protects_model_identity_and_browser_boundary() -> None:
    argv = _spec().argv

    assert argv[argv.index("--alias") + 1] == "chat-model"
    assert "--no-ui" in argv
    assert argv[argv.index("--cors-origins") + 1] == "https://pensae.invalid"
    assert "--no-cors-credentials" in argv


def test_readiness_policy_is_positive_and_bounded() -> None:
    with pytest.raises(ValueError):
        ReadinessPolicy(timeout_seconds=0, poll_seconds=1)
    with pytest.raises(ValueError):
        ReadinessPolicy(timeout_seconds=1, poll_seconds=2)
