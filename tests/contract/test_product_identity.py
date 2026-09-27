from pathlib import Path

import httpx
import pytest

from pensae.api.app import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_api_and_openapi_expose_pensae_signal_product_identity() -> None:
    app = create_app()
    schema = app.openapi()

    assert app.title == "Pensae Signal"
    assert schema["info"]["title"] == "Pensae Signal"
    assert (
        schema["components"]["schemas"]["ProposedScores"]["properties"]["pensae_feasibility"][
            "title"
        ]
        == "Pensae Feasibility"
    )


def test_product_rename_preserves_compatibility_contracts() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    health = (ROOT / "src" / "pensae" / "infrastructure" / "health" / "live.py").read_text(
        encoding="utf-8"
    )
    source = (
        ROOT / "src" / "pensae" / "infrastructure" / "retrieval" / "safe_source.py"
    ).read_text(encoding="utf-8")
    roles = (ROOT / "src" / "pensae" / "research" / "roles.py").read_text(encoding="utf-8")

    assert "PENSAE_" in environment
    assert '"Pensae capability check"' in health
    assert '"Pensae capability probe"' in health
    assert '"PensaeResearchBot/1.0"' in source
    assert 'f"Pensae role:' in roles


def test_browser_shell_exposes_pensae_signal_identity() -> None:
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    shell = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "<title>Pensae Signal | Local readiness</title>" in index
    assert "Pensae Signal" in shell


@pytest.mark.anyio
async def test_missing_frontend_fallback_exposes_pensae_signal_identity(
    tmp_path: Path,
) -> None:
    app = create_app(frontend_dist=tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        for path in ("/", "/opportunities/example"):
            response = await client.get(path)

            assert response.status_code == 503
            assert "<h1>Pensae Signal</h1>" in response.text
            assert "Run make bootstrap." in response.text


def test_local_search_launcher_and_cli_expose_pensae_signal_identity() -> None:
    search = (ROOT / "infra" / "searxng" / "settings.yml").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "linux" / "launcher.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "pensae" / "cli" / "status.py").read_text(encoding="utf-8")

    assert "instance_name: Pensae Signal local search" in search
    assert 'description="Pensae Signal native Fedora 44 launcher"' in launcher
    assert "Pensae Signal capability preflight" in launcher
    assert "Show safe local Pensae Signal dependency health" in cli
