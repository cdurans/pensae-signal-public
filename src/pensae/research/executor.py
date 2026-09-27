"""Execution boundary for the fixed research graph."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from pydantic import ValidationError

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.opportunities import OpportunityAggregateStore
from pensae.research.operations import ResearchWorkflowOperations
from pensae.research.workflow import (
    CancellationProbe,
    FixedWorkflowGraph,
    WorkflowContractFailure,
    WorkflowObserver,
    WorkflowResult,
)
from pensae.settings import SavedSettings


class ResearchWorkflowExecutor:
    """Build a fresh transient operation set for every manager-owned run."""

    def __init__(
        self,
        *,
        store: OpportunityAggregateStore,
        cancellation: CancellationProbe,
        observer_factory: Callable[[UUID], WorkflowObserver],
        operation_factory: Callable[
            [UUID, str, SavedSettings, ProtectedWorkflowBounds], ResearchWorkflowOperations
        ],
    ) -> None:
        self._store = store
        self._cancellation = cancellation
        self._observer_factory = observer_factory
        self._operation_factory = operation_factory

    async def run(self, run_id: UUID) -> WorkflowResult:
        snapshot = await self._store.get_run(run_id)
        if snapshot is None:
            raise WorkflowContractFailure("run snapshot is missing")
        industry = snapshot.effective_config.get("industry")
        if not isinstance(industry, str) or not industry:
            raise WorkflowContractFailure("run snapshot industry is invalid")
        try:
            saved_settings = SavedSettings.model_validate(
                snapshot.effective_config.get("saved_settings")
            )
            effective_bounds = ProtectedWorkflowBounds(
                **dict(snapshot.effective_config.get("bounds", {}))
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise WorkflowContractFailure("run snapshot settings are invalid") from exc
        graph = FixedWorkflowGraph(
            bounds=effective_bounds,
            operations=self._operation_factory(run_id, industry, saved_settings, effective_bounds),
            cancellation=self._cancellation,
            observer=self._observer_factory(run_id),
        )
        return await graph.run(run_id)
