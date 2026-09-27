from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from scripts.check_architecture import (
    PINNED_COMPOSE_IMAGES,
    check_no_phase_coupled_production_names,
    validate_compose_contract,
)


def test_native_linux_is_the_only_active_launcher_namespace() -> None:
    native_scripts = Path("scripts/linux")

    assert native_scripts.is_dir()
    assert (native_scripts / "launcher.py").is_file()
    assert not Path("scripts/wsl").exists()


def test_compose_contains_only_allowed_loopback_services() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    searxng_settings = yaml.safe_load(
        Path("infra/searxng/settings.yml").read_text(encoding="utf-8")
    )

    validate_compose_contract(compose, searxng_settings)
    assert {
        name: service["image"] for name, service in compose["services"].items()
    } == PINNED_COMPOSE_IMAGES


@pytest.mark.parametrize(
    "drift",
    ["extra_service", "bridge", "ports", "missing_bind", "non_loopback", "changed_pin"],
)
def test_compose_contract_rejects_host_network_security_drift(drift: str) -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    searxng_settings = yaml.safe_load(
        Path("infra/searxng/settings.yml").read_text(encoding="utf-8")
    )
    candidate = deepcopy(compose)

    if drift == "extra_service":
        candidate["services"]["adminer"] = deepcopy(candidate["services"]["redis"])
    elif drift == "bridge":
        candidate["services"]["postgres"].pop("network_mode")
    elif drift == "ports":
        candidate["services"]["redis"]["ports"] = ["127.0.0.1:6379:6379"]
    elif drift == "missing_bind":
        candidate["services"]["redis"]["command"] = ["redis-server"]
    elif drift == "non_loopback":
        candidate["services"]["searxng"]["environment"]["GRANIAN_HOST"] = "0.0.0.0"  # noqa: S104
    elif drift == "changed_pin":
        candidate["services"]["postgres"]["image"] = "pgvector/pgvector:latest"

    with pytest.raises(ValueError):
        validate_compose_contract(candidate, searxng_settings)


def test_protected_model_arguments_are_arrays_without_shell_fragments() -> None:
    text = Path("config/protected.toml").read_text(encoding="utf-8")

    assert "fixed_args = [" in text
    assert "sh -c" not in text
    assert "bash -c" not in text


@pytest.mark.parametrize("identifier", ["Phase4Workflow", "Phase27Workflow"])
def test_architecture_rejects_phase_coupled_production_identifiers(
    tmp_path: Path, identifier: str
) -> None:
    source = tmp_path / "src" / "pensae"
    source.mkdir(parents=True)
    (source / "workflow.py").write_text(f"class {identifier}:\n    pass\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="phase-coupled production identifier"):
        check_no_phase_coupled_production_names((source,))


@pytest.mark.parametrize("filename", ["phase4_workflow.py", "phase27_workflow.py"])
def test_architecture_rejects_phase_coupled_production_paths(tmp_path: Path, filename: str) -> None:
    source = tmp_path / "src" / "pensae"
    source.mkdir(parents=True)
    (source / filename).write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="phase-coupled production path"):
        check_no_phase_coupled_production_names((source,))


@pytest.mark.parametrize(
    "provenance",
    ["phase2.fixed-two-pass.v1", "phase27.fixed-two-pass.v1"],
)
def test_architecture_allows_provenance_and_excluded_artifacts(
    tmp_path: Path, provenance: str
) -> None:
    source = tmp_path / "src" / "pensae"
    source.mkdir(parents=True)
    (source / "workflow.py").write_text(f'workflow_version = "{provenance}"\n', encoding="utf-8")
    generated = source / "generated"
    generated.mkdir()
    (generated / "phase27_client.py").write_text("class Phase27Client: pass\n", encoding="utf-8")

    check_no_phase_coupled_production_names((source,))


@pytest.mark.parametrize(
    "production_text",
    [
        '"""Phase 4 workflow operations."""\n',
        "# Phase 27 orchestration\nvalue = 1\n",
    ],
)
def test_architecture_rejects_phase_coupled_production_text(
    tmp_path: Path, production_text: str
) -> None:
    source = tmp_path / "src" / "pensae"
    source.mkdir(parents=True)
    (source / "workflow.py").write_text(production_text, encoding="utf-8")

    with pytest.raises(SystemExit, match="phase-coupled production"):
        check_no_phase_coupled_production_names((source,))
