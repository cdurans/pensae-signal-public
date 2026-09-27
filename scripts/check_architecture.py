from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import yaml

ALLOWED_SERVICES = {"postgres", "redis", "searxng"}
PINNED_COMPOSE_IMAGES = {
    "postgres": (
        "docker.io/pgvector/pgvector:0.8.2-pg17-bookworm@"
        "sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966"
    ),
    "redis": (
        "docker.io/library/redis:7.4.9-bookworm@"
        "sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954"
    ),
    "searxng": (
        "docker.io/searxng/searxng:2026.7.19-6da6eee26@"
        "sha256:b8ca38ba06eea544d7555e88321e212ddc0d5c3c7de055419cfb2e5c6bf30812"
    ),
}
FORBIDDEN_COMPONENT_NAMES = {"celery", "rq", "scheduler", "worker", "supervisor_agent"}
PRODUCTION_ROOTS = (Path("src"), Path("scripts"), Path("frontend/src"))
PRODUCTION_SUFFIXES = {".py", ".sh", ".ts", ".tsx", ".js", ".jsx"}
IGNORED_PRODUCTION_PARTS = {"__pycache__", "generated"}
PHASE_NAME = re.compile(r"phase[\s_-]*[0-9]+", re.IGNORECASE)
IMMUTABLE_PROVENANCE = re.compile(r"^phase[0-9]+[._-][a-z0-9._-]*v[0-9]+$", re.IGNORECASE)


def validate_compose_contract(compose: object, searxng_settings: object) -> None:
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise ValueError("compose services mapping is required")
    services = set(compose.get("services", {}))
    if services != ALLOWED_SERVICES:
        raise ValueError(
            f"compose services must be exactly {sorted(ALLOWED_SERVICES)}; got {services}"
        )
    for name, service in compose["services"].items():
        if not isinstance(service, dict):
            raise ValueError(f"{name} service configuration must be a mapping")
        if service.get("image") != PINNED_COMPOSE_IMAGES[name]:
            raise ValueError(f"{name} image must match the accepted digest pin")
        if service.get("network_mode") != "host":
            raise ValueError(f"{name} must use Linux host networking")
        if "ports" in service:
            raise ValueError(f"{name} must not publish or map ports")

    postgres_command = compose["services"]["postgres"].get("command")
    if (
        not isinstance(postgres_command, list)
        or "listen_addresses=127.0.0.1" not in postgres_command
    ):
        raise ValueError("postgres must bind explicitly to 127.0.0.1")

    redis_command = compose["services"]["redis"].get("command")
    if not isinstance(redis_command, list):
        raise ValueError("redis fixed command array is required")
    redis_text = " ".join(str(value) for value in redis_command)
    for required in ("--bind 127.0.0.1", "--protected-mode yes", "--save  --appendonly no"):
        if required not in redis_text:
            raise ValueError(f"redis fixed command lacks {required}")

    searxng_environment = compose["services"]["searxng"].get("environment")
    if not isinstance(searxng_environment, dict):
        raise ValueError("SearXNG environment mapping is required")
    if searxng_environment.get("GRANIAN_HOST") != "127.0.0.1":
        raise ValueError("SearXNG Granian must bind explicitly to 127.0.0.1")
    if str(searxng_environment.get("GRANIAN_PORT")) != "8888":
        raise ValueError("SearXNG Granian must use protected port 8888")

    if not isinstance(searxng_settings, dict) or not isinstance(
        searxng_settings.get("server"), dict
    ):
        raise ValueError("SearXNG server settings mapping is required")
    server = searxng_settings["server"]
    if server.get("bind_address") != "127.0.0.1" or server.get("port") != 8888:
        raise ValueError("SearXNG settings must agree with loopback port 8888")


def check_compose() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    searxng_settings = yaml.safe_load(
        Path("infra/searxng/settings.yml").read_text(encoding="utf-8")
    )
    try:
        validate_compose_contract(compose, searxng_settings)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def check_no_shell_model_launch() -> None:
    runtime = Path("src/pensae/infrastructure/model_runtime")
    if not runtime.exists():
        return
    for path in runtime.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "create_subprocess_shell" in text or "shell=True" in text or "os.system" in text:
            raise SystemExit(f"forbidden shell launch surface in {path}")
        ast.parse(text, filename=str(path))


def check_forbidden_components() -> None:
    roots = [Path("src"), Path("frontend/src")]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and any(part in FORBIDDEN_COMPONENT_NAMES for part in path.parts):
                raise SystemExit(f"forbidden component path: {path}")


def _phase_name_violation(value: str, *, allow_provenance: bool = False) -> bool:
    if not PHASE_NAME.search(value):
        return False
    return not (allow_provenance and IMMUTABLE_PROVENANCE.fullmatch(value.strip()))


def _check_python_phase_names(path: Path, text: str) -> None:
    tree = ast.parse(text, filename=str(path))
    identifiers: list[tuple[str, int]] = []
    strings: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            identifiers.append((node.name, node.lineno))
        elif isinstance(node, ast.Name):
            identifiers.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            identifiers.append((node.attr, node.lineno))
        elif isinstance(node, ast.arg):
            identifiers.append((node.arg, node.lineno))
        elif isinstance(node, ast.alias):
            identifiers.append((node.asname or node.name, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append((node.value, node.lineno))
    for value, line in identifiers:
        if _phase_name_violation(value):
            raise SystemExit(f"phase-coupled production identifier in {path}:{line}: {value}")
    for value, line in strings:
        if _phase_name_violation(value, allow_provenance=True):
            raise SystemExit(f"phase-coupled production text in {path}:{line}")
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and _phase_name_violation(token.string):
            raise SystemExit(f"phase-coupled production comment in {path}:{token.start[0]}")


def check_no_phase_coupled_production_names(
    roots: tuple[Path, ...] = PRODUCTION_ROOTS,
) -> None:
    """Reject delivery-milestone naming while preserving immutable version values."""

    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in PRODUCTION_SUFFIXES:
                continue
            if path == Path("scripts/check_architecture.py"):
                continue
            if any(part in IGNORED_PRODUCTION_PARTS for part in path.parts):
                continue
            if path.name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
                continue
            if _phase_name_violation(path.as_posix()):
                raise SystemExit(f"phase-coupled production path: {path}")
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                _check_python_phase_names(path, text)
            elif _phase_name_violation(text):
                raise SystemExit(f"phase-coupled production text in {path}")


def main() -> None:
    check_compose()
    check_no_shell_model_launch()
    check_forbidden_components()
    check_no_phase_coupled_production_names()


if __name__ == "__main__":
    main()
