from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pensae.config.protected import ProtectedConfig
from pensae.research.budget import FinalizationBudget, WorkCapacity

RECORD_PATH = Path("tests/fixtures/policy/five_opportunity_expectations.json")
CONFIG_PATH = Path("config/protected.toml")


def _record() -> dict[str, Any]:
    return json.loads(RECORD_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capacity(value: dict[str, int]) -> WorkCapacity:
    return WorkCapacity(**value)


def test_published_policy_matches_the_exact_protected_config_and_versions() -> None:
    record = _record()
    protected = ProtectedConfig.load()
    policy = protected.research
    inventory = record["protected_configuration"]

    assert inventory["path"] == str(CONFIG_PATH)
    assert inventory["sha256"] == _sha256(CONFIG_PATH)
    assert inventory["alembic_head"] == "20260722_0005"
    assert inventory["prompt_input_max_tokens"] == policy.prompt_input_max_tokens == 8_192
    assert inventory["context_safety_tokens"] == policy.context_safety_tokens == 2_048
    assert inventory["workflow_version"] == policy.workflow_version == ("phase7.sequential-five.v1")
    assert (
        inventory["planner_prompt_version"]
        == policy.planner_prompt_version
        == ("research-planner.retrieval-v2")
    )
    assert inventory["schema_version"] == policy.schema_version == "phase2.opportunity.v1"
    assert inventory["fingerprint_version"] == policy.fingerprint_version
    assert inventory["similarity_threshold_version"] == policy.similarity_threshold_version
    assert inventory["model_parameter_version"] == policy.model_parameter_version
    assert inventory["llama_build"] == protected.policy.llama_build
    assert inventory["chat_model_id"] == protected.policy.chat_model_id
    assert inventory["embedding_model_id"] == protected.policy.embedding_model_id
    assert inventory["embedding_dimension"] == protected.policy.embedding_dimension
    assert record["bounds"] == asdict(policy.bounds)


def test_phase7_record_freezes_the_counting_contract_without_filler() -> None:
    counting = _record()["counting_contract"]

    assert counting == {
        "target": 5,
        "concept_cap": 8,
        "counts": ["new", "definitely_related"],
        "does_not_count": [
            "automatic_exact_rediscovery",
            "updated_version",
            "unresolved_possible_rediscovery",
            "invalid_candidate",
            "incomplete_candidate",
        ],
        "verdict_affects_count": False,
        "filler_permitted": False,
    }


def test_phase7_five_slot_default_reserve_arithmetic_is_exact() -> None:
    record = _record()
    policy = ProtectedConfig.load().research
    budget = FinalizationBudget.from_policy(policy)
    values = record["finalization_budget"]
    required_for_five = budget.maximum_cost_for_evaluations(5)

    budget.validate_feasibility()
    assert _capacity(values["default_capacity"]) == budget.capacity
    assert _capacity(values["per_target_slot"]) == budget.per_slot
    assert _capacity(values["normal_configured_upstream"]) == budget.upstream_normal
    assert _capacity(values["upstream_plus_three_non_slot_repairs"]) == budget.upstream_floor
    assert _capacity(values["five_target_admission_envelope"]) == required_for_five
    assert _capacity(values["default_remaining_after_five_target_admission"]) == (
        budget.capacity.remaining_after(required_for_five)
    )
    assert required_for_five.total_tokens == 1_120_256
    assert budget.capacity.total_tokens == 1_500_000
    assert budget.capacity.total_tokens - required_for_five.total_tokens == 379_744
    assert values["five_slot_default_feasible"] is True


def test_phase7_record_states_the_eight_maximum_cost_default_limitation_truthfully() -> None:
    record = _record()
    policy = ProtectedConfig.load().research
    budget = FinalizationBudget.from_policy(policy)
    values = record["finalization_budget"]
    required_for_eight = budget.maximum_cost_for_evaluations(8)
    ceiling = _capacity(values["ceiling_capacity"])

    required_for_eight_with_ceiling_repairs = (
        budget.upstream_normal + budget.normal_slot.scaled(8) + budget.repair_cost.scaled(16)
    )

    assert _capacity(values["full_eight_candidate_pool_with_default_repairs"]) == (
        required_for_eight
    )
    assert _capacity(values["full_eight_candidate_pool_with_repair_ceiling"]) == (
        required_for_eight_with_ceiling_repairs
    )
    assert required_for_eight.total_tokens == 1_255_424
    assert required_for_eight.bytes == 226_492_416
    assert required_for_eight_with_ceiling_repairs.model_calls == 112
    assert required_for_eight_with_ceiling_repairs.total_tokens == 1_370_112
    assert required_for_eight.total_tokens <= budget.capacity.total_tokens
    assert required_for_eight.bytes > budget.capacity.bytes
    assert required_for_eight.total_tokens <= ceiling.total_tokens
    assert required_for_eight.bytes <= ceiling.bytes
    assert values["eight_slot_default_token_feasible"] is True
    assert values["eight_slot_default_byte_feasible"] is False
    assert values["eight_slot_ceiling_feasible"] is True
    assert "201326592-byte" in values["limitation"]
    assert "226492416-byte" in values["limitation"]
