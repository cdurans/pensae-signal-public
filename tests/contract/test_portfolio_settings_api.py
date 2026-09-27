"""Portfolio and saved-settings API contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from tests.fakes.health import fake_health_service

from pensae.api.app import create_app
from pensae.config.settings import BootstrapSettings, Environment
from pensae.opportunities import (
    DeletionResult,
    LifecycleConflict,
    LifecycleMutationResult,
    PortfolioQuery,
    RediscoveryDecision,
    StableOpportunityMetadata,
)
from pensae.settings import (
    DurableSettingsValue,
    ResetSettingsRequest,
    SavedSettings,
    SavedSettingsService,
)

OPPORTUNITY_ID = UUID("00000000-0000-0000-0000-000000000101")


class FakePortfolio:
    def __init__(self) -> None:
        self.queries: list[PortfolioQuery] = []
        self.favorite = False
        self.note: str | None = None
        self.revision = 1

    async def list(self, query: PortfolioQuery) -> tuple[Any, ...]:
        self.queries.append(query)
        return ()

    async def set_favorite(
        self, opportunity_id: UUID, *, favorite: bool
    ) -> StableOpportunityMetadata:
        assert opportunity_id == OPPORTUNITY_ID
        self.favorite = favorite
        self.revision += 1
        return self._metadata()

    async def set_note(
        self, opportunity_id: UUID, *, note: str | None
    ) -> StableOpportunityMetadata:
        assert opportunity_id == OPPORTUNITY_ID
        self.note = note
        self.revision += 1
        return self._metadata()

    def _metadata(self) -> StableOpportunityMetadata:
        return StableOpportunityMetadata(
            id=OPPORTUNITY_ID,
            revision=self.revision,
            favorite=self.favorite,
            note=self.note,
            updated_at=datetime(2026, 7, 22, tzinfo=UTC),
        )


class FakeSettingsStore:
    def __init__(self) -> None:
        self.policy = SavedSettingsService()
        self.current = self.policy.initial_value()

    @property
    def field_metadata(self):
        return self.policy.baseline.field_metadata

    async def get(self) -> DurableSettingsValue:
        return self.current

    async def save(self, candidate: SavedSettings) -> DurableSettingsValue:
        self.current = self.policy.save(self.current, candidate)
        return self.current

    async def reset(self, request: ResetSettingsRequest) -> DurableSettingsValue:
        self.current = self.policy.reset(self.current, request)
        return self.current


class FakeLifecycle:
    def __init__(self) -> None:
        self.revision = 1

    async def decide_rediscovery(
        self,
        opportunity_id: UUID,
        *,
        target_id: UUID,
        decision: RediscoveryDecision,
        expected_candidate_revision: int,
        expected_target_revision: int,
    ) -> LifecycleMutationResult:
        if expected_candidate_revision != self.revision:
            raise LifecycleConflict("opportunity revision changed; refresh before retrying")
        self.revision += 1
        return LifecycleMutationResult(
            opportunity_id=opportunity_id,
            revision=self.revision,
            classification=decision.value,
            lifecycle_status="active",
            target_opportunity_id=target_id,
            target_revision=expected_target_revision,
        )

    async def merge(
        self,
        opportunity_id: UUID,
        *,
        survivor_id: UUID,
        expected_revision: int,
        expected_survivor_revision: int,
    ) -> LifecycleMutationResult:
        return LifecycleMutationResult(
            opportunity_id=opportunity_id,
            revision=expected_revision + 1,
            classification="related",
            lifecycle_status="merged",
            target_opportunity_id=survivor_id,
            target_revision=expected_survivor_revision,
        )

    async def reverse_merge(
        self, opportunity_id: UUID, *, expected_revision: int
    ) -> LifecycleMutationResult:
        return LifecycleMutationResult(
            opportunity_id=opportunity_id,
            revision=expected_revision + 1,
            classification="related",
            lifecycle_status="active",
        )

    async def delete_permanently(
        self, opportunity_id: UUID, *, expected_revision: int
    ) -> DeletionResult:
        del expected_revision
        return DeletionResult(opportunity_id=opportunity_id, deleted=True, audit_id=UUID(int=999))


def portfolio_settings_app() -> tuple[Any, BootstrapSettings, FakePortfolio, FakeSettingsStore]:
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    portfolio = FakePortfolio()
    saved = FakeSettingsStore()
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        portfolio_service=cast(Any, portfolio),
        settings_store=cast(Any, saved),
        lifecycle_service=cast(Any, FakeLifecycle()),
    )
    return app, settings, portfolio, saved


@pytest.mark.anyio
async def test_portfolio_api_allows_only_industry_and_the_frozen_sorts() -> None:
    app, settings, portfolio, _ = portfolio_settings_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        for sort in ("default", "newest", "weighted_score_desc", "evidence_score_desc"):
            response = await client.get(
                "/api/opportunities",
                params={"industry": "  Property   management ", "sort": sort, "limit": 12},
            )
            assert response.status_code == 200
            assert response.json()["sort"] == sort
            assert response.json()["industry"] == "Property management"
        unsupported_sort = await client.get("/api/opportunities", params={"sort": "name"})
        unsupported_filter = await client.get("/api/opportunities", params={"tag": "urgent"})
        unbounded = await client.get("/api/opportunities", params={"limit": 101})

    assert unsupported_sort.status_code == 422
    assert unsupported_filter.status_code == 400
    assert unbounded.status_code == 422
    assert len(portfolio.queries) == 4


@pytest.mark.anyio
async def test_all_portfolio_settings_mutations_keep_nonce_origin_and_json_guards() -> None:
    app, settings, _, _ = portfolio_settings_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        secure = {
            "x-pensae-session": nonce,
            "origin": settings.canonical_origin,
            "content-type": "application/json",
        }
        mutations = (
            ("PUT", f"/api/opportunities/{OPPORTUNITY_ID}/favorite", {"favorite": True}),
            ("PUT", f"/api/opportunities/{OPPORTUNITY_ID}/note", {"note": "Review this"}),
            (
                "POST",
                f"/api/opportunities/{OPPORTUNITY_ID}/rediscovery-decision",
                {
                    "target_opportunity_id": str(UUID(int=202)),
                    "decision": "related",
                    "expected_candidate_revision": 1,
                    "expected_target_revision": 1,
                },
            ),
            (
                "POST",
                f"/api/opportunities/{OPPORTUNITY_ID}/merge",
                {
                    "survivor_id": str(UUID(int=202)),
                    "expected_revision": 1,
                    "expected_survivor_revision": 1,
                    "confirmation": "MERGE",
                },
            ),
            (
                "POST",
                f"/api/opportunities/{OPPORTUNITY_ID}/merge/reverse",
                {"expected_revision": 2, "confirmation": "REVERSE MERGE"},
            ),
            (
                "DELETE",
                f"/api/opportunities/{OPPORTUNITY_ID}",
                {
                    "expected_revision": 1,
                    "confirmation": f"DELETE {OPPORTUNITY_ID}",
                },
            ),
            ("PUT", "/api/settings", SavedSettings().model_dump(mode="json")),
            (
                "POST",
                "/api/settings/reset",
                {"confirmation": "RESET TO PROTECTED DEFAULTS"},
            ),
        )
        for method, path, body in mutations:
            missing_nonce = await client.request(method, path, json=body)
            wrong_content = await client.request(
                method,
                path,
                content="{}",
                headers={
                    "content-type": "text/plain",
                    "x-pensae-session": nonce,
                    "origin": settings.canonical_origin,
                },
            )
            accepted = await client.request(method, path, json=body, headers=secure)
            assert missing_nonce.status_code == 403
            assert wrong_content.status_code == 415
            assert accepted.status_code == 200, accepted.text


@pytest.mark.anyio
async def test_portfolio_settings_mutation_payloads_fail_closed() -> None:
    app, settings, _, _ = portfolio_settings_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        headers = {"x-pensae-session": nonce, "origin": settings.canonical_origin}
        note = await client.put(
            f"/api/opportunities/{OPPORTUNITY_ID}/note",
            json={"note": "x" * 4_001},
            headers=headers,
        )
        reset = await client.post(
            "/api/settings/reset",
            json={"confirmation": "yes"},
            headers=headers,
        )
        protected_field = SavedSettings().model_dump(mode="json")
        protected_field["launcher_model_path"] = "/forbidden"
        settings_response = await client.put("/api/settings", json=protected_field, headers=headers)

    assert note.status_code == 422
    assert reset.status_code == 422
    assert settings_response.status_code == 422


@pytest.mark.anyio
async def test_lifecycle_api_rejects_stale_revision_and_confirmation_shortcuts() -> None:
    app, settings, _, _ = portfolio_settings_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        nonce = (await client.get("/api/bootstrap")).json()["session_nonce"]
        headers = {"x-pensae-session": nonce, "origin": settings.canonical_origin}
        accepted = await client.post(
            f"/api/opportunities/{OPPORTUNITY_ID}/rediscovery-decision",
            json={
                "target_opportunity_id": str(UUID(int=202)),
                "decision": "rediscovered",
                "expected_candidate_revision": 1,
                "expected_target_revision": 1,
            },
            headers=headers,
        )
        stale = await client.post(
            f"/api/opportunities/{OPPORTUNITY_ID}/rediscovery-decision",
            json={
                "target_opportunity_id": str(UUID(int=202)),
                "decision": "updated",
                "expected_candidate_revision": 1,
                "expected_target_revision": 1,
            },
            headers=headers,
        )
        merge_shortcut = await client.post(
            f"/api/opportunities/{OPPORTUNITY_ID}/merge",
            json={
                "survivor_id": str(UUID(int=202)),
                "expected_revision": 1,
                "expected_survivor_revision": 1,
                "confirmation": "yes",
            },
            headers=headers,
        )
        delete_shortcut = await client.request(
            "DELETE",
            f"/api/opportunities/{OPPORTUNITY_ID}",
            json={"expected_revision": 1, "confirmation": "DELETE"},
            headers=headers,
        )

    assert accepted.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    assert merge_shortcut.status_code == 422
    assert delete_shortcut.status_code == 422


@pytest.mark.anyio
async def test_router_deep_links_are_refresh_safe_without_masking_missing_api(
    tmp_path: Path,
) -> None:
    app, settings, portfolio, saved = portfolio_settings_app()
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>Pensae Signal route shell</main>", encoding="utf-8")
    app = create_app(
        settings=settings,
        health_service=fake_health_service(),
        portfolio_service=cast(Any, portfolio),
        settings_store=cast(Any, saved),
        frontend_dist=frontend,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.canonical_origin
    ) as client:
        portfolio_route = await client.get(
            "/opportunities?industry=Property%20management&sort=newest"
        )
        detail_route = await client.get(f"/opportunities/{OPPORTUNITY_ID}")
        missing_api = await client.get("/api/not-a-route")

    assert portfolio_route.status_code == 200
    assert detail_route.status_code == 200
    assert "Pensae Signal route shell" in portfolio_route.text
    assert missing_api.status_code == 404
