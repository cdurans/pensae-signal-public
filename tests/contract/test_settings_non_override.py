from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pensae.settings import (
    BROWSER_EDITABLE_FIELD_PATHS,
    RESET_CONFIRMATION,
    FutureRunSettingsSnapshot,
    SavedSettings,
    SavedSettingsService,
)

FORBIDDEN_SETTING_NAMES = {
    "llama_executable",
    "chat_model_path",
    "embedding_model_path",
    "fixed_args",
    "ownership_dir",
    "data_dir",
    "log_dir",
    "verbose_logging",
    "postgres_dsn",
    "redis_url",
    "credentials",
    "repair_max",
    "run_bytes_ceiling",
    "model_calls_ceiling",
    "repairs_ceiling",
    "total_run_tokens_ceiling",
    "commercial_weight",
    "evidence_weight",
    "feasibility_weight",
    "differentiation_weight",
    "chat_model_id",
    "embedding_model_id",
    "embedding_dimension",
    "workflow_version",
    "schema_version",
    "fingerprint_version",
    "prompt_versions",
    "similarity_threshold_version",
    "related_similarity_threshold",
    "possible_rediscovery_similarity_threshold",
}


def test_browser_schema_is_an_exact_allowlist_without_protected_fields() -> None:
    baseline = SavedSettingsService().baseline
    metadata_paths = {item.key for item in baseline.field_metadata}
    schema_text = json.dumps(SavedSettings.model_json_schema(), sort_keys=True)

    assert metadata_paths == BROWSER_EDITABLE_FIELD_PATHS
    assert not FORBIDDEN_SETTING_NAMES.intersection(schema_text)
    assert not FORBIDDEN_SETTING_NAMES.intersection(metadata_paths)
    assert "postgres" not in schema_text.casefold()
    assert "redis" not in schema_text.casefold()


def test_future_snapshot_accepts_only_a_complete_saved_revision() -> None:
    service = SavedSettingsService()
    initial = service.initial_value()
    snapshot = service.snapshot_for_future_run(initial)

    assert snapshot == FutureRunSettingsSnapshot(
        settings_revision=initial.revision,
        values=initial.values,
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FutureRunSettingsSnapshot.model_validate(
            {
                **snapshot.model_dump(mode="python"),
                "override": {"workflow": {"total_run_tokens": 1}},
            }
        )


def test_later_save_cannot_mutate_an_existing_future_run_snapshot() -> None:
    service = SavedSettingsService()
    initial = service.initial_value()
    existing_snapshot = service.snapshot_for_future_run(initial)
    candidate = initial.values.model_dump(mode="python")
    candidate["workflow"]["total_run_tokens"] = 1_400_000

    saved = service.save(initial, candidate)
    future_snapshot = service.snapshot_for_future_run(saved)

    assert existing_snapshot.settings_revision == 1
    assert existing_snapshot.values.workflow.total_run_tokens == 1_500_000
    assert future_snapshot.settings_revision == 2
    assert future_snapshot.values.workflow.total_run_tokens == 1_400_000


def test_reset_has_one_exact_explicit_request_confirmation() -> None:
    service = SavedSettingsService()
    initial = service.initial_value()

    reset = service.reset(initial, {"confirmation": RESET_CONFIRMATION})
    assert reset.values == service.baseline.values

    for invalid in ({}, {"confirmation": True}, {"confirmation": "reset"}):
        with pytest.raises(ValidationError):
            service.reset(initial, invalid)


def test_service_exposes_no_per_run_override_operation() -> None:
    operation_names = {name.casefold() for name in dir(SavedSettingsService)}

    assert not any("override" in name for name in operation_names)
    assert not any("active_run" in name for name in operation_names)
