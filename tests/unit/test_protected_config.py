from dataclasses import replace
from pathlib import Path

import pytest

from pensae.config.protected import ProtectedConfig
from pensae.config.settings import BootstrapSettings
from pensae.infrastructure.model_runtime import ModelRole
from pensae.research.budget import (
    BudgetLimitExceeded,
    FinalizationBudget,
    WorkCapacity,
    WorkDimension,
)


def test_protected_config_builds_fixed_exec_argument_vectors() -> None:
    protected = ProtectedConfig.load()
    assert protected.policy.compose_command_timeout_seconds == 300
    assert protected.policy.migration_timeout_seconds == 60
    assert protected.research.workflow_version == "phase7.sequential-five.v1"
    assert protected.research.schema_version == "phase2.opportunity.v1"
    assert protected.research.fingerprint_version == "sha256-canonical-json-v1"
    assert protected.research.model_parameter_version == "llama-chat-nonthinking-grammar-v2"
    assert protected.research.similarity_threshold_version == "phase5.labeled-offline-live-v1"
    assert protected.research.related_similarity_threshold == 0.6
    assert protected.research.possible_rediscovery_similarity_threshold == 0.82
    assert protected.research.repair_max == 1
    assert protected.research.score_weights == (35, 30, 25, 10)
    assert protected.research.temperature == 0.7
    assert protected.research.top_p == 0.8
    assert protected.research.top_k == 20
    assert protected.research.min_p == 0
    assert protected.research.presence_penalty == 1.5
    assert protected.research.repetition_penalty == 1
    assert protected.research.prompt_input_max_tokens == 8_192
    assert protected.research.context_safety_tokens == 2_048
    assert (
        protected.research.planner_output_max_tokens,
        protected.research.problem_analyst_output_max_tokens,
        protected.research.product_strategist_output_max_tokens,
        protected.research.opportunity_analyst_output_max_tokens,
    ) == (2_048, 4_096, 4_096, 6_144)
    assert protected.research.bounds == (
        protected.research.bounds.__class__(
            discovery_queries=8,
            results_per_query=10,
            unique_urls=60,
            first_pass_pages=24,
            run_queries=40,
            run_pages=96,
            signals=30,
            patterns=10,
            segments_per_pattern=3,
            preliminary_survivors=12,
            concepts=8,
            focused_queries_per_concept=3,
            focused_pages_per_concept=6,
            opportunities=5,
            search_retries=1,
            public_page_retries=0,
            source_bytes=3_145_728,
            run_bytes=201_326_592,
            run_bytes_ceiling=402_653_184,
            model_calls=160,
            model_calls_ceiling=256,
            repairs=8,
            repairs_ceiling=16,
            total_run_tokens=1_500_000,
            total_run_tokens_ceiling=1_500_000,
            search_timeout_seconds=30,
            model_timeout_seconds=120,
            embedding_timeout_seconds=60,
            redis_stream_events=1_000,
            redis_active_ttl_seconds=86_400,
            redis_terminal_ttl_seconds=3_600,
            redis_cancellation_ttl_seconds=86_400,
        )
    )
    settings = BootstrapSettings.model_validate(
        {
            "llama_executable": Path("/opt/llama/llama-server"),
            "chat_model_path": Path("/models/chat.gguf"),
            "embedding_model_path": Path("/models/embedding.gguf"),
        }
    )

    specs = protected.launch_specs(settings)

    assert tuple(specs) == (ModelRole.CHAT, ModelRole.EMBEDDING)
    assert specs[ModelRole.CHAT].argv[:7] == (
        "/opt/llama/llama-server",
        "--host",
        "127.0.0.1",
        "--port",
        "8085",
        "--model",
        "/models/chat.gguf",
    )
    assert specs[ModelRole.EMBEDDING].argv[4] == "8086"
    assert "--embedding" in specs[ModelRole.EMBEDDING].argv
    for spec in specs.values():
        assert spec.argv[spec.argv.index("--alias") + 1] == spec.expected_model
        assert "--no-ui" in spec.argv
        assert spec.argv[spec.argv.index("--cors-origins") + 1] == "https://pensae.invalid"
        assert "--no-cors-credentials" in spec.argv


def test_protected_config_rejects_noncanonical_repair_or_score_policy(
    tmp_path: Path,
) -> None:
    original = Path("config/protected.toml").read_text(encoding="utf-8")
    invalid_repair = tmp_path / "invalid-repair.toml"
    invalid_repair.write_text(
        original.replace("repair_max = 1", "repair_max = 2"), encoding="utf-8"
    )
    invalid_weights = tmp_path / "invalid-weights.toml"
    invalid_weights.write_text(
        original.replace("commercial_weight = 35", "commercial_weight = 34"),
        encoding="utf-8",
    )
    invalid_page_retries = tmp_path / "invalid-page-retries.toml"
    invalid_page_retries.write_text(
        original.replace("public_page_retries = 0", "public_page_retries = 1"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="repair_max"):
        ProtectedConfig.load(invalid_repair)
    with pytest.raises(ValueError, match="weights"):
        ProtectedConfig.load(invalid_weights)
    with pytest.raises(ValueError, match="public_page_retries"):
        ProtectedConfig.load(invalid_page_retries)


def test_phase7_finalization_reserve_is_exact_and_blocks_optional_spend() -> None:
    budget = FinalizationBudget.from_policy(ProtectedConfig.load().research)

    assert budget.target == 5
    assert budget.per_slot == WorkCapacity(
        queries=3,
        pages=6,
        bytes=18_874_368,
        model_calls=5,
        repairs=1,
        total_tokens=59_392,
    )
    assert budget.upstream_floor == WorkCapacity(
        queries=8,
        pages=24,
        bytes=75_497_472,
        model_calls=67,
        repairs=3,
        total_tokens=823_296,
    )
    assert budget.upstream_floor + budget.reserve_for_slots(5) == WorkCapacity(
        queries=23,
        pages=54,
        bytes=169_869_312,
        model_calls=92,
        repairs=8,
        total_tokens=1_120_256,
    )
    assert budget.maximum_cost_for_evaluations(8) == WorkCapacity(
        queries=32,
        pages=72,
        bytes=226_492_416,
        model_calls=104,
        repairs=8,
        total_tokens=1_255_424,
    )
    ceiling_bounds = replace(
        ProtectedConfig.load().research.bounds,
        run_bytes=402_653_184,
        model_calls=256,
        repairs=16,
        total_run_tokens=1_500_000,
    )
    ceiling = FinalizationBudget.from_policy(
        ProtectedConfig.load().research,
        ceiling_bounds,
    )
    assert ceiling.maximum_cost_for_evaluations(8) == WorkCapacity(
        queries=32,
        pages=72,
        bytes=226_492_416,
        model_calls=112,
        repairs=16,
        total_tokens=1_370_112,
    )
    snapshot = budget.snapshot(consumed=WorkCapacity(), remaining_target_slots=5)
    assert snapshot.reserved == budget.per_slot.scaled(5)
    assert snapshot.available == WorkCapacity(
        queries=25,
        pages=66,
        bytes=106_954_752,
        model_calls=135,
        repairs=3,
        total_tokens=1_203_040,
    )

    with pytest.raises(BudgetLimitExceeded) as raised:
        budget.admit(
            consumed=WorkCapacity(),
            request=WorkCapacity(queries=26),
            remaining_target_slots=5,
            stage="discovery_search",
        )
    assert raised.value.dimension is WorkDimension.QUERIES
    assert raised.value.stage == "discovery_search"
    assert raised.value.available == 25
    assert raised.value.reserved == 15


def test_protected_config_rejects_cross_field_infeasible_target_capacity(
    tmp_path: Path,
) -> None:
    original = Path("config/protected.toml").read_text(encoding="utf-8")
    invalid = tmp_path / "infeasible.toml"
    invalid.write_text(original.replace("model_calls = 160", "model_calls = 61"), encoding="utf-8")

    with pytest.raises(ValueError, match=r"cross-field infeasible.*model_calls"):
        ProtectedConfig.load(invalid)


def test_protected_config_rejects_pattern_capacity_below_target(tmp_path: Path) -> None:
    original = Path("config/protected.toml").read_text(encoding="utf-8")
    invalid = tmp_path / "infeasible-patterns.toml"
    invalid.write_text(original.replace("patterns = 10", "patterns = 1"), encoding="utf-8")

    with pytest.raises(ValueError, match="patterns and segments per pattern cannot yield"):
        ProtectedConfig.load(invalid)


def test_protected_config_rejects_prompt_calibration_drift(tmp_path: Path) -> None:
    original = Path("config/protected.toml").read_text(encoding="utf-8")
    invalid = tmp_path / "prompt-drift.toml"
    invalid.write_text(
        original.replace("prompt_input_max_tokens = 8192", "prompt_input_max_tokens = 8193"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="calibrated 8192-token"):
        ProtectedConfig.load(invalid)
