"""Similarity classification, atomic persistence, and transient cleanup stages."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, cast

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.domain.opportunity import classify_initial_candidate
from pensae.opportunities import OpportunityAggregate, SimilarityRelationInput
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.workflow import (
    CandidateOutcome,
    CounterDelta,
    NodeResult,
    WorkflowContractFailure,
    WorkflowPartialCommitFailure,
    WorkflowStopRequested,
)


class CommitStages:
    def __init__(self, context: ResearchRunContext, calls: BoundedExternalCalls) -> None:
        self.context = context
        self.calls = calls

    async def classify_and_commit(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        if len(self.context.memory.aggregates) != 1:
            raise WorkflowContractFailure(
                "sequential commit requires exactly one complete candidate"
            )
        aggregate = self.context.memory.aggregates[0]
        outcome: CandidateOutcome | None = None
        try:
            await self.calls._checkpoint()
            exact = await self.context.store.find_identity(aggregate.identity_fingerprint)
            if exact is not None:
                await self.context.store.record_rediscovery(
                    run_id=self.context.run_id,
                    opportunity_id=exact,
                    lifecycle_event_id=self.context.id_factory(),
                )
                outcome = "automatic_exact_rediscovery"
                await self.calls._checkpoint()
            else:
                matches = await self.context.store.nearest_problems(
                    aggregate.opportunity_embedding,
                    limit=3,
                    minimum_similarity=(
                        self.context.protected.research.related_similarity_threshold
                    ),
                )
                strongest = matches[0].similarity if matches else None
                classification = classify_initial_candidate(
                    exact_identity_match=False,
                    strongest_similarity=strongest,
                    related_threshold=(
                        self.context.protected.research.related_similarity_threshold
                    ),
                    possible_rediscovery_threshold=(
                        self.context.protected.research.possible_rediscovery_similarity_threshold
                    ),
                )
                relations = tuple(
                    SimilarityRelationInput(
                        id=self.context.id_factory(),
                        opportunity_id=match.opportunity_id,
                        similarity=Decimal(str(match.similarity)),
                        relation_kind=cast(
                            Literal["related", "possible_rediscovery"],
                            classify_initial_candidate(
                                exact_identity_match=False,
                                strongest_similarity=match.similarity,
                                related_threshold=(
                                    self.context.protected.research.related_similarity_threshold
                                ),
                                possible_rediscovery_threshold=(
                                    self.context.protected.research.possible_rediscovery_similarity_threshold
                                ),
                            ).value,
                        ),
                    )
                    for match in matches
                )
                data = aggregate.model_dump(mode="python")
                data.update(
                    classification=classification.value,
                    relations=relations,
                    relation_id=None,
                    related_opportunity_id=None,
                    relation_similarity=None,
                )
                await self.calls._checkpoint()
                # Admission is checked before opening the writer's short transaction.
                self.context.accounting.counters.increment(CounterDelta(opportunities=1), bounds)
                await self.context.store.commit(OpportunityAggregate.model_validate(data))
                self.calls._apply(CounterDelta(opportunities=1), bounds)
                self.context.memory.committed_count += 1
                if classification.value in {"new", "related"}:
                    self.context.memory.achieved_count += 1
                    outcome = cast(CandidateOutcome, classification.value)
                else:
                    outcome = "unresolved_possible_rediscovery"
                await self.calls._checkpoint()
        except WorkflowStopRequested:
            return NodeResult(
                committed_count=self.context.memory.committed_count,
                achieved_count=self.context.memory.achieved_count,
                candidate_outcome=outcome,
                warnings=("stop_requested",),
            )
        except Exception as exc:
            if self.context.memory.committed_count:
                raise WorkflowPartialCommitFailure(self.context.memory.committed_count) from exc
            raise
        return NodeResult(
            committed_count=self.context.memory.committed_count,
            achieved_count=self.context.memory.achieved_count,
            candidate_outcome=outcome,
            warnings=("exact_rediscovery",) if outcome == "automatic_exact_rediscovery" else (),
        )

    async def cleanup(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        del bounds
        self.context.memory.clear()
        return NodeResult()
