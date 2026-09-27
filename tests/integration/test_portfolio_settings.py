"""Portfolio and saved-settings persistence integration tests."""

from __future__ import annotations

import os

import pytest
from tests.fakes.opportunities import fake_opportunity_aggregate, fake_run_snapshot

from pensae.infrastructure.db import create_engine, create_session_factory
from pensae.opportunities import (
    OpportunityAggregateStore,
    PortfolioQuery,
    PortfolioService,
    PortfolioSort,
)
from pensae.settings import (
    DiscoveryMode,
    ResearchSettings,
    ResetSettingsRequest,
    SavedSettingsStore,
)

pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_settings_and_stable_metadata_persist_without_rewriting_versions() -> None:
    engine = create_engine(os.environ["PENSAE_POSTGRES_DSN"])
    session_factory = create_session_factory(engine)
    settings = SavedSettingsStore(session_factory)
    aggregate_store = OpportunityAggregateStore(session_factory)
    portfolio = PortfolioService(session_factory)
    try:
        initial = await settings.get()
        assert initial.revision == 1
        assert initial.values.research.discovery_mode == "broad"

        directed = initial.values.model_copy(
            update={
                "research": ResearchSettings(
                    focus="Property management maintenance",
                    discovery_mode=DiscoveryMode.DIRECTED,
                ),
                "workflow": initial.values.workflow.model_copy(
                    update={"discovery_queries": 4, "run_queries": 19}
                ),
            }
        )
        saved = await settings.save(directed)
        snapshot = await settings.snapshot_for_future_run()
        reset = await settings.reset(
            ResetSettingsRequest(confirmation="RESET TO PROTECTED DEFAULTS")
        )
        assert saved.revision == 2
        assert snapshot.settings_revision == 2
        assert snapshot.values.research.focus == "Property management maintenance"
        assert reset.revision == 3
        assert reset.values.research.focus is None
        assert snapshot.values.research.focus == "Property management maintenance"

        run = fake_run_snapshot()
        aggregate = fake_opportunity_aggregate(run.id).model_copy(
            update={"primary_industry": "Unique portfolio industry"}
        )
        await aggregate_store.create_run(run)
        await aggregate_store.commit(aggregate)
        before = await aggregate_store.get_detail(aggregate.opportunity_id)
        assert before is not None
        assert before.origin_pattern is not None
        assert before.origin_signals
        assert len(before.versions) == 1

        await portfolio.set_favorite(aggregate.opportunity_id, favorite=True)
        metadata = await portfolio.set_note(
            aggregate.opportunity_id, note="  Revisit\r\nafter customer interviews.  "
        )
        after = await aggregate_store.get_detail(aggregate.opportunity_id)
        assert after is not None
        assert metadata.note == "Revisit\nafter customer interviews."
        assert after.favorite is True
        assert after.note == metadata.note
        assert after.report == before.report
        assert after.scores == before.scores
        assert after.verdict == before.verdict
        assert after.provenance == before.provenance

        for sort in PortfolioSort:
            rows = await portfolio.list(
                PortfolioQuery(industry=aggregate.primary_industry, sort=sort)
            )
            assert [row.id for row in rows] == [aggregate.opportunity_id]
            assert rows[0].favorite is True
            assert rows[0].note == metadata.note
        assert await portfolio.list(PortfolioQuery(industry="Not this industry")) == ()
    finally:
        await engine.dispose()
