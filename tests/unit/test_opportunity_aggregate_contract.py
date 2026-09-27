from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fakes.opportunities import fake_opportunity_aggregate, fake_run_snapshot

from pensae.opportunities import OpportunityAggregateStore, RunSnapshot


def test_aggregate_store_facade_composes_persistence_capabilities() -> None:
    store = OpportunityAggregateStore(async_sessionmaker())

    assert type(store._runs).__module__ == "pensae.opportunities.run_store"
    assert type(store._writer).__module__ == "pensae.opportunities.writer"
    assert type(store._reader).__module__ == "pensae.opportunities.reader"
    assert type(store._similarity).__module__ == "pensae.opportunities.similarity"


def test_offline_fixture_has_the_expected_minimum_durable_chain() -> None:
    snapshot = fake_run_snapshot()
    aggregate = fake_opportunity_aggregate(snapshot.id)

    assert len(aggregate.sources) == 3
    assert len(aggregate.evidence) == 4
    assert len(aggregate.signals) == 2
    assert len(aggregate.pattern.embedding) == 1024
    assert len(aggregate.opportunity_embedding) == 1024


def test_aggregate_rejects_cross_aggregate_evidence_reference() -> None:
    snapshot = fake_run_snapshot()
    aggregate = fake_opportunity_aggregate(snapshot.id)
    invalid = aggregate.model_dump(mode="python")
    invalid["evidence"][0]["source_id"] = uuid4()

    with pytest.raises(ValidationError, match="aggregate source"):
        type(aggregate).model_validate(invalid)


def test_aggregate_rejects_report_that_invents_evidence_identifier() -> None:
    snapshot = fake_run_snapshot()
    aggregate = fake_opportunity_aggregate(snapshot.id)
    invalid = aggregate.model_dump(mode="python")
    invalid["report"]["supporting_evidence_ids"] = [str(uuid4())]

    with pytest.raises(ValidationError, match="exactly match durable evidence"):
        type(aggregate).model_validate(invalid)


def test_aggregate_rejects_report_evidence_under_the_wrong_kind() -> None:
    snapshot = fake_run_snapshot()
    aggregate = fake_opportunity_aggregate(snapshot.id)
    invalid = aggregate.model_dump(mode="python")
    supporting = invalid["report"]["supporting_evidence_ids"]
    moved = supporting[-1]
    invalid["report"]["supporting_evidence_ids"] = supporting[:-1]
    invalid["report"]["negative_evidence_ids"] = (
        *invalid["report"]["negative_evidence_ids"],
        moved,
    )

    with pytest.raises(ValidationError, match="match durable evidence kinds"):
        type(aggregate).model_validate(invalid)


def test_report_requires_all_seven_claim_distinctions_and_evidence_for_conflicts() -> None:
    aggregate = fake_opportunity_aggregate(fake_run_snapshot().id)
    missing_estimate = aggregate.model_dump(mode="python")
    missing_estimate["report"]["claims"] = [
        claim for claim in missing_estimate["report"]["claims"] if claim["label"] != "estimate"
    ]
    with pytest.raises(ValidationError, match="estimates"):
        type(aggregate).model_validate(missing_estimate)

    unsupported_conflict = aggregate.model_dump(mode="python")
    for claim in unsupported_conflict["report"]["claims"]:
        if claim["label"] == "conflict":
            claim["evidence_ids"] = []
    with pytest.raises(ValidationError, match="conflicts require"):
        type(aggregate).model_validate(unsupported_conflict)


def test_run_snapshot_rejects_prohibited_processing_payloads() -> None:
    with pytest.raises(ValidationError, match="prohibited field"):
        RunSnapshot(
            id=uuid4(),
            effective_config={"nested": {"raw_output": "must not persist"}},
            workflow_version="workflow.v1",
            schema_version="schema.v1",
        )
