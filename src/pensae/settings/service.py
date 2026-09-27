"""Pure saved-settings policy behind driver-owned persistence and API seams."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pensae.config.protected import ProtectedConfig
from pensae.settings.models import (
    DurableSettingsValue,
    FutureRunSettingsSnapshot,
    LocalEndpointSettings,
    LoggingSettings,
    ResearchSettings,
    ResetSettingsRequest,
    SavedSettings,
    SettingsFieldMetadata,
    WorkflowSettings,
)

BROWSER_EDITABLE_FIELD_PATHS = frozenset(
    {
        "research.focus",
        "research.country",
        "research.language",
        "research.discovery_mode",
        "research.preferred_technologies",
        "endpoints.searxng_url",
        "endpoints.chat_url",
        "endpoints.embedding_url",
        "workflow.discovery_queries",
        "workflow.results_per_query",
        "workflow.unique_urls",
        "workflow.first_pass_pages",
        "workflow.run_queries",
        "workflow.run_pages",
        "workflow.signals",
        "workflow.patterns",
        "workflow.segments_per_pattern",
        "workflow.preliminary_survivors",
        "workflow.concepts",
        "workflow.focused_queries_per_concept",
        "workflow.focused_pages_per_concept",
        "workflow.search_retries",
        "workflow.model_calls",
        "workflow.total_run_tokens",
        "workflow.search_timeout_seconds",
        "workflow.model_timeout_seconds",
        "workflow.embedding_timeout_seconds",
        "workflow.planner_output_max_tokens",
        "workflow.problem_analyst_output_max_tokens",
        "workflow.product_strategist_output_max_tokens",
        "workflow.opportunity_analyst_output_max_tokens",
        "logging.rotation_size_mib",
        "logging.retained_files",
    }
)


@dataclass(frozen=True, slots=True)
class ProtectedSettingsBaseline:
    values: SavedSettings
    field_metadata: tuple[SettingsFieldMetadata, ...]


def build_protected_settings_baseline(
    protected: ProtectedConfig | None = None,
) -> ProtectedSettingsBaseline:
    """Copy the shipped protected defaults into an immutable saved-settings baseline."""

    resolved = protected or ProtectedConfig.load()
    policy = resolved.research
    bounds = policy.bounds
    values = SavedSettings(
        research=ResearchSettings(),
        endpoints=LocalEndpointSettings(),
        workflow=WorkflowSettings(
            discovery_queries=bounds.discovery_queries,
            results_per_query=bounds.results_per_query,
            unique_urls=bounds.unique_urls,
            first_pass_pages=bounds.first_pass_pages,
            run_queries=bounds.run_queries,
            run_pages=bounds.run_pages,
            signals=bounds.signals,
            patterns=bounds.patterns,
            segments_per_pattern=bounds.segments_per_pattern,
            preliminary_survivors=bounds.preliminary_survivors,
            concepts=bounds.concepts,
            focused_queries_per_concept=bounds.focused_queries_per_concept,
            focused_pages_per_concept=bounds.focused_pages_per_concept,
            opportunities=5,
            search_retries=bounds.search_retries,
            model_calls=bounds.model_calls,
            total_run_tokens=bounds.total_run_tokens,
            search_timeout_seconds=bounds.search_timeout_seconds,
            model_timeout_seconds=bounds.model_timeout_seconds,
            embedding_timeout_seconds=bounds.embedding_timeout_seconds,
            planner_output_max_tokens=policy.planner_output_max_tokens,
            problem_analyst_output_max_tokens=policy.problem_analyst_output_max_tokens,
            product_strategist_output_max_tokens=policy.product_strategist_output_max_tokens,
            opportunity_analyst_output_max_tokens=(policy.opportunity_analyst_output_max_tokens),
        ),
        logging=LoggingSettings(),
    )
    return ProtectedSettingsBaseline(values=values, field_metadata=_field_metadata(values))


class SavedSettingsService:
    """Create new revisions and isolated future-run copies; never mutate inputs."""

    def __init__(self, baseline: ProtectedSettingsBaseline | None = None) -> None:
        self._baseline = baseline or build_protected_settings_baseline()

    @property
    def baseline(self) -> ProtectedSettingsBaseline:
        return self._baseline

    def initial_value(self) -> DurableSettingsValue:
        return DurableSettingsValue(
            revision=1,
            values=self._baseline.values.model_copy(deep=True),
        )

    def save(
        self,
        current: DurableSettingsValue,
        candidate: SavedSettings | Mapping[str, object],
    ) -> DurableSettingsValue:
        raw_candidate = (
            candidate.model_dump(mode="python")
            if isinstance(candidate, SavedSettings)
            else candidate
        )
        values = SavedSettings.model_validate(raw_candidate)
        return DurableSettingsValue(
            revision=current.revision + 1,
            values=values.model_copy(deep=True),
        )

    def reset(
        self,
        current: DurableSettingsValue,
        request: ResetSettingsRequest | Mapping[str, object],
    ) -> DurableSettingsValue:
        ResetSettingsRequest.model_validate(request)
        return DurableSettingsValue(
            revision=current.revision + 1,
            values=self._baseline.values.model_copy(deep=True),
        )

    def snapshot_for_future_run(self, current: DurableSettingsValue) -> FutureRunSettingsSnapshot:
        return FutureRunSettingsSnapshot(
            settings_revision=current.revision,
            values=current.values.model_copy(deep=True),
        )


def _field_metadata(defaults: SavedSettings) -> tuple[SettingsFieldMetadata, ...]:
    default_values = _flatten_settings(defaults)
    specifications: dict[str, dict[str, object]] = {
        "research.focus": _text_spec(
            "Research focus",
            "Optional plain-language focus, up to 2,000 characters.",
        ),
        "research.country": _choice_spec(
            "Country or region", ("United States",), "United States research scope."
        ),
        "research.language": _choice_spec("Language", ("English",), "English-language research."),
        "research.discovery_mode": _choice_spec(
            "Discovery mode",
            ("broad", "directed"),
            "Broad discovery; directed mode requires a research focus.",
        ),
        "research.preferred_technologies": _text_spec(
            "Preferred technologies",
            "One to 12 unique values, each at most 80 characters; they influence feasibility only.",
        ),
        "endpoints.searxng_url": _endpoint_spec("SearXNG endpoint"),
        "endpoints.chat_url": _endpoint_spec("Chat model endpoint"),
        "endpoints.embedding_url": _endpoint_spec("Embedding model endpoint"),
    }
    numeric_labels = {
        "workflow.discovery_queries": "Discovery queries",
        "workflow.results_per_query": "Results per query",
        "workflow.unique_urls": "Unique source candidates",
        "workflow.first_pass_pages": "First-pass retrieved pages",
        "workflow.run_queries": "Total run queries",
        "workflow.run_pages": "Total run retrieved pages",
        "workflow.signals": "Problem signals",
        "workflow.patterns": "Problem patterns",
        "workflow.segments_per_pattern": "Segments per pattern",
        "workflow.preliminary_survivors": "Preliminary survivors",
        "workflow.concepts": "Solution concepts",
        "workflow.focused_queries_per_concept": "Focused queries per concept",
        "workflow.focused_pages_per_concept": "Focused pages per concept",
        "workflow.search_retries": "Search retries",
        "workflow.model_calls": "Total model calls",
        "workflow.total_run_tokens": "Total run tokens",
        "workflow.search_timeout_seconds": "Search timeout (seconds)",
        "workflow.model_timeout_seconds": "Model timeout (seconds)",
        "workflow.embedding_timeout_seconds": "Embedding timeout (seconds)",
        "workflow.planner_output_max_tokens": "Planner output tokens",
        "workflow.problem_analyst_output_max_tokens": "Problem Analyst output tokens",
        "workflow.product_strategist_output_max_tokens": "Product Strategist output tokens",
        "workflow.opportunity_analyst_output_max_tokens": "Opportunity Analyst output tokens",
        "logging.rotation_size_mib": "Log rotation size (MiB)",
        "logging.retained_files": "Retained log files",
    }
    for key, label in numeric_labels.items():
        minimum, maximum = _numeric_bounds(key)
        specifications[key] = {
            "label": label,
            "validation": f"Enter a whole number from {minimum:,} to {maximum:,}.",
            "minimum": minimum,
            "maximum": maximum,
        }
    if set(specifications) != BROWSER_EDITABLE_FIELD_PATHS:
        raise RuntimeError("saved-settings metadata does not match the browser allowlist")
    return tuple(
        SettingsFieldMetadata(
            key=key,
            label=str(specifications[key]["label"]),
            validation=str(specifications[key]["validation"]),
            protected_default_explanation=(
                str(specifications[key].get("default_explanation"))
                if specifications[key].get("default_explanation") is not None
                else f"Protected shipped default: {_display(default_values[key])}."
            ),
            minimum=_optional_int(specifications[key].get("minimum")),
            maximum=_optional_int(specifications[key].get("maximum")),
            choices=_choices(specifications[key].get("choices")),
        )
        for key in sorted(specifications)
    )


def _flatten_settings(settings: SavedSettings) -> dict[str, object]:
    raw = settings.model_dump(mode="python")
    return {
        f"{section}.{field}": value
        for section, section_values in raw.items()
        for field, value in section_values.items()
    }


def _numeric_bounds(key: str) -> tuple[int, int]:
    section, field = key.split(".", 1)
    model = {
        "workflow": WorkflowSettings,
        "logging": LoggingSettings,
    }[section]
    metadata = model.model_fields[field].metadata
    minimum = next(int(item.ge) for item in metadata if getattr(item, "ge", None) is not None)
    maximum = next(int(item.le) for item in metadata if getattr(item, "le", None) is not None)
    return minimum, maximum


def _text_spec(label: str, validation: str) -> dict[str, object]:
    return {"label": label, "validation": validation}


def _choice_spec(label: str, choices: tuple[str, ...], explanation: str) -> dict[str, object]:
    return {
        "label": label,
        "validation": f"Choose one of: {', '.join(choices)}.",
        "choices": choices,
        "default_explanation": explanation,
    }


def _endpoint_spec(label: str) -> dict[str, object]:
    return {
        "label": label,
        "validation": (
            "Enter an HTTP URL with an explicit port on an IPv4 loopback host; "
            "credentials, queries, and fragments are not allowed."
        ),
    }


def _display(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("metadata bounds must be integers")
    return value


def _choices(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise TypeError("metadata choices must be a string tuple")
    return value


__all__ = [
    "BROWSER_EDITABLE_FIELD_PATHS",
    "ProtectedSettingsBaseline",
    "SavedSettingsService",
    "build_protected_settings_baseline",
]
