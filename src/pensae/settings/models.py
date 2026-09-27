"""Typed, browser-safe saved settings and future-run snapshots."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pensae.research.budget import validate_saved_workflow_budget

RESET_CONFIRMATION = "RESET TO PROTECTED DEFAULTS"


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value))).strip()
    return normalized or None


def _normalize_loopback_http_url(value: object) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme != "http":
        raise ValueError("endpoint must use http")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint must not contain credentials")
    if parsed.hostname is None:
        raise ValueError("endpoint must include a host")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("endpoint host must be an IPv4 loopback address") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("endpoint host must be an IPv4 loopback address")
    if parsed.port is None:
        raise ValueError("endpoint must include an explicit port")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


NormalizedOptionalText = Annotated[str | None, BeforeValidator(_normalize_optional_text)]
LoopbackHttpUrl = Annotated[str, BeforeValidator(_normalize_loopback_http_url)]


class DiscoveryMode(StrEnum):
    BROAD = "broad"
    DIRECTED = "directed"


class ResearchSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    focus: NormalizedOptionalText = Field(default=None, max_length=2_000)
    country: Literal["United States"] = "United States"
    language: Literal["English"] = "English"
    discovery_mode: DiscoveryMode = DiscoveryMode.BROAD
    preferred_technologies: tuple[str, ...] = Field(
        default=("Python", "TypeScript", "C++"), min_length=1, max_length=12
    )

    @field_validator("preferred_technologies", mode="before")
    @classmethod
    def normalize_preferred_technologies(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("preferred technologies must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            technology = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(item))).strip()
            if not technology:
                raise ValueError("preferred technologies cannot contain blank values")
            if len(technology) > 80:
                raise ValueError("each preferred technology must be at most 80 characters")
            identity = technology.casefold()
            if identity not in seen:
                seen.add(identity)
                normalized.append(technology)
        if not normalized:
            raise ValueError("at least one preferred technology is required")
        return tuple(normalized)

    @model_validator(mode="after")
    def require_focus_for_directed_mode(self) -> ResearchSettings:
        if self.discovery_mode is DiscoveryMode.DIRECTED and self.focus is None:
            raise ValueError("directed discovery requires a research focus")
        return self


class LocalEndpointSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    searxng_url: LoopbackHttpUrl = "http://127.0.0.1:8888"
    chat_url: LoopbackHttpUrl = "http://127.0.0.1:8085"
    embedding_url: LoopbackHttpUrl = "http://127.0.0.1:8086"


class WorkflowSettings(BaseModel):
    """Operator-adjustable work limits bounded by protected ceilings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery_queries: int = Field(default=8, ge=1, le=20)
    results_per_query: int = Field(default=10, ge=1, le=10)
    unique_urls: int = Field(default=60, ge=1, le=60)
    first_pass_pages: int = Field(default=24, ge=1, le=48)
    run_queries: int = Field(default=40, ge=1, le=40)
    run_pages: int = Field(default=96, ge=1, le=96)
    signals: int = Field(default=30, ge=1, le=30)
    patterns: int = Field(default=10, ge=1, le=10)
    segments_per_pattern: int = Field(default=3, ge=1, le=3)
    preliminary_survivors: int = Field(default=12, ge=1, le=12)
    concepts: int = Field(default=8, ge=1, le=8)
    focused_queries_per_concept: int = Field(default=3, ge=1, le=3)
    focused_pages_per_concept: int = Field(default=6, ge=1, le=6)
    opportunities: Literal[5] = 5
    search_retries: int = Field(default=1, ge=0, le=1)
    model_calls: int = Field(default=160, ge=92, le=256)
    total_run_tokens: int = Field(default=1_500_000, ge=1_120_256, le=1_500_000)
    search_timeout_seconds: int = Field(default=30, ge=1, le=30)
    model_timeout_seconds: int = Field(default=120, ge=1, le=120)
    embedding_timeout_seconds: int = Field(default=60, ge=1, le=60)
    planner_output_max_tokens: int = Field(default=2_048, ge=1, le=2_048)
    problem_analyst_output_max_tokens: int = Field(default=4_096, ge=1, le=4_096)
    product_strategist_output_max_tokens: int = Field(default=4_096, ge=1, le=4_096)
    opportunity_analyst_output_max_tokens: int = Field(default=6_144, ge=1, le=6_144)

    @model_validator(mode="after")
    def validate_nested_bounds(self) -> WorkflowSettings:
        if self.discovery_queries > self.run_queries:
            raise ValueError("discovery queries cannot exceed total run queries")
        if self.first_pass_pages > self.run_pages:
            raise ValueError("first-pass pages cannot exceed total run pages")
        validate_saved_workflow_budget(self)
        return self


class LoggingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rotation_size_mib: int = Field(default=10, ge=1, le=10)
    retained_files: int = Field(default=5, ge=1, le=5)


class SavedSettings(BaseModel):
    """Complete durable operator value; unknown or protected fields fail closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research: ResearchSettings = Field(default_factory=ResearchSettings)
    endpoints: LocalEndpointSettings = Field(default_factory=LocalEndpointSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


class DurableSettingsValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=1)
    values: SavedSettings


class FutureRunSettingsSnapshot(BaseModel):
    """Deeply immutable settings copied when a future run is created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    settings_revision: int = Field(ge=1)
    values: SavedSettings


class ResetSettingsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["RESET TO PROTECTED DEFAULTS"]


class SettingsFieldMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    validation: str = Field(min_length=1)
    protected_default_explanation: str = Field(min_length=1)
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
