from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pensae.config.settings import BootstrapSettings
from pensae.infrastructure.model_runtime import LaunchSpec, ModelRole


@dataclass(frozen=True, slots=True)
class ProtectedPolicy:
    canonical_host: str
    llama_build: str
    chat_model_id: str
    embedding_model_id: str
    embedding_dimension: int
    compose_readiness_seconds: int
    compose_command_timeout_seconds: int
    migration_timeout_seconds: int
    launcher_readiness_seconds: float
    launcher_stop_grace_seconds: float


@dataclass(frozen=True, slots=True)
class ProtectedRole:
    role: ModelRole
    port: int
    fixed_args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProtectedResearchPolicy:
    workflow_version: str
    schema_version: str
    fingerprint_version: str
    planner_prompt_version: str
    problem_analyst_prompt_version: str
    product_strategist_prompt_version: str
    opportunity_analyst_prompt_version: str
    model_parameter_version: str
    similarity_threshold_version: str
    related_similarity_threshold: float
    possible_rediscovery_similarity_threshold: float
    repair_max: int
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    presence_penalty: float
    repetition_penalty: float
    prompt_input_max_tokens: int
    context_safety_tokens: int
    planner_output_max_tokens: int
    problem_analyst_output_max_tokens: int
    product_strategist_output_max_tokens: int
    opportunity_analyst_output_max_tokens: int
    commercial_weight: int
    evidence_weight: int
    feasibility_weight: int
    differentiation_weight: int
    bounds: ProtectedWorkflowBounds

    @property
    def score_weights(self) -> tuple[int, int, int, int]:
        return (
            self.commercial_weight,
            self.evidence_weight,
            self.feasibility_weight,
            self.differentiation_weight,
        )


@dataclass(frozen=True, slots=True)
class ProtectedWorkflowBounds:
    discovery_queries: int
    results_per_query: int
    unique_urls: int
    first_pass_pages: int
    run_queries: int
    run_pages: int
    signals: int
    patterns: int
    segments_per_pattern: int
    preliminary_survivors: int
    concepts: int
    focused_queries_per_concept: int
    focused_pages_per_concept: int
    opportunities: int
    search_retries: int
    public_page_retries: int
    source_bytes: int
    run_bytes: int
    run_bytes_ceiling: int
    model_calls: int
    model_calls_ceiling: int
    repairs: int
    repairs_ceiling: int
    total_run_tokens: int
    total_run_tokens_ceiling: int
    search_timeout_seconds: int
    model_timeout_seconds: int
    embedding_timeout_seconds: int
    redis_stream_events: int
    redis_active_ttl_seconds: int
    redis_terminal_ttl_seconds: int
    redis_cancellation_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class ProtectedConfig:
    policy: ProtectedPolicy
    research: ProtectedResearchPolicy
    chat: ProtectedRole
    embedding: ProtectedRole

    @classmethod
    def load(cls, path: Path = Path("config/protected.toml")) -> ProtectedConfig:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        policy = _mapping(raw, "policy")
        research = _mapping(raw, "research")
        bounds = _mapping(research, "bounds")
        chat = _role(_mapping(raw, "chat"), expected=ModelRole.CHAT)
        embedding = _role(_mapping(raw, "embedding"), expected=ModelRole.EMBEDDING)
        research_policy = ProtectedResearchPolicy(
            workflow_version=_string(research, "workflow_version"),
            schema_version=_string(research, "schema_version"),
            fingerprint_version=_string(research, "fingerprint_version"),
            planner_prompt_version=_string(research, "planner_prompt_version"),
            problem_analyst_prompt_version=_string(research, "problem_analyst_prompt_version"),
            product_strategist_prompt_version=_string(
                research, "product_strategist_prompt_version"
            ),
            opportunity_analyst_prompt_version=_string(
                research, "opportunity_analyst_prompt_version"
            ),
            model_parameter_version=_string(research, "model_parameter_version"),
            similarity_threshold_version=_string(research, "similarity_threshold_version"),
            related_similarity_threshold=_nonnegative_number(
                research, "related_similarity_threshold"
            ),
            possible_rediscovery_similarity_threshold=_nonnegative_number(
                research, "possible_rediscovery_similarity_threshold"
            ),
            repair_max=_positive_integer(research, "repair_max"),
            temperature=_nonnegative_number(research, "temperature"),
            top_p=_positive_number(research, "top_p"),
            top_k=_positive_integer(research, "top_k"),
            min_p=_nonnegative_number(research, "min_p"),
            presence_penalty=_nonnegative_number(research, "presence_penalty"),
            repetition_penalty=_positive_number(research, "repetition_penalty"),
            prompt_input_max_tokens=_positive_integer(research, "prompt_input_max_tokens"),
            context_safety_tokens=_positive_integer(research, "context_safety_tokens"),
            planner_output_max_tokens=_positive_integer(research, "planner_output_max_tokens"),
            problem_analyst_output_max_tokens=_positive_integer(
                research, "problem_analyst_output_max_tokens"
            ),
            product_strategist_output_max_tokens=_positive_integer(
                research, "product_strategist_output_max_tokens"
            ),
            opportunity_analyst_output_max_tokens=_positive_integer(
                research, "opportunity_analyst_output_max_tokens"
            ),
            commercial_weight=_positive_integer(research, "commercial_weight"),
            evidence_weight=_positive_integer(research, "evidence_weight"),
            feasibility_weight=_positive_integer(research, "feasibility_weight"),
            differentiation_weight=_positive_integer(research, "differentiation_weight"),
            bounds=ProtectedWorkflowBounds(
                discovery_queries=_positive_integer(bounds, "discovery_queries"),
                results_per_query=_positive_integer(bounds, "results_per_query"),
                unique_urls=_positive_integer(bounds, "unique_urls"),
                first_pass_pages=_positive_integer(bounds, "first_pass_pages"),
                run_queries=_positive_integer(bounds, "run_queries"),
                run_pages=_positive_integer(bounds, "run_pages"),
                signals=_positive_integer(bounds, "signals"),
                patterns=_positive_integer(bounds, "patterns"),
                segments_per_pattern=_positive_integer(bounds, "segments_per_pattern"),
                preliminary_survivors=_positive_integer(bounds, "preliminary_survivors"),
                concepts=_positive_integer(bounds, "concepts"),
                focused_queries_per_concept=_positive_integer(
                    bounds, "focused_queries_per_concept"
                ),
                focused_pages_per_concept=_positive_integer(bounds, "focused_pages_per_concept"),
                opportunities=_positive_integer(bounds, "opportunities"),
                search_retries=_nonnegative_integer(bounds, "search_retries"),
                public_page_retries=_nonnegative_integer(bounds, "public_page_retries"),
                source_bytes=_positive_integer(bounds, "source_bytes"),
                run_bytes=_positive_integer(bounds, "run_bytes"),
                run_bytes_ceiling=_positive_integer(bounds, "run_bytes_ceiling"),
                model_calls=_positive_integer(bounds, "model_calls"),
                model_calls_ceiling=_positive_integer(bounds, "model_calls_ceiling"),
                repairs=_positive_integer(bounds, "repairs"),
                repairs_ceiling=_positive_integer(bounds, "repairs_ceiling"),
                total_run_tokens=_positive_integer(bounds, "total_run_tokens"),
                total_run_tokens_ceiling=_positive_integer(bounds, "total_run_tokens_ceiling"),
                search_timeout_seconds=_positive_integer(bounds, "search_timeout_seconds"),
                model_timeout_seconds=_positive_integer(bounds, "model_timeout_seconds"),
                embedding_timeout_seconds=_positive_integer(bounds, "embedding_timeout_seconds"),
                redis_stream_events=_positive_integer(bounds, "redis_stream_events"),
                redis_active_ttl_seconds=_positive_integer(bounds, "redis_active_ttl_seconds"),
                redis_terminal_ttl_seconds=_positive_integer(bounds, "redis_terminal_ttl_seconds"),
                redis_cancellation_ttl_seconds=_positive_integer(
                    bounds, "redis_cancellation_ttl_seconds"
                ),
            ),
        )
        if research_policy.repair_max != 1:
            raise ValueError("protected repair_max must be exactly one")
        if sum(research_policy.score_weights) != 100:
            raise ValueError("protected score weights must total 100")
        if research_policy.top_p > 1 or research_policy.min_p > 1:
            raise ValueError("protected probability parameters cannot exceed one")
        if not (
            0
            <= research_policy.related_similarity_threshold
            <= research_policy.possible_rediscovery_similarity_threshold
            <= 1
        ):
            raise ValueError("protected similarity thresholds must be ordered within zero and one")
        if research_policy.bounds.public_page_retries != 0:
            raise ValueError("protected public_page_retries must be zero")
        if research_policy.bounds.search_retries != 1:
            raise ValueError("protected search_retries must be exactly one")
        for default_name, ceiling_name in (
            ("run_bytes", "run_bytes_ceiling"),
            ("model_calls", "model_calls_ceiling"),
            ("repairs", "repairs_ceiling"),
            ("total_run_tokens", "total_run_tokens_ceiling"),
        ):
            if getattr(research_policy.bounds, default_name) > getattr(
                research_policy.bounds, ceiling_name
            ):
                raise ValueError(f"protected {default_name} exceeds its ceiling")
        if research_policy.bounds.first_pass_pages > research_policy.bounds.run_pages:
            raise ValueError("first-pass pages cannot exceed the whole-run page ceiling")
        if research_policy.bounds.discovery_queries > research_policy.bounds.run_queries:
            raise ValueError("discovery queries cannot exceed the whole-run query ceiling")
        from pensae.research.budget import validate_protected_budget

        validate_protected_budget(research_policy)
        return cls(
            policy=ProtectedPolicy(
                canonical_host=_string(policy, "canonical_host"),
                llama_build=_string(policy, "llama_build"),
                chat_model_id=_string(policy, "chat_model_id"),
                embedding_model_id=_string(policy, "embedding_model_id"),
                embedding_dimension=_integer(policy, "embedding_dimension"),
                compose_readiness_seconds=_positive_integer(policy, "compose_readiness_seconds"),
                compose_command_timeout_seconds=_positive_integer(
                    policy, "compose_command_timeout_seconds"
                ),
                migration_timeout_seconds=_positive_integer(policy, "migration_timeout_seconds"),
                launcher_readiness_seconds=_positive_number(policy, "launcher_readiness_seconds"),
                launcher_stop_grace_seconds=_positive_number(policy, "launcher_stop_grace_seconds"),
            ),
            research=research_policy,
            chat=chat,
            embedding=embedding,
        )

    def launch_specs(self, bootstrap: BootstrapSettings) -> dict[ModelRole, LaunchSpec]:
        if bootstrap.llama_executable is None:
            raise ValueError("PENSAE_LLAMA_EXECUTABLE is not configured")
        if bootstrap.chat_model_path is None:
            raise ValueError("PENSAE_CHAT_MODEL_PATH is not configured")
        if bootstrap.embedding_model_path is None:
            raise ValueError("PENSAE_EMBEDDING_MODEL_PATH is not configured")
        if not bootstrap.llama_executable.is_absolute():
            raise ValueError("PENSAE_LLAMA_EXECUTABLE must be an absolute Fedora path")
        if not bootstrap.chat_model_path.is_absolute():
            raise ValueError("PENSAE_CHAT_MODEL_PATH must be an absolute Fedora path")
        if not bootstrap.embedding_model_path.is_absolute():
            raise ValueError("PENSAE_EMBEDDING_MODEL_PATH must be an absolute Fedora path")
        return {
            ModelRole.CHAT: LaunchSpec(
                role=ModelRole.CHAT,
                executable=bootstrap.llama_executable,
                model_path=bootstrap.chat_model_path,
                port=self.chat.port,
                expected_model=self.policy.chat_model_id,
                fixed_arguments=self.chat.fixed_args,
            ),
            ModelRole.EMBEDDING: LaunchSpec(
                role=ModelRole.EMBEDDING,
                executable=bootstrap.llama_executable,
                model_path=bootstrap.embedding_model_path,
                port=self.embedding.port,
                expected_model=self.policy.embedding_model_id,
                fixed_arguments=self.embedding.fixed_args,
            ),
        }


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"protected config {key} must be a table")
    return cast(dict[str, Any], value)


def _string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"protected config {key} must be a non-empty string")
    return value


def _integer(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"protected config {key} must be an integer")
    return value


def _positive_number(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"protected config {key} must be positive")
    return float(value)


def _nonnegative_number(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"protected config {key} must be nonnegative")
    return float(value)


def _positive_integer(raw: dict[str, Any], key: str) -> int:
    value = _integer(raw, key)
    if value <= 0:
        raise ValueError(f"protected config {key} must be positive")
    return value


def _nonnegative_integer(raw: dict[str, Any], key: str) -> int:
    value = _integer(raw, key)
    if value < 0:
        raise ValueError(f"protected config {key} must be nonnegative")
    return value


def _role(raw: dict[str, Any], *, expected: ModelRole) -> ProtectedRole:
    role = ModelRole(_string(raw, "role"))
    if role is not expected:
        raise ValueError(f"protected role must be {expected.value}")
    fixed_args = raw.get("fixed_args")
    if not isinstance(fixed_args, list) or not all(
        isinstance(value, str) and value for value in fixed_args
    ):
        raise ValueError("protected fixed_args must be a non-empty string array")
    return ProtectedRole(
        role=role,
        port=_integer(raw, "port"),
        fixed_args=tuple(cast(list[str], fixed_args)),
    )
