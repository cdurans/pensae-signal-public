from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_release_check_preserves_the_exact_g8_order() -> None:
    script = (REPO_ROOT / "scripts/linux/release_check.sh").read_text(encoding="utf-8")
    targets = [
        "release-freeze-locks",
        "release-static",
        "release-deterministic",
        "release-integration",
        "release-empty-migration",
        "release-generated-client",
        "release-frontend-build",
        "release-browser-scenarios",
        "release-packaging",
        "release-cleanup",
    ]
    offsets = [script.index(target) for target in targets]
    assert offsets == sorted(offsets)
    assert script.count("run_stage ") == len(targets)


def test_release_subcommands_are_offline_and_disposable() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    release_script = (REPO_ROOT / "scripts/linux/release_check.sh").read_text(encoding="utf-8")
    migration_script = (REPO_ROOT / "scripts/linux/test_empty_migration.sh").read_text(
        encoding="utf-8"
    )

    assert "uv lock --check --offline" in makefile
    assert "pnpm install --frozen-lockfile --offline" in makefile
    assert "uv run --frozen --offline" in makefile
    assert "PENSAE_SKIP_FRONTEND_BUILD=1 PENSAE_BROWSER_RELEASE_ONLY=1" in makefile
    assert "pensae_release_migration" in migration_script
    assert "down --volumes --remove-orphans" in migration_script
    assert "PENSAE_ENVIRONMENT=test" in migration_script
    assert "mktemp -d /tmp/pensae-release-check.XXXXXX" in release_script
    assert "PENSAE_RELEASE_OWNERSHIP_DIR" in release_script
    assert 'PYTHONPYCACHEPREFIX="${RELEASE_PYCACHE_DIR}"' in release_script
    assert 'find "${RELEASE_PYCACHE_DIR}" -depth -delete' in release_script
    assert 'rmdir "${RELEASE_RUNTIME_DIR}"' in release_script
    assert "make stop" not in release_script
    assert "make start" not in release_script


def test_release_browser_selector_includes_both_phase7_outcomes() -> None:
    script = (REPO_ROOT / "scripts/linux/test_browser.sh").read_text(encoding="utf-8")

    assert "p7_five_opportunity.spec.ts" in script
    assert "P7 commits five sequential" in script
    assert "P7 reports an honest shortfall" in script
