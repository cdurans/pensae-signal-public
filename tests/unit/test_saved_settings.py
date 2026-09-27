from __future__ import annotations

import pytest
from pydantic import ValidationError

from pensae.settings import (
    RESET_CONFIRMATION,
    DiscoveryMode,
    DurableSettingsValue,
    LocalEndpointSettings,
    ResearchSettings,
    ResetSettingsRequest,
    SavedSettings,
    SavedSettingsService,
    WorkflowSettings,
    build_protected_settings_baseline,
)
from pensae.settings.store import _validate_or_upgrade_legacy


def test_protected_baseline_has_readable_metadata_for_every_exposed_field() -> None:
    baseline = build_protected_settings_baseline()

    assert baseline.values.research.preferred_technologies == (
        "Python",
        "TypeScript",
        "C++",
    )
    assert baseline.values.workflow.discovery_queries == 8
    assert baseline.values.workflow.total_run_tokens == 1_500_000
    assert baseline.values.workflow.opportunities == 5
    assert baseline.values.endpoints.searxng_url == "http://127.0.0.1:8888"
    assert all(item.label for item in baseline.field_metadata)
    assert all(item.validation for item in baseline.field_metadata)
    assert all(item.protected_default_explanation for item in baseline.field_metadata)
    token_metadata = next(
        item for item in baseline.field_metadata if item.key == "workflow.total_run_tokens"
    )
    assert (token_metadata.minimum, token_metadata.maximum) == (1_120_256, 1_500_000)
    assert all(item.key != "workflow.opportunities" for item in baseline.field_metadata)


def test_research_values_are_normalized_and_directed_mode_requires_focus() -> None:
    settings = ResearchSettings(
        focus="  recurring\n maintenance   intake ",
        discovery_mode=DiscoveryMode.DIRECTED,
        preferred_technologies=(" Python ", "python", "TypeScript"),
    )

    assert settings.focus == "recurring maintenance intake"
    assert settings.preferred_technologies == ("Python", "TypeScript")

    with pytest.raises(ValidationError, match="directed discovery requires"):
        ResearchSettings(discovery_mode=DiscoveryMode.DIRECTED)
    with pytest.raises(ValidationError, match="at least one preferred"):
        ResearchSettings(preferred_technologies=())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("discovery_queries", 0, "greater than or equal to 1"),
        ("run_queries", 41, "less than or equal to 40"),
        ("model_calls", 257, "less than or equal to 256"),
        ("total_run_tokens", 1_500_001, "less than or equal to 1500000"),
        ("search_retries", 2, "less than or equal to 1"),
        ("planner_output_max_tokens", 2_049, "less than or equal to 2048"),
    ],
)
def test_workflow_values_reject_protected_boundary_violations(
    field: str, value: int, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkflowSettings.model_validate({field: value})


def test_workflow_values_reject_invalid_nested_bounds() -> None:
    with pytest.raises(ValidationError, match="discovery queries cannot exceed"):
        WorkflowSettings(discovery_queries=8, run_queries=7)
    with pytest.raises(ValidationError, match="first-pass pages cannot exceed"):
        WorkflowSettings(first_pass_pages=24, run_pages=23)
    with pytest.raises(ValidationError, match="Input should be 5"):
        WorkflowSettings.model_validate({"opportunities": 4})
    with pytest.raises(ValidationError, match="concepts cannot be fewer"):
        WorkflowSettings(concepts=4)
    with pytest.raises(ValidationError, match="patterns and segments per pattern cannot yield"):
        WorkflowSettings(patterns=1)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"run_queries": 22}, "queries must be at least 23"),
        ({"run_pages": 53}, "pages must be at least 54"),
        ({"model_calls": 91}, "greater than or equal to 92"),
        ({"total_run_tokens": 1_120_255}, "greater than or equal to 1120256"),
        ({"preliminary_survivors": 7, "concepts": 8}, "survivors cannot be fewer"),
    ],
)
def test_workflow_values_reject_cross_field_infeasible_capacity(
    changes: dict[str, int], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkflowSettings.model_validate(changes)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8888",
        "http://example.com:8888",
        "http://127.0.0.1",
        "http://user:secret@127.0.0.1:8888",
        "http://127.0.0.1:8888?query=secret",
    ],
)
def test_local_endpoints_fail_closed(url: str) -> None:
    with pytest.raises(ValidationError):
        LocalEndpointSettings(searxng_url=url)


def test_save_returns_a_normalized_new_revision_and_preserves_prior_values() -> None:
    service = SavedSettingsService()
    initial = service.initial_value()
    snapshot = service.snapshot_for_future_run(initial)
    candidate = initial.values.model_dump(mode="python")
    candidate["research"]["focus"] = "  billing   reconciliation "
    candidate["workflow"]["discovery_queries"] = 6

    saved = service.save(initial, candidate)

    assert saved.revision == initial.revision + 1
    assert saved.values.research.focus == "billing reconciliation"
    assert saved.values.workflow.discovery_queries == 6
    assert initial.values.research.focus is None
    assert snapshot.settings_revision == 1
    assert snapshot.values.research.focus is None
    assert saved.values is not initial.values
    assert saved.values is not snapshot.values


def test_reset_refuses_wrong_confirmation_and_copies_protected_defaults() -> None:
    service = SavedSettingsService()
    initial = service.initial_value()
    changed = service.save(
        initial,
        initial.values.model_copy(
            update={
                "research": ResearchSettings(focus="maintenance intake"),
            }
        ),
    )

    with pytest.raises(ValidationError, match="RESET TO PROTECTED DEFAULTS"):
        service.reset(changed, {"confirmation": "yes"})

    reset = service.reset(
        changed,
        ResetSettingsRequest(confirmation=RESET_CONFIRMATION),
    )

    assert reset.revision == changed.revision + 1
    assert reset.values == service.baseline.values
    assert reset.values is not service.baseline.values
    assert changed.values.research.focus == "maintenance intake"


def test_settings_models_are_frozen_and_reject_unknown_fields() -> None:
    values = SavedSettings()

    with pytest.raises(ValidationError, match="Instance is frozen"):
        values.workflow.discovery_queries = 1
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SavedSettings.model_validate(
            {
                **values.model_dump(mode="python"),
                "temporary_run_override": {"total_run_tokens": 1},
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DurableSettingsValue.model_validate(
            {
                "revision": 1,
                "values": values,
                "active_run": {"total_run_tokens": 1},
            }
        )


def test_save_revalidates_a_preconstructed_settings_instance() -> None:
    service = SavedSettingsService()
    current = service.initial_value()
    invalid_workflow = current.values.workflow.model_copy(update={"run_queries": 1})
    preconstructed = current.values.model_copy(update={"workflow": invalid_workflow})

    with pytest.raises(ValidationError, match="discovery queries cannot exceed"):
        service.save(current, preconstructed)


def _accepted_phase6_row() -> dict[str, object]:
    values = SavedSettingsService().baseline.values.model_dump(mode="python")
    values["research"]["focus"] = "Preserved legacy operator focus"
    values["endpoints"]["chat_url"] = "http://127.0.0.1:8095"
    values["workflow"].update(
        {
            "run_queries": 20,
            "run_pages": 48,
            "preliminary_survivors": 6,
            "concepts": 4,
            "opportunities": 2,
            "model_calls": 32,
            "total_run_tokens": 300_000,
        }
    )
    return {"revision": 7, "values": values}


def test_accepted_phase6_saved_row_preserves_operator_sections_and_uses_p7_workflow() -> None:
    policy = SavedSettingsService()

    upgraded = _validate_or_upgrade_legacy(_accepted_phase6_row(), policy)

    assert upgraded.revision == 7
    assert upgraded.values.research.focus == "Preserved legacy operator focus"
    assert upgraded.values.endpoints.chat_url == "http://127.0.0.1:8095"
    assert upgraded.values.logging == policy.baseline.values.logging
    assert upgraded.values.workflow == policy.baseline.values.workflow
    assert upgraded.values.workflow.opportunities == 5


def test_precalibration_phase7_row_only_raises_token_budget() -> None:
    policy = SavedSettingsService()
    values = policy.baseline.values.model_dump(mode="python")
    values["research"]["focus"] = "Preserved Phase 7 focus"
    values["workflow"]["discovery_queries"] = 6
    values["workflow"]["total_run_tokens"] = 900_000

    upgraded = _validate_or_upgrade_legacy({"revision": 9, "values": values}, policy)

    assert upgraded.revision == 9
    assert upgraded.values.research.focus == "Preserved Phase 7 focus"
    assert upgraded.values.workflow.discovery_queries == 6
    assert upgraded.values.workflow.total_run_tokens == 1_500_000


@pytest.mark.parametrize(
    "mutation",
    [
        lambda values: values["workflow"].update({"unknown_limit": 1}),
        lambda values: values["workflow"].update({"discovery_queries": 20, "run_queries": 10}),
        lambda values: values["endpoints"].update({"chat_url": "https://remote.example:8095"}),
    ],
)
def test_phase6_compatibility_rejects_unknown_infeasible_or_remote_values(mutation) -> None:
    row = _accepted_phase6_row()
    mutation(row["values"])

    with pytest.raises(ValidationError):
        _validate_or_upgrade_legacy(row, SavedSettingsService())
