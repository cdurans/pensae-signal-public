from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from scripts.release import audit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _codes(findings: list[audit.Finding] | tuple[audit.Finding, ...]) -> set[str]:
    return {finding.code for finding in findings}


def test_repository_packaging_scope_passes_offline() -> None:
    assert audit.packaging_findings(REPO_ROOT) == ()


def test_release_payload_uses_git_ignores_for_dependency_build_and_cache_dirs(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(
        "node_modules/\nfrontend/dist/\n.pytest_cache/\n", encoding="utf-8"
    )
    kept = tmp_path / "kept.txt"
    kept.write_text("release content\n", encoding="utf-8")
    ignored = (
        tmp_path / "node_modules/package/token.txt",
        tmp_path / "frontend/dist/bundle.js",
        tmp_path / ".pytest_cache/private-key.txt",
    )
    for path in ignored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignored local content\n", encoding="utf-8")
    subprocess.run(("git", "init", "--quiet", str(tmp_path)), check=True)  # noqa: S603, S607

    assert audit.release_files(tmp_path) == (tmp_path / ".gitignore", kept)


def test_secret_model_and_garbage_defects_are_detected_without_printing_payload(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "credentials.txt"
    secret.write_text("-----BEGIN " + "PRIVATE KEY-----\nsecret\n", encoding="utf-8")
    model = tmp_path / "weights.gguf"
    model.write_bytes(b"model bytes")
    cache = tmp_path / "src/__pycache__/module.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"cache")
    files = (secret, model, cache)

    secret_findings = audit.audit_secrets(tmp_path, files)
    model_findings = audit.audit_model_weights(tmp_path, files)
    garbage_findings = audit.audit_garbage(tmp_path, files)

    assert _codes(secret_findings) == {"secret.detected"}
    assert _codes(model_findings) == {"model.redistribution"}
    assert _codes(garbage_findings) == {"garbage.release_payload"}
    assert all("secret\n" not in finding.message for finding in secret_findings)


def test_platform_audit_rejects_active_wsl_namespace(tmp_path: Path) -> None:
    legacy_script = tmp_path / "scripts/wsl/launcher.py"
    legacy_script.parent.mkdir(parents=True)
    legacy_script.write_text("# retired runtime\n", encoding="utf-8")

    findings = audit.audit_active_platform(tmp_path, (legacy_script,))

    assert _codes(findings) == {"platform.wsl_namespace"}


@pytest.mark.parametrize(
    "dependency",
    (
        'child_environment = {"WSL_INTEROP": "enabled"}\n',
        'child_environment = {"WSL_DISTRO_NAME": "FedoraLinux-44"}\n',
        "grep microsoft /proc/sys/kernel/osrelease\n",
        'cuda_library = "/usr/lib/wsl/lib"\n',
        'model_path = "/mnt/c/models/chat.gguf"\n',
    ),
)
def test_platform_audit_rejects_wsl_only_dependencies(tmp_path: Path, dependency: str) -> None:
    active = tmp_path / "src/pensae/runtime.py"
    active.parent.mkdir(parents=True)
    active.write_text(dependency, encoding="utf-8")

    assert _codes(audit.audit_active_platform(tmp_path, (active,))) == {"platform.wsl_dependency"}


def test_platform_audit_rejects_windows_browser_instructions(tmp_path: Path) -> None:
    active = tmp_path / "docs/operator/OPERATIONS.md"
    active.parent.mkdir(parents=True)
    active.write_text("Open http://127.0.0.1:8000 in the Windows browser.\n", encoding="utf-8")

    assert _codes(audit.audit_active_platform(tmp_path, (active,))) == {"platform.windows_browser"}


def test_platform_audit_rejects_localhost_mirroring(tmp_path: Path) -> None:
    active = tmp_path / "README.md"
    active.write_text("Use WSL mirrored localhost to reach Pensae.\n", encoding="utf-8")

    assert _codes(audit.audit_active_platform(tmp_path, (active,))) == {
        "platform.localhost_mirroring"
    }


def test_platform_audit_rejects_wsl_gpu_passthrough_claims(tmp_path: Path) -> None:
    active = tmp_path / "docs/runtime/GPU.md"
    active.parent.mkdir(parents=True)
    active.write_text("The chat model requires WSL GPU passthrough.\n", encoding="utf-8")

    assert _codes(audit.audit_active_platform(tmp_path, (active,))) == {
        "platform.wsl_gpu_passthrough"
    }


def test_platform_audit_allows_history_and_migration_governance(tmp_path: Path) -> None:
    historical_and_governance = (
        tmp_path / "docs/implementation/STATUS.md",
        tmp_path / "docs/implementation/phases/05_HARDENING_RELEASE.md",
        tmp_path / "docs/implementation/tasks/P6-03_PLATFORM_RELEASE_AUDITS.md",
        tmp_path / "docs/implementation/handoffs/P5-04.md",
        tmp_path / "docs/implementation/reviews/PHASE_5_FINAL_REVIEW.md",
        tmp_path / "docs/evaluation/PHASE_5_CALIBRATION.md",
        tmp_path / "docs/release/G7_LOCAL_FEDORA_CHECKLIST.md",
        tmp_path / "docs/decisions/0001-native-fedora-runtime.md",
    )
    legacy_evidence = (
        "scripts/wsl/launcher.py; WSL_INTEROP; /usr/lib/wsl/lib; Windows browser; "
        "localhost mirroring; WSL GPU passthrough\n"
    )
    for path in historical_and_governance:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(legacy_evidence, encoding="utf-8")
    neutral_history = tmp_path / "notes.md"
    neutral_history.write_text("WSL2 was the accepted Phase 5 baseline.\n", encoding="utf-8")

    assert (
        audit.audit_active_platform(tmp_path, (*historical_and_governance, neutral_history)) == []
    )


def test_tracked_environment_and_key_files_are_rejected(tmp_path: Path) -> None:
    environment = tmp_path / ".env"
    environment.write_text("VALUE=private\n", encoding="utf-8")
    key = tmp_path / "operator.pem"
    key.write_text("not even parsed\n", encoding="utf-8")

    findings = audit.audit_secrets(tmp_path, (environment, key))

    assert [finding.code for finding in findings] == [
        "secret.sensitive_file",
        "secret.sensitive_file",
    ]


def test_release_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("-----BEGIN " + "PRIVATE KEY-----\n", encoding="utf-8")
    link = tmp_path / "linked.txt"
    link.symlink_to(outside)

    findings = audit.audit_secrets(tmp_path, (link,))

    assert _codes(findings) == {"packaging.symlink"}


def test_direct_notice_drift_is_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text(
        "Redis 7.4.9 is not vendored or repackaged; re-verify upstream terms before "
        "redistribution. Redis uses RSALv2 or SSPLv1. llama.cpp b10076 is MIT. "
        "pgvector uses the PostgreSQL license. SearXNG is AGPL-3.0.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "_python_direct_versions", lambda _: {"example": "1.2.3"})
    monkeypatch.setattr(audit, "_frontend_direct_versions", lambda _: {})
    monkeypatch.setattr(audit, "_compose_images", lambda _: ())

    findings = audit.audit_notices(tmp_path)

    assert _codes(findings) == {"license.notice_drift"}


def test_repository_license_notices_and_model_policy_are_complete() -> None:
    assert audit.audit_project_license(REPO_ROOT) == []
    assert audit.audit_notices(REPO_ROOT) == []
    assert audit.audit_model_policy(REPO_ROOT) == []


def test_project_license_requires_exact_selected_apache_grant_and_notice(tmp_path: Path) -> None:
    license_path = tmp_path / "LICENSE"
    canonical = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")

    license_path.write_text(canonical, encoding="utf-8")
    assert audit.audit_project_license(tmp_path) == []

    license_path.write_text(
        canonical.replace(
            "Copyright 2026 Pensae contributors.", "Copyright 2026 Another owner.", 1
        ),
        encoding="utf-8",
    )
    assert _codes(audit.audit_project_license(tmp_path)) == {"license.project_incomplete"}

    license_path.write_text(
        "Copyright 2026 Pensae contributors.\n\nMIT License\n"
        "Permission is hereby granted, free of charge.\n"
        'THE SOFTWARE IS PROVIDED "AS IS".\n',
        encoding="utf-8",
    )
    assert _codes(audit.audit_project_license(tmp_path)) == {"license.project_not_open_source"}

    license_path.write_text(
        "Copyright 2026 Pensae contributors.\n\n"
        "Apache License\nVersion 2.0, January 2004\n"
        "http://www.apache.org/licenses/\n"
        "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION\n",
        encoding="utf-8",
    )
    assert _codes(audit.audit_project_license(tmp_path)) == {"license.project_not_open_source"}


def test_migration_audit_accepts_the_single_repository_head() -> None:
    assert audit.audit_migrations(REPO_ROOT) == []


def test_migration_audit_detects_missing_parent_and_multiple_heads(tmp_path: Path) -> None:
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    (versions / "one.py").write_text(
        'revision: str = "one"\ndown_revision: str | None = None\n', encoding="utf-8"
    )
    (versions / "two.py").write_text(
        'revision: str = "two"\ndown_revision: str | None = "absent"\n', encoding="utf-8"
    )

    findings = audit.audit_migrations(tmp_path)

    assert {"migration.missing_parent", "migration.head_count", "migration.disconnected"} <= _codes(
        findings
    )


def test_generated_inputs_have_authoritative_markers_and_generation_command() -> None:
    assert audit.audit_generated_inputs(REPO_ROOT) == []


def test_generated_marker_drift_is_detected(tmp_path: Path) -> None:
    (tmp_path / "frontend/src/api/generated").mkdir(parents=True)
    (tmp_path / "frontend/src/api/generated/types.gen.ts").write_text(
        "export type Value = string;\n", encoding="utf-8"
    )
    (tmp_path / "frontend/package.json").write_text(
        json.dumps(
            {"scripts": {"generate:api": "openapi-ts -i ../openapi.json -o src/api/generated"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "openapi.json").write_text(
        json.dumps({"openapi": "3.1.0", "paths": {"/api/status": {"get": {}}}}),
        encoding="utf-8",
    )

    assert _codes(audit.audit_generated_inputs(tmp_path)) == {"generated.marker_drift"}


def test_locked_chromium_executable_is_available() -> None:
    assert audit.audit_chromium(REPO_ROOT) == []


def test_chromium_check_fails_safely_when_cache_is_empty(tmp_path: Path) -> None:
    assert _codes(audit.audit_chromium(REPO_ROOT, browser_cache=tmp_path)) == {
        "chromium.executable_unavailable"
    }


def test_cleanup_accepts_empty_owned_state_and_persistent_lock(tmp_path: Path) -> None:
    ownership = tmp_path / "ownership"
    ownership.mkdir()
    (ownership / "launcher.lock").write_text("", encoding="utf-8")
    (ownership / "model-processes.json").write_text(
        json.dumps({"version": 1, "records": []}), encoding="utf-8"
    )

    assert audit.audit_ownership(tmp_path, ownership_dir=ownership) == []
    assert audit.cleanup_findings(tmp_path, files=(), ownership_dir=ownership) == ()


def test_cleanup_reports_only_records_and_never_signals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ownership = tmp_path / "ownership"
    ownership.mkdir()
    (ownership / "app-process.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 424242,
                "start_identity": "boot:1",
                "executable": "/opt/pensae/python",
                "launcher_instance": "release-test",
            }
        ),
        encoding="utf-8",
    )
    (ownership / "model-processes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": [
                    {
                        "role": "chat",
                        "pid": 434343,
                        "start_identity": "boot:2",
                        "executable": "/opt/pensae/llama-server",
                        "port": 8085,
                        "launcher_instance": "release-test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def forbidden_signal(*_: object) -> None:
        raise AssertionError("cleanup audit must never signal a process")

    monkeypatch.setattr(os, "kill", forbidden_signal)
    findings = audit.audit_ownership(tmp_path, ownership_dir=ownership)

    assert _codes(findings) == {"cleanup.app_ownership", "cleanup.model_ownership"}
    assert all(
        "424242" not in finding.message and "434343" not in finding.message for finding in findings
    )


def test_cleanup_fails_closed_on_non_object_ownership_record(tmp_path: Path) -> None:
    ownership = tmp_path / "ownership"
    ownership.mkdir()
    (ownership / "model-processes.json").write_text("[]\n", encoding="utf-8")

    findings = audit.audit_ownership(tmp_path, ownership_dir=ownership)

    assert _codes(findings) == {"cleanup.model_ownership_invalid"}


@pytest.mark.parametrize(
    ("scope", "packaging_calls", "cleanup_calls"),
    (("packaging", 1, 0), ("cleanup", 0, 1), ("all", 1, 1)),
)
def test_cli_scope_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
    packaging_calls: int,
    cleanup_calls: int,
) -> None:
    calls = {"packaging": 0, "cleanup": 0}

    def packaging(_: Path) -> tuple[audit.Finding, ...]:
        calls["packaging"] += 1
        return ()

    def cleanup(_: Path) -> tuple[audit.Finding, ...]:
        calls["cleanup"] += 1
        return ()

    monkeypatch.setattr(audit, "packaging_findings", packaging)
    monkeypatch.setattr(audit, "cleanup_findings", cleanup)

    assert audit.main(("--scope", scope, "--root", str(tmp_path))) == 0
    assert calls == {"packaging": packaging_calls, "cleanup": cleanup_calls}
