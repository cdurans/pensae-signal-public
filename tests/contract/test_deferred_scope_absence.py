from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import yaml

from pensae.infrastructure.db import schema

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_API_PATHS = {
    "/api/bootstrap",
    "/api/opportunities",
    "/api/opportunities/{opportunity_id}",
    "/api/opportunities/{opportunity_id}/favorite",
    "/api/opportunities/{opportunity_id}/lifecycle-targets",
    "/api/opportunities/{opportunity_id}/merge",
    "/api/opportunities/{opportunity_id}/merge/reverse",
    "/api/opportunities/{opportunity_id}/note",
    "/api/opportunities/{opportunity_id}/rediscovery-decision",
    "/api/opportunities/{opportunity_id}/versions/{version_id}",
    "/api/runs",
    "/api/runs/active",
    "/api/runs/preflight",
    "/api/runs/{run_id}",
    "/api/runs/{run_id}/events",
    "/api/runs/{run_id}/stop",
    "/api/settings",
    "/api/settings/reset",
    "/api/status",
}

EXPECTED_DURABLE_TABLES = {
    "settings",
    "runs",
    "sources",
    "evidence_items",
    "problem_signals",
    "problem_signal_evidence",
    "problem_patterns",
    "problem_pattern_signals",
    "opportunities",
    "opportunity_versions",
    "opportunity_version_evidence",
    "opportunity_version_signals",
    "opportunity_version_patterns",
    "opportunity_relations",
    "lifecycle_events",
    "operational_audit",
}

FORBIDDEN_DIRECT_DEPENDENCIES = {
    "anthropic",
    "authlib",
    "celery",
    "langsmith",
    "openai",
    "rq",
    "serpapi",
    "stripe",
}

FORBIDDEN_IMPORT_ROOTS = FORBIDDEN_DIRECT_DEPENDENCIES | {
    "boto3",
    "google.cloud",
    "kubernetes",
}


def test_release_surface_has_no_deferred_remote_provider_or_management_api() -> None:
    openapi = json.loads((REPO_ROOT / "openapi.json").read_text(encoding="utf-8"))

    assert set(openapi["paths"]) == EXPECTED_API_PATHS
    assert not any(
        word in path
        for path in openapi["paths"]
        for word in ("auth", "account", "billing", "provider", "worker", "schedule", "checkpoint")
    )


def test_release_has_only_the_approved_services_tables_and_no_hosted_deployment() -> None:
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"postgres", "redis", "searxng"}
    assert set(schema.metadata.tables) == EXPECTED_DURABLE_TABLES
    assert not (REPO_ROOT / ".github" / "workflows").exists()
    assert not (REPO_ROOT / "Dockerfile").exists()
    assert not any(REPO_ROOT.glob("**/docker-compose*.yml"))
    assert not any(REPO_ROOT.glob("**/docker-compose*.yaml"))


def test_release_dependencies_and_imports_exclude_deferred_infrastructure() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        value.split("[", 1)[0].split("<", 1)[0].split(">", 1)[0].split("=", 1)[0]
        for value in project["project"]["dependencies"]
    }
    frontend = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    frontend_dependencies = set(frontend.get("dependencies", {})) | set(
        frontend.get("devDependencies", {})
    )

    assert dependencies.isdisjoint(FORBIDDEN_DIRECT_DEPENDENCIES)
    assert frontend_dependencies.isdisjoint(
        {"@auth0/auth0-react", "firebase", "mobx", "next-auth", "redux", "zustand"}
    )

    imported: set[str] = set()
    for path in (REPO_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not any(
        name == forbidden or name.startswith(f"{forbidden}.")
        for name in imported
        for forbidden in FORBIDDEN_IMPORT_ROOTS
    )


def test_release_frontend_has_no_remote_assets_analytics_or_service_worker() -> None:
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "frontend" / "src").rglob("*")
        if path.is_file() and path.suffix in {".ts", ".tsx", ".css"}
    ).casefold()

    for forbidden in (
        "googletagmanager",
        "google-analytics",
        "segment.io",
        "serviceworker.register",
        "navigator.serviceworker",
        "https://fonts.",
    ):
        assert forbidden not in production_text
