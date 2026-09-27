"""Focused validation and final opportunity-analysis stages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import ValidationError

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.domain.evidence import (
    TransientSpanDocument,
    construct_verified_evidence,
    create_transient_spans,
)
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchHit
from pensae.opportunities import EvidenceInput, OpportunityAggregate
from pensae.research.assembly import OpportunityAssembler
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.metrics import add_usage
from pensae.research.roles import RoleName, RoleSpec, pack_untrusted_spans
from pensae.research.schemas import FocusedEvidenceSelection, QueryPlan
from pensae.research.state import ConceptCandidate, FocusedSource
from pensae.research.workflow import (
    CounterDelta,
    NodeResult,
    WorkflowStopRequested,
    WorkLimitExceeded,
)


class ValidationStages:
    def __init__(
        self,
        context: ResearchRunContext,
        calls: BoundedExternalCalls,
        assembler: OpportunityAssembler,
    ) -> None:
        self.context = context
        self.calls = calls
        self.assembler = assembler

    async def plan(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        spec = cast(RoleSpec[QueryPlan], self.context.specs[RoleName.RESEARCH_PLANNER])
        index, concept = self._current_concept()
        usage = [0, 0, 0, 0]
        result = await self.calls._model(
            spec,
            trusted_input={
                "industry": self.context.industry,
                "country": self.context.saved_settings.research.country,
                "language": self.context.saved_settings.research.language,
                "target_segment": concept.strategy.target_segment,
                "desired_outcome": concept.strategy.desired_outcome,
                "maximum_queries": bounds.focused_queries_per_concept,
            },
            seed=400 + index,
            timeout_seconds=bounds.model_timeout_seconds,
        )
        usage[:] = add_usage(result, *usage)
        plan = (
            result.value.queries[: bounds.focused_queries_per_concept]
            if result.value is not None
            else ()
        )
        self.context.memory.focused_queries = (plan,)
        return NodeResult(
            counters=CounterDelta(
                model_calls=usage[0],
                repairs=usage[1],
                input_tokens=usage[2],
                output_tokens=usage[3],
            )
        )

    async def search(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        retries_before = self.context.accounting.counters.search_retries
        groups: list[tuple[SearchHit, ...]] = []
        queries = results = 0
        warnings: list[str] = []
        plan = self.context.memory.focused_queries[0]
        unique: dict[str, SearchHit] = {}
        for query in plan:
            batch = await self.calls._search_call(query, bounds)
            queries += 1
            results += len(batch.results)
            if batch.warnings:
                warnings.append("search_engine_warning")
            for hit in batch.results:
                unique.setdefault(hit.canonical_url, hit)
        groups.append(tuple(unique.values()))
        self.context.memory.focused_hits = tuple(groups)
        return NodeResult(
            counters=CounterDelta(
                queries=queries,
                search_results=results,
                # The frozen URL ceiling applies to pass-one discovery candidates.
                unique_urls=0,
                search_retries=(self.context.accounting.counters.search_retries - retries_before),
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def retrieve(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        spec = RoleSpec(
            role=RoleName.PROBLEM_ANALYST,
            prompt_version=self.context.protected.research.problem_analyst_prompt_version,
            output_type=FocusedEvidenceSelection,
            max_tokens=self.context.protected.research.problem_analyst_output_max_tokens,
            required=False,
            instruction=(
                "Select contiguous source span IDs supporting focused demand, alternatives, "
                "pricing, negative evidence, or conflicting evidence."
            ),
        )
        pages = byte_count = 0
        usage = [0, 0, 0, 0]
        warnings: list[str] = []
        concept_index, concept = self._current_concept()
        hits = self.context.memory.focused_hits[0]
        chosen: FocusedSource | None = None
        retrieved_candidates: list[
            tuple[
                SearchHit,
                RetrievedSource,
                str,
                TransientSpanDocument,
                tuple[Mapping[str, object], ...],
            ]
        ] = []
        for page_index, hit in enumerate(hits[: bounds.focused_pages_per_concept]):
            try:
                retrieved = await self.calls._retrieval_call(hit.canonical_url, bounds)
            except Exception as exc:
                if isinstance(exc, (WorkflowStopRequested, WorkLimitExceeded)):
                    raise
                warnings.append("source_unavailable")
                continue
            pages += 1
            byte_count += retrieved.response_bytes
            key = f"focused_{concept_index + 1}_{page_index + 1}"
            document = create_transient_spans(
                source_id=key, extracted_text=retrieved.extracted_text
            )
            untrusted = tuple(
                {"source_id": key, "span_id": span.span_id, "text": span.text}
                for span in document.spans
            )
            retrieved_candidates.append((hit, retrieved, key, document, untrusted))

        if retrieved_candidates:
            packed = pack_untrusted_spans(
                tuple((item[2], item[4]) for item in retrieved_candidates),
                prompt_input_max_tokens=(self.context.protected.research.prompt_input_max_tokens),
            )
            if packed.truncated:
                warnings.append("focused_evidence_prompt_truncated")
            if not packed.spans:
                warnings.append("focused_evidence_prompt_overflow")
                self.context.memory.focused = (None,)
                return NodeResult(
                    counters=CounterDelta(
                        retrieved_pages=pages,
                        retrieved_bytes=byte_count,
                    ),
                    warnings=tuple(dict.fromkeys(warnings)),
                )
            result = await self.calls._model(
                spec,
                trusted_input={
                    "allowed_source_ids": packed.source_ids,
                    "allowed_source_spans": tuple(
                        {
                            "source_id": source_id,
                            "span_ids": tuple(
                                cast(str, span["span_id"])
                                for span in packed.spans
                                if span["source_id"] == source_id
                            ),
                        }
                        for source_id in packed.source_ids
                    ),
                    "target_segment": concept.strategy.target_segment,
                    "desired_outcome": concept.strategy.desired_outcome,
                },
                untrusted=packed.spans,
                seed=500 + concept_index,
                timeout_seconds=bounds.model_timeout_seconds,
            )
            usage[:] = add_usage(result, *usage)
            value = result.value
            selected = (
                next(
                    (
                        item
                        for item in retrieved_candidates
                        if item[2] in packed.source_ids and item[2] == value.source_id
                    ),
                    None,
                )
                if value is not None
                else None
            )
            if value is not None and selected is None:
                warnings.append("evidence_integrity_failure")
            elif value is not None and selected is not None:
                hit, retrieved, selected_key, document, _selected_untrusted = selected
                packed_selected_spans = tuple(
                    span for span in packed.spans if span["source_id"] == selected_key
                )
                packed_span_ids = {cast(str, span["span_id"]) for span in packed_selected_spans}
                try:
                    if any(span_id not in packed_span_ids for span_id in value.span_ids):
                        raise ValueError("selected evidence span was not included in the prompt")
                    source_id = self.context.id_factory()
                    evidence = tuple(
                        EvidenceInput(
                            id=self.context.id_factory(),
                            source_id=source_id,
                            excerpt=construct_verified_evidence(
                                document,
                                selected_span_ids=(span_id,),
                                supported_claim=value.supported_claim,
                            ).excerpt,
                            supported_claim=value.supported_claim,
                            evidence_kind=cast(
                                Literal["supporting", "negative", "conflicting"],
                                value.evidence_kind,
                            ),
                            material_conflict=(
                                "Focused source conflicts with the discovery evidence."
                                if value.evidence_kind == "conflicting"
                                else None
                            ),
                        )
                        for span_id in value.span_ids
                    )
                except ValueError:
                    warnings.append("evidence_integrity_failure")
                else:
                    chosen = FocusedSource(
                        source_id, hit, retrieved, evidence, packed_selected_spans
                    )
        self.context.memory.focused = (chosen,)
        return NodeResult(
            counters=CounterDelta(
                retrieved_pages=pages,
                retrieved_bytes=byte_count,
                model_calls=usage[0],
                repairs=usage[1],
                input_tokens=usage[2],
                output_tokens=usage[3],
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def analyze(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        index, concept = self._current_concept()
        focused = self.context.memory.focused[0] if self.context.memory.focused else None
        usage = [0, 0, 0, 0]
        warnings: list[str] = []
        aggregate: OpportunityAggregate | None = None
        outcome: Literal["invalid_candidate", "incomplete_candidate"] | None = None
        if focused is None:
            warnings.extend(("focused_evidence_unavailable", "final_candidates_unavailable"))
            outcome = "incomplete_candidate"
        else:
            try:
                aggregate, result, embedding_tokens = await self.assembler._build_aggregate(
                    concept, focused, seed=600 + index, bounds=bounds
                )
                usage[:] = add_usage(result, *usage)
                if aggregate is not None:
                    usage[0] += 1
                    usage[2] += embedding_tokens
                else:
                    warnings.append("final_candidate_discarded")
                    outcome = "invalid_candidate"
            except (ValidationError, ValueError):
                warnings.append("final_candidate_discarded")
                outcome = "invalid_candidate"
        self.context.memory.aggregates = (aggregate,) if aggregate is not None else ()
        self.context.memory.evaluated_count = index + 1
        if aggregate is None and "final_candidates_unavailable" not in warnings:
            warnings.append("final_candidates_unavailable")
        return NodeResult(
            counters=CounterDelta(
                model_calls=usage[0],
                repairs=usage[1],
                input_tokens=usage[2],
                output_tokens=usage[3],
            ),
            warnings=tuple(dict.fromkeys(warnings)),
            evaluated_count=self.context.memory.evaluated_count,
            candidate_ready=aggregate is not None,
            candidate_outcome=outcome,
        )

    def _current_concept(self) -> tuple[int, ConceptCandidate]:
        index = self.context.memory.evaluated_count
        if not 0 <= index < len(self.context.memory.concepts):
            raise ValueError("sequential candidate index is outside the admitted pool")
        return index, self.context.memory.concepts[index]
