"""Deterministic release payload and launcher-cleanliness audits.

The audit deliberately does not build artifacts, contact a service, or signal a
process.  It examines the Git release payload plus a designated ownership
directory. The release driver composes the two scopes into the ordered G8 gate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APACHE_2_PENSAE_LICENSE_SHA256 = "776fd7081e02aa9205d4969a4f09f01f1ae2430fe2d23b08e42a22ed21eacc2e"
REQUIRED_RELEASE_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "MODEL_REDISTRIBUTION.md",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "compose.yaml",
    "openapi.json",
    "alembic.ini",
    "config/protected.toml",
    "frontend/package.json",
)
CONTENT_SCAN_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".pnpm-store",
        ".pytest_cache",
        ".ruff_cache",
        ".tools",
        ".uv-cache",
        ".venv",
        "dist",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
GARBAGE_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".pyright",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
GARBAGE_PREFIXES = ("frontend/dist/", ".pensae/", "data/", "logs/", "backups/")
GARBAGE_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".log",
    ".jsonl",
    ".dump",
    ".sql.gz",
    ".pid",
    ".ownership.json",
)
MODEL_SUFFIXES = frozenset({".gguf", ".safetensors", ".ckpt", ".pt", ".pth"})
MODEL_FILENAMES = frozenset({"model.bin", "pytorch_model.bin"})
SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
SECRET_FILENAMES = frozenset({".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
PLATFORM_CONTENT_ALLOWLIST_EXACT = frozenset(
    {
        "docs/decisions/0001-native-fedora-runtime.md",
        "docs/implementation/BASELINE_CHANGE_NATIVE_LINUX.md",
        "docs/implementation/MASTER_PLAN.md",
        "docs/implementation/PROMPT_PLAYBOOK.md",
        "docs/implementation/QUALITY_GATES.md",
        "docs/implementation/REQUIREMENTS_TRACEABILITY.md",
        "docs/implementation/SOURCE_BASELINE.md",
        "docs/implementation/STATUS.md",
        "docs/release/G7_LOCAL_FEDORA_CHECKLIST.md",
        "scripts/release/audit.py",
    }
)
PLATFORM_CONTENT_ALLOWLIST_PREFIXES = (
    "docs/archive/",
    "docs/evaluation/",
    "docs/implementation/handoffs/",
    "docs/implementation/phases/",
    "docs/implementation/reviews/",
    "docs/implementation/tasks/",
    "tests/",
)
PLATFORM_PROHIBITED_PATTERNS = (
    (
        "platform.wsl_namespace_reference",
        re.compile(r"(?:scripts/wsl/|scripts\.wsl\b)", flags=re.IGNORECASE),
        "active content references the retired scripts/wsl runtime namespace",
    ),
    (
        "platform.wsl_dependency",
        re.compile(
            r"(?:\bWSL_(?:DISTRO_NAME|INTEROP)\b|/usr/lib/wsl(?:/|\b)|/mnt/[a-z](?:/|\b)|"
            r"(?:microsoft[^\n]{0,80}/proc/sys/kernel/osrelease|"
            r"/proc/sys/kernel/osrelease[^\n]{0,80}microsoft))",
            flags=re.IGNORECASE,
        ),
        "active content contains a WSL-only environment, kernel, or path dependency",
    ),
    (
        "platform.windows_browser",
        re.compile(
            r"(?:\bWindows(?: 11)?\b[^\n]{0,50}\b(?:owns?|hosts?|runs?)\b[^\n]{0,30}"
            r"\bbrowser\b|\b(?:open|browse|launch)\b[^\n]{0,50}\bWindows browser\b|"
            r"\bWindows browser\b)",
            flags=re.IGNORECASE,
        ),
        "active content contains a Windows-browser operating instruction",
    ),
    (
        "platform.localhost_mirroring",
        re.compile(
            r"(?:\blocalhost mirroring\b|\bmirrored localhost\b|\blocalhostForwarding\b|"
            r"\.wslconfig\b|\bWindows[- ]to[- ]WSL localhost\b)",
            flags=re.IGNORECASE,
        ),
        "active content depends on WSL localhost mirroring",
    ),
    (
        "platform.wsl_gpu_passthrough",
        re.compile(
            r"(?:\bWSL(?:2)?\b[^\n]{0,60}\b(?:GPU|CUDA|passthrough)\b|"
            r"\b(?:GPU|CUDA) passthrough\b|\bWindows(?: 11)? (?:NVIDIA|GPU) driver\b)",
            flags=re.IGNORECASE,
        ),
        "active content contains a WSL or Windows GPU-passthrough claim",
    ),
)


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    """One safely printable audit failure."""

    code: str
    path: str
    message: str


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def release_files(root: Path) -> tuple[Path, ...]:
    """Return tracked and non-ignored untracked release candidates.

    Git applies the repository's ignore rules, so installed dependencies, build
    output, caches, runtime data, and model files ignored by policy are not read
    as release payload.  Already tracked garbage remains visible to the audit.
    """

    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("Git is unavailable for release payload enumeration")
    completed = subprocess.run(  # noqa: S603 - resolved Git, fixed argv, selected repository root
        (
            git_executable,
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Git could not enumerate the release payload")
    paths = {root / os.fsdecode(raw_path) for raw_path in completed.stdout.split(b"\0") if raw_path}
    return tuple(sorted(paths))


def _content_scan_allowed(path: Path, root: Path) -> bool:
    relative = Path(_relative(path, root))
    return not CONTENT_SCAN_EXCLUDED_PARTS.intersection(relative.parts)


def audit_required_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative in REQUIRED_RELEASE_FILES:
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            findings.append(
                Finding(
                    "packaging.required_file", relative, "required release file is missing or empty"
                )
            )
    package_init = root / "src/pensae/__init__.py"
    if not package_init.is_file():
        findings.append(
            Finding("packaging.python_package", "src/pensae/__init__.py", "wheel package is absent")
        )
    return findings


def audit_secrets(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        relative = _relative(path, root)
        environment_variant = path.name.startswith(".env.") and path.name != ".env.example"
        if (
            path.name in SECRET_FILENAMES
            or environment_variant
            or path.suffix.lower() in {".pem", ".key"}
        ):
            findings.append(
                Finding(
                    "secret.sensitive_file", relative, "sensitive file name is in release payload"
                )
            )
            continue
        if path.is_symlink():
            findings.append(
                Finding(
                    "packaging.symlink",
                    relative,
                    "symbolic link is not accepted in the release payload",
                )
            )
            continue
        if not path.is_file() or not _content_scan_allowed(path, root):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            findings.append(Finding("packaging.unreadable", relative, "release file is unreadable"))
            continue
        if b"\0" in content[:8192]:
            continue
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                findings.append(
                    Finding(
                        "secret.detected",
                        relative,
                        f"possible {name} material is in release payload",
                    )
                )
    return findings


def audit_model_weights(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        name = path.name.lower()
        if path.suffix.lower() in MODEL_SUFFIXES or name in MODEL_FILENAMES:
            findings.append(
                Finding(
                    "model.redistribution",
                    _relative(path, root),
                    "model artifact is present in release payload",
                )
            )
    return findings


def audit_garbage(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        relative = _relative(path, root)
        relative_path = Path(relative)
        is_garbage = bool(GARBAGE_PARTS.intersection(relative_path.parts))
        is_garbage = is_garbage or relative == ".env" or relative == ".coverage"
        is_garbage = is_garbage or relative.startswith(GARBAGE_PREFIXES)
        is_garbage = is_garbage or relative.endswith(GARBAGE_SUFFIXES)
        if is_garbage:
            findings.append(
                Finding(
                    "garbage.release_payload",
                    relative,
                    "generated/runtime garbage is in release payload",
                )
            )
    return findings


def _platform_content_allowlisted(relative: str) -> bool:
    return relative in PLATFORM_CONTENT_ALLOWLIST_EXACT or relative.startswith(
        PLATFORM_CONTENT_ALLOWLIST_PREFIXES
    )


def audit_active_platform(root: Path, files: Iterable[Path]) -> list[Finding]:
    """Reject active dependencies on the retired Windows/WSL runtime topology.

    Dated historical evidence and migration-governance paths are deliberately
    allowlisted. The runtime namespace itself is never allowlisted: a release
    candidate containing ``scripts/wsl/**`` fails before content classification.
    """

    findings: list[Finding] = []
    for path in files:
        relative = _relative(path, root)
        if path.exists() and (relative == "scripts/wsl" or relative.startswith("scripts/wsl/")):
            findings.append(
                Finding(
                    "platform.wsl_namespace",
                    relative,
                    "retired scripts/wsl runtime namespace is in the release payload",
                )
            )
            continue
        if (
            _platform_content_allowlisted(relative)
            or not path.is_file()
            or path.suffix.lower() == ".docx"
            or not _content_scan_allowed(path, root)
        ):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            findings.append(Finding("packaging.unreadable", relative, "release file is unreadable"))
            continue
        if b"\0" in content[:8192]:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for code, pattern, message in PLATFORM_PROHIBITED_PATTERNS:
            if pattern.search(text):
                findings.append(Finding(code, relative, message))
    return findings


def _dependency_name(specification: str) -> str:
    return re.split(r"[\[<>=!~; ]", specification, maxsplit=1)[0].lower().replace("_", "-")


def _python_direct_versions(root: Path) -> dict[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    project_table = project["project"]
    dependency_groups = project.get("dependency-groups", {})
    specifications = list(project_table.get("dependencies", ()))
    specifications.extend(dependency_groups.get("dev", ()))
    locked = {
        str(package["name"]).lower().replace("_", "-"): str(package["version"])
        for package in uv_lock["package"]
    }
    return {
        _dependency_name(str(specification)): locked[_dependency_name(str(specification))]
        for specification in specifications
    }


def _frontend_direct_versions(root: Path) -> dict[str, str]:
    package = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    names = tuple(package.get("dependencies", {})) + tuple(package.get("devDependencies", {}))
    lock_text = (root / "pnpm-lock.yaml").read_text(encoding="utf-8")
    versions: dict[str, str] = {}
    for name in names:
        quoted_name = re.escape(name)
        match = re.search(
            rf"(?m)^      ['\"]?{quoted_name}['\"]?:\n"
            rf"        specifier: [^\n]+\n"
            rf"        version: ([^\n]+)$",
            lock_text,
        )
        if match is None:
            raise ValueError(f"frontend direct dependency is absent from pnpm lock: {name}")
        versions[name] = match.group(1).strip().split("(", maxsplit=1)[0].strip("'\"")
    return versions


def _compose_images(root: Path) -> tuple[str, ...]:
    compose_text = (root / "compose.yaml").read_text(encoding="utf-8")
    return tuple(re.findall(r"(?m)^\s+image:\s+(\S+)\s*$", compose_text))


def audit_notices(root: Path) -> list[Finding]:
    path = root / "THIRD_PARTY_NOTICES.md"
    if not path.is_file():
        return [Finding("license.notices_missing", path.name, "dependency notice is absent")]
    text = path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    try:
        dependencies = {**_python_direct_versions(root), **_frontend_direct_versions(root)}
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        return [Finding("license.lock_invalid", path.name, str(exc))]
    for name, version in sorted(dependencies.items()):
        token = f"`{name}` | `{version}`"
        if token not in text:
            findings.append(
                Finding(
                    "license.notice_drift",
                    path.name,
                    f"notice lacks locked dependency {name} {version}",
                )
            )
    for image in _compose_images(root):
        if f"`{image}`" not in text:
            findings.append(
                Finding("license.image_notice", path.name, "notice lacks a pinned Compose image")
            )
    required_phrases = (
        "Redis 7.4.9",
        "RSALv2 or SSPLv1",
        "not vendored or repackaged",
        "re-verify upstream terms before redistribution",
        "llama.cpp b10076",
        "MIT",
        "PostgreSQL license",
        "AGPL-3.0",
    )
    for phrase in required_phrases:
        if phrase not in text:
            findings.append(
                Finding(
                    "license.policy_notice",
                    path.name,
                    f"required release policy is absent: {phrase}",
                )
            )
    if re.search(r"\b(?:UNKNOWN|TBD|TODO)\b", text, flags=re.IGNORECASE):
        findings.append(
            Finding(
                "license.unresolved_marker",
                path.name,
                "notice contains an unresolved license marker",
            )
        )
    return findings


def audit_project_license(root: Path) -> list[Finding]:
    path = root / "LICENSE"
    if not path.is_file():
        return [Finding("license.project_missing", path.name, "project license notice is absent")]
    text = path.read_text(encoding="utf-8")
    if not text.startswith("Copyright 2026 Pensae contributors.\n\n"):
        return [
            Finding(
                "license.project_incomplete",
                path.name,
                "project license lacks the exact operator-selected copyright notice",
            )
        ]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest == APACHE_2_PENSAE_LICENSE_SHA256:
        return []
    return [
        Finding(
            "license.project_not_open_source",
            path.name,
            "project license must contain the complete canonical operator-selected "
            "Apache-2.0 grant",
        )
    ]


def audit_model_policy(root: Path) -> list[Finding]:
    path = root / "MODEL_REDISTRIBUTION.md"
    if not path.is_file():
        return [Finding("model.policy_missing", path.name, "model redistribution policy is absent")]
    text = path.read_text(encoding="utf-8")
    protected = tomllib.loads((root / "config/protected.toml").read_text(encoding="utf-8"))
    policy = protected["policy"]
    required = (
        str(policy["chat_model_id"]),
        str(policy["embedding_model_id"]),
        "unsloth/Qwen3.6-35B-A3B-GGUF",
        "649d7508507b84638732c4f52c24c8b15843c6dca2f3ff793ae07c14a67ebbb3",
        "Qwen/Qwen3-Embedding-0.6B-GGUF",
        "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
        "Apache-2.0",
        "not redistribute",
        "separately",
        "re-verify",
        "GGUF",
    )
    return [
        Finding("model.policy_incomplete", path.name, f"model policy lacks: {phrase}")
        for phrase in required
        if phrase not in text
    ]


def _literal_assignment(tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and (
            isinstance(value.value, str) or value.value is None
        ):
            return value.value
    raise ValueError(f"migration {name} is not a string/None literal")


def audit_migrations(root: Path) -> list[Finding]:
    version_dir = root / "migrations/versions"
    findings: list[Finding] = []
    revisions: dict[str, tuple[str | None, Path]] = {}
    for path in sorted(version_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            revision = _literal_assignment(tree, "revision")
            parent = _literal_assignment(tree, "down_revision")
        except (OSError, SyntaxError, ValueError) as exc:
            findings.append(
                Finding(
                    "migration.invalid",
                    _relative(path, root),
                    f"migration metadata is invalid: {exc}",
                )
            )
            continue
        if revision is None:
            findings.append(
                Finding("migration.invalid", _relative(path, root), "revision cannot be None")
            )
            continue
        if revision in revisions:
            findings.append(
                Finding(
                    "migration.duplicate", _relative(path, root), "duplicate migration revision"
                )
            )
        revisions[revision] = (parent, path)
    if not revisions:
        return [
            *findings,
            Finding("migration.empty", "migrations/versions", "no migrations exist"),
        ]
    referenced = {parent for parent, _ in revisions.values() if parent is not None}
    for parent in sorted(referenced - revisions.keys()):
        findings.append(
            Finding(
                "migration.missing_parent",
                "migrations/versions",
                f"missing parent revision {parent}",
            )
        )
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        findings.append(
            Finding(
                "migration.head_count", "migrations/versions", "migration graph must have one head"
            )
        )
    reachable: set[str] = set()
    current = heads[0] if len(heads) == 1 else None
    while current is not None and current not in reachable and current in revisions:
        reachable.add(current)
        current = revisions[current][0]
    has_cycle = False
    for start in revisions:
        local_path: set[str] = set()
        current = start
        while current is not None and current in revisions:
            if current in local_path:
                has_cycle = True
                break
            local_path.add(current)
            current = revisions[current][0]
    if has_cycle:
        findings.append(
            Finding("migration.cycle", "migrations/versions", "migration graph has a cycle")
        )
    if len(reachable) != len(revisions):
        findings.append(
            Finding(
                "migration.disconnected", "migrations/versions", "migration graph is disconnected"
            )
        )
    return findings


def audit_generated_inputs(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    openapi_path = root / "openapi.json"
    try:
        document = json.loads(openapi_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [Finding("generated.openapi_invalid", "openapi.json", "OpenAPI input is invalid")]
    if (
        not isinstance(document, dict)
        or document.get("openapi") is None
        or not document.get("paths")
    ):
        findings.append(
            Finding(
                "generated.openapi_incomplete",
                "openapi.json",
                "OpenAPI input lacks version or paths",
            )
        )
    package = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    generate_command = package.get("scripts", {}).get("generate:api")
    if generate_command != "openapi-ts -i ../openapi.json -o src/api/generated":
        findings.append(
            Finding(
                "generated.command_drift",
                "frontend/package.json",
                "generated-client command drifted",
            )
        )
    generated_dir = root / "frontend/src/api/generated"
    generated_files = sorted(generated_dir.rglob("*.ts"))
    if not generated_files:
        findings.append(
            Finding(
                "generated.client_missing",
                _relative(generated_dir, root),
                "generated client is absent",
            )
        )
    for path in generated_files:
        first_line = path.read_text(encoding="utf-8").splitlines()[:1]
        if not first_line or "auto-generated" not in first_line[0]:
            findings.append(
                Finding(
                    "generated.marker_drift", _relative(path, root), "generated marker is absent"
                )
            )
    return findings


def _playwright_version(root: Path) -> str:
    versions = _frontend_direct_versions(root)
    return versions["@playwright/test"]


def audit_chromium(root: Path, *, browser_cache: Path | None = None) -> list[Finding]:
    try:
        version = _playwright_version(root)
        registry_path = (
            root
            / "node_modules/.pnpm"
            / f"playwright-core@{version}"
            / "node_modules/playwright-core/browsers.json"
        )
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        chromium = next(item for item in registry["browsers"] if item["name"] == "chromium")
        revision = str(chromium["revision"])
    except (KeyError, OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        return [
            Finding(
                "chromium.registry_unavailable",
                "frontend",
                f"locked Playwright Chromium registry is unavailable: {exc}",
            )
        ]
    cache = browser_cache
    if cache is None:
        configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        cache = (
            Path(configured).expanduser() if configured else Path.home() / ".cache/ms-playwright"
        )
    candidates = (
        cache / f"chromium-{revision}" / "chrome-linux64/chrome",
        cache / f"chromium_headless_shell-{revision}" / "chrome-linux/headless_shell",
        cache / f"chromium-headless-shell-{revision}" / "chrome-linux/headless_shell",
    )
    if not any(path.is_file() and path.stat().st_mode & stat.S_IXUSR for path in candidates):
        return [
            Finding(
                "chromium.executable_unavailable",
                "Playwright browser cache",
                f"locked Playwright Chromium revision {revision} is not executable",
            )
        ]
    return []


def audit_ownership(root: Path, *, ownership_dir: Path | None = None) -> list[Finding]:
    """Verify release-owned launcher state without process discovery or signals."""

    directory = ownership_dir
    if directory is None:
        configured = os.environ.get("PENSAE_RELEASE_OWNERSHIP_DIR")
        directory = Path(configured) if configured else root / ".pensae/ownership"
    findings: list[Finding] = []
    app_path = directory / "app-process.json"
    model_path = directory / "model-processes.json"
    if app_path.exists():
        findings.append(
            Finding(
                "cleanup.app_ownership",
                "ownership/app-process.json",
                "FastAPI launcher ownership record remains; no process was signaled",
            )
        )
    if model_path.exists():
        try:
            payload = json.loads(model_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("unsupported ownership record")
            records = payload["records"] if payload.get("version") == 1 else None
            if not isinstance(records, list):
                raise ValueError("unsupported ownership record")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            findings.append(
                Finding(
                    "cleanup.model_ownership_invalid",
                    "ownership/model-processes.json",
                    "model ownership record is unreadable; no process was signaled",
                )
            )
        else:
            if records:
                findings.append(
                    Finding(
                        "cleanup.model_ownership",
                        "ownership/model-processes.json",
                        "launcher-owned model records remain; no process was signaled",
                    )
                )
    if directory.exists():
        for path in sorted(directory.glob("*.tmp")):
            findings.append(
                Finding(
                    "cleanup.ownership_temporary",
                    f"ownership/{path.name}",
                    "ownership temporary file remains",
                )
            )
    return findings


def packaging_findings(
    root: Path,
    *,
    files: Sequence[Path] | None = None,
    browser_cache: Path | None = None,
) -> tuple[Finding, ...]:
    candidates = tuple(files) if files is not None else release_files(root)
    findings = [
        *audit_required_files(root),
        *audit_secrets(root, candidates),
        *audit_model_weights(root, candidates),
        *audit_garbage(root, candidates),
        *audit_active_platform(root, candidates),
        *audit_project_license(root),
        *audit_notices(root),
        *audit_model_policy(root),
        *audit_migrations(root),
        *audit_generated_inputs(root),
        *audit_chromium(root, browser_cache=browser_cache),
    ]
    return tuple(sorted(set(findings)))


def cleanup_findings(
    root: Path,
    *,
    files: Sequence[Path] | None = None,
    ownership_dir: Path | None = None,
) -> tuple[Finding, ...]:
    candidates = tuple(files) if files is not None else release_files(root)
    findings = [
        *audit_garbage(root, candidates),
        *audit_ownership(root, ownership_dir=ownership_dir),
    ]
    return tuple(sorted(set(findings)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Pensae Signal offline, read-only release audits"
    )
    parser.add_argument("--scope", choices=("packaging", "cleanup", "all"), default="all")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.root.resolve()
    findings: list[Finding] = []
    scopes: list[str] = []
    try:
        if arguments.scope in {"packaging", "all"}:
            scopes.append("packaging")
            findings.extend(packaging_findings(root))
        if arguments.scope in {"cleanup", "all"}:
            scopes.append("cleanup")
            findings.extend(cleanup_findings(root))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"release audit could not run safely: {exc}")
        return 2
    if findings:
        for finding in sorted(set(findings)):
            print(f"FAIL {finding.code} [{finding.path}]: {finding.message}")
        print(f"release audit failed ({', '.join(scopes)}): {len(set(findings))} finding(s)")
        return 1
    print(f"release audit passed ({', '.join(scopes)}; offline and read-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
