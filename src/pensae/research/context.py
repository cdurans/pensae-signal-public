"""Run-scoped dependencies, accounting, and transient workflow state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.infrastructure.models import EmbeddingClient
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchHit
from pensae.opportunities import OpportunityAggregate, OpportunityAggregateStore
from pensae.research.ports import Retriever, Searcher
from pensae.research.roles import (
    RoleName,
    RoleSpec,
    StructuredRoleExecutor,
    build_role_specs,
)
from pensae.research.state import (
    ConceptCandidate,
    FocusedSource,
    PatternCandidate,
    SelectedSource,
    SynthesizedPattern,
)
from pensae.research.workflow import CancellationProbe, ModelCallUsage, WorkCounters
from pensae.settings import SavedSettings


@dataclass(slots=True)
class WorkflowMemory:
    """Transient values exchanged between fixed workflow stages."""

    queries: tuple[str, ...] = ()
    hits: tuple[SearchHit, ...] = ()
    retrieved: tuple[tuple[SearchHit, RetrievedSource], ...] = ()
    selected: tuple[SelectedSource, ...] = ()
    patterns: tuple[SynthesizedPattern, ...] = ()
    mapped: tuple[PatternCandidate, ...] = ()
    survivors: tuple[PatternCandidate, ...] = ()
    concepts: tuple[ConceptCandidate, ...] = ()
    focused_queries: tuple[tuple[str, ...], ...] = ()
    focused_hits: tuple[tuple[SearchHit, ...], ...] = ()
    focused: tuple[FocusedSource | None, ...] = ()
    aggregates: tuple[OpportunityAggregate, ...] = ()
    evaluated_count: int = 0
    committed_count: int = 0
    achieved_count: int = 0

    def clear(self) -> None:
        self.queries = ()
        self.hits = ()
        self.retrieved = ()
        self.selected = ()
        self.patterns = ()
        self.mapped = ()
        self.survivors = ()
        self.concepts = ()
        self.focused_queries = ()
        self.focused_hits = ()
        self.focused = ()
        self.aggregates = ()
        self.evaluated_count = 0
        self.committed_count = 0
        self.achieved_count = 0


@dataclass(slots=True)
class RunAccounting:
    """Mutable counters shared by every bounded capability."""

    counters: WorkCounters = field(default_factory=WorkCounters)
    model_usage: list[ModelCallUsage] = field(default_factory=list)
    model_calls: int = 0
    repairs: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class ResearchRunContext:
    """One run's stable dependencies plus explicitly owned mutable state."""

    run_id: UUID
    industry: str
    protected: ProtectedConfig
    search: Searcher
    retrieval: Retriever
    roles: StructuredRoleExecutor
    embeddings: EmbeddingClient
    store: OpportunityAggregateStore
    cancellation: CancellationProbe
    saved_settings: SavedSettings = field(default_factory=SavedSettings)
    effective_bounds: ProtectedWorkflowBounds | None = None
    role_output_tokens: Mapping[str, int] = field(default_factory=dict)
    id_factory: Callable[[], UUID] = uuid4
    now: Callable[[], datetime] = field(default_factory=lambda: lambda: datetime.now(UTC))
    memory: WorkflowMemory = field(default_factory=WorkflowMemory)
    accounting: RunAccounting = field(default_factory=RunAccounting)
    specs: Mapping[RoleName, RoleSpec[BaseModel]] = field(init=False)

    def __post_init__(self) -> None:
        if self.effective_bounds is None:
            self.effective_bounds = self.protected.research.bounds
        self.specs = {
            role: replace(
                spec,
                max_tokens=self.role_output_tokens.get(spec.role.value, spec.max_tokens),
            )
            for role, spec in build_role_specs(self.protected.research).items()
        }
