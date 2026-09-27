"""Composed dispatcher for the fixed bounded research workflow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.infrastructure.models import EmbeddingClient
from pensae.opportunities import OpportunityAggregateStore
from pensae.research.assembly import OpportunityAssembler
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.metrics import strongest_pattern_similarity
from pensae.research.ports import Retriever, Searcher
from pensae.research.roles import StructuredRoleExecutor
from pensae.research.stages.commit import CommitStages
from pensae.research.stages.discovery import DiscoveryStages
from pensae.research.stages.synthesis import SynthesisStages
from pensae.research.stages.validation import ValidationStages
from pensae.research.workflow import (
    CancellationProbe,
    ModelCallUsage,
    NodeResult,
    WorkCounters,
    WorkflowNode,
)
from pensae.settings import SavedSettings

_strongest_pattern_similarity = strongest_pattern_similarity


class ResearchWorkflowOperations:
    """Dispatch fixed workflow nodes to composed, run-scoped capabilities."""

    def __init__(
        self,
        *,
        run_id: UUID,
        industry: str,
        protected: ProtectedConfig,
        search: Searcher,
        retrieval: Retriever,
        roles: StructuredRoleExecutor,
        embeddings: EmbeddingClient,
        store: OpportunityAggregateStore,
        cancellation: CancellationProbe,
        saved_settings: SavedSettings | None = None,
        effective_bounds: ProtectedWorkflowBounds | None = None,
        role_output_tokens: Mapping[str, int] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.context = ResearchRunContext(
            run_id=run_id,
            industry=industry,
            protected=protected,
            search=search,
            retrieval=retrieval,
            roles=roles,
            embeddings=embeddings,
            store=store,
            cancellation=cancellation,
            saved_settings=saved_settings or SavedSettings(),
            effective_bounds=effective_bounds,
            role_output_tokens=role_output_tokens or {},
            id_factory=id_factory,
            now=now or (lambda: datetime.now(UTC)),
        )
        self.calls = BoundedExternalCalls(self.context)
        self._finalization_active = False
        assembler = OpportunityAssembler(self.context, self.calls)
        discovery = DiscoveryStages(self.context, self.calls)
        synthesis = SynthesisStages(self.context, self.calls, assembler)
        validation = ValidationStages(self.context, self.calls, assembler)
        commit = CommitStages(self.context, self.calls)
        self._handlers = {
            WorkflowNode.DISCOVERY_PLAN: discovery.plan,
            WorkflowNode.DISCOVERY_SEARCH: discovery.search,
            WorkflowNode.DISCOVERY_RETRIEVAL: discovery.retrieve,
            WorkflowNode.SIGNAL_EXTRACTION: discovery.extract_signals,
            WorkflowNode.PATTERN_SYNTHESIS: synthesis.synthesize_patterns,
            WorkflowNode.SEGMENT_MAPPING: synthesis.map_segments,
            WorkflowNode.PRELIMINARY_GATE: synthesis.apply_preliminary_gate,
            WorkflowNode.CONCEPT_DESIGN: synthesis.design_concepts,
            WorkflowNode.FOCUSED_PLAN: validation.plan,
            WorkflowNode.FOCUSED_SEARCH: validation.search,
            WorkflowNode.FOCUSED_RETRIEVAL: validation.retrieve,
            WorkflowNode.FINAL_ANALYSIS: validation.analyze,
            WorkflowNode.SIMILARITY_COMMIT: commit.classify_and_commit,
            WorkflowNode.TERMINAL_CLEANUP: commit.cleanup,
        }

    def accounting_snapshot(self) -> WorkCounters:
        return self.context.accounting.counters

    def model_usage_snapshot(self) -> tuple[ModelCallUsage, ...]:
        return tuple(self.context.accounting.model_usage)

    async def run_node(
        self,
        node: WorkflowNode,
        state: Mapping[str, object],
        bounds: ProtectedWorkflowBounds,
    ) -> NodeResult:
        del state
        self.calls.set_budget_stage(node.value)
        if node is WorkflowNode.DISCOVERY_PLAN:
            self.calls.admit_upstream(stage=node.value)
        if node is WorkflowNode.FOCUSED_PLAN:
            self.calls.begin_finalization(
                achieved_count=self.context.memory.achieved_count,
                admitted_count=len(self.context.memory.concepts),
                evaluated_count=self.context.memory.evaluated_count,
                stage=node.value,
            )
            self._finalization_active = True
        usage_start = len(self.context.accounting.model_usage)
        result = await self._handlers[node](bounds)
        if self._finalization_active and (
            (node is WorkflowNode.FINAL_ANALYSIS and not result.candidate_ready)
            or node is WorkflowNode.SIMILARITY_COMMIT
        ):
            self.calls.finish_finalization(
                achieved_count=self.context.memory.achieved_count,
                admitted_count=len(self.context.memory.concepts),
                evaluated_count=self.context.memory.evaluated_count,
            )
            self._finalization_active = False
        return replace(
            result,
            model_usage=tuple(self.context.accounting.model_usage[usage_start:]),
        )
