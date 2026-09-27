"""Discovery planning, search, retrieval, and signal extraction stages."""

from __future__ import annotations

from typing import cast

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.domain.evidence import construct_verified_evidence, create_transient_spans
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchHit
from pensae.opportunities import EvidenceInput
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.metrics import add_usage, model_delta
from pensae.research.roles import (
    DISCOVERY_ANALYST_HEADROOM_TOKENS,
    RoleName,
    RoleSpec,
    pack_untrusted_spans,
)
from pensae.research.schemas import QueryPlan, SignalSelection
from pensae.research.state import SelectedSource
from pensae.research.workflow import (
    CounterDelta,
    NodeResult,
    WorkflowContractFailure,
    WorkflowStopRequested,
    WorkLimitExceeded,
)


class DiscoveryStages:
    def __init__(self, context: ResearchRunContext, calls: BoundedExternalCalls) -> None:
        self.context = context
        self.calls = calls

    async def plan(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        spec = cast(RoleSpec[QueryPlan], self.context.specs[RoleName.RESEARCH_PLANNER])
        result = await self.calls._model(
            spec,
            trusted_input={
                "industry": self.context.industry,
                "country": self.context.saved_settings.research.country,
                "language": self.context.saved_settings.research.language,
                "discovery_mode": self.context.saved_settings.research.discovery_mode.value,
                "research_focus": self.context.saved_settings.research.focus,
                "maximum_queries": bounds.discovery_queries,
            },
            seed=100,
            timeout_seconds=bounds.model_timeout_seconds,
        )
        if result.value is None:
            raise WorkflowContractFailure("required discovery plan is absent")
        self.context.memory.queries = result.value.queries[: bounds.discovery_queries]
        return NodeResult(counters=model_delta(result))

    async def search(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        retries_before = self.context.accounting.counters.search_retries
        unique: dict[str, SearchHit] = {}
        query_count = 0
        raw_count = 0
        warnings: list[str] = []
        for query in self.context.memory.queries:
            if len(unique) >= bounds.unique_urls:
                break
            batch = await self.calls._search_call(query, bounds)
            query_count += 1
            raw_count += len(batch.results)
            if batch.warnings:
                warnings.append("search_engine_warning")
            for hit in batch.results:
                if len(unique) >= bounds.unique_urls:
                    break
                unique.setdefault(hit.canonical_url, hit)
        self.context.memory.hits = tuple(unique.values())
        self.calls._apply(CounterDelta(unique_urls=len(self.context.memory.hits)), bounds)
        return NodeResult(
            counters=CounterDelta(
                queries=query_count,
                search_results=raw_count,
                unique_urls=len(self.context.memory.hits),
                search_retries=(self.context.accounting.counters.search_retries - retries_before),
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def retrieve(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        selected: list[tuple[SearchHit, RetrievedSource]] = []
        warnings: list[str] = []
        for hit in self.context.memory.hits[: bounds.first_pass_pages]:
            try:
                source = await self.calls._retrieval_call(hit.canonical_url, bounds)
            except Exception as exc:
                if isinstance(exc, (WorkflowStopRequested, WorkLimitExceeded)):
                    raise
                warnings.append("source_unavailable")
                continue
            selected.append((hit, source))
        self.context.memory.retrieved = tuple(selected)
        return NodeResult(
            counters=CounterDelta(
                retrieved_pages=len(selected),
                retrieved_bytes=sum(item.response_bytes for _, item in selected),
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def extract_signals(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        spec = cast(RoleSpec[SignalSelection], self.context.specs[RoleName.PROBLEM_ANALYST])
        selected: list[SelectedSource] = []
        calls = repairs = input_tokens = output_tokens = 0
        warnings: list[str] = []
        for index, (hit, retrieved) in enumerate(self.context.memory.retrieved):
            if len(selected) == bounds.signals:
                break
            key = f"discovery_{index + 1}"
            document = create_transient_spans(
                source_id=key, extracted_text=retrieved.extracted_text
            )
            packed = pack_untrusted_spans(
                (
                    (
                        key,
                        tuple(
                            {"source_id": key, "span_id": span.span_id, "text": span.text}
                            for span in document.spans
                        ),
                    ),
                ),
                prompt_input_max_tokens=(self.context.protected.research.prompt_input_max_tokens),
                trusted_headroom_tokens=DISCOVERY_ANALYST_HEADROOM_TOKENS,
            )
            if not packed.spans:
                warnings.append("signal_discarded")
                continue
            result = await self.calls._model(
                spec,
                trusted_input={"source_id": key},
                untrusted=packed.spans,
                seed=200 + index,
                timeout_seconds=bounds.model_timeout_seconds,
            )
            calls, repairs, input_tokens, output_tokens = add_usage(
                result, calls, repairs, input_tokens, output_tokens
            )
            value = result.value
            if value is None or value.source_id != key:
                warnings.append("signal_discarded")
                continue
            try:
                packed_span_ids = {str(span["span_id"]) for span in packed.spans}
                if any(span_id not in packed_span_ids for span_id in value.span_ids):
                    raise ValueError("selected discovery span was not included in the prompt")
                source_id = self.context.id_factory()
                items = tuple(
                    EvidenceInput(
                        id=self.context.id_factory(),
                        source_id=source_id,
                        excerpt=construct_verified_evidence(
                            document,
                            selected_span_ids=(span_id,),
                            supported_claim=value.business_consequence,
                        ).excerpt,
                        supported_claim=value.business_consequence,
                        evidence_kind="supporting",
                    )
                    for span_id in value.span_ids
                )
            except ValueError:
                warnings.append("evidence_integrity_failure")
                continue
            selected.append(SelectedSource(source_id, hit, retrieved, value, items))
            self.calls._apply(CounterDelta(signals=1), bounds)
        self.context.memory.selected = tuple(selected)
        return NodeResult(
            counters=CounterDelta(
                signals=len(selected),
                model_calls=calls,
                repairs=repairs,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )
