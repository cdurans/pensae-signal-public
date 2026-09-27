"""Pattern synthesis, segment mapping, gating, and concept design stages."""

from __future__ import annotations

from typing import cast

from pensae.config.protected import ProtectedWorkflowBounds
from pensae.domain.opportunity import (
    PreliminaryCandidate,
    ProblemSimilarityInput,
    evaluate_preliminary_gate,
    problem_similarity_text,
)
from pensae.research.assembly import OpportunityAssembler
from pensae.research.calls import BoundedExternalCalls
from pensae.research.context import ResearchRunContext
from pensae.research.metrics import (
    add_usage,
    cosine,
    pattern_fingerprint,
    strongest_pattern_similarity,
)
from pensae.research.roles import RoleName, RoleSpec
from pensae.research.schemas import PatternSynthesis, SegmentMap, SegmentSolution
from pensae.research.state import (
    ConceptCandidate,
    PatternCandidate,
    SelectedSource,
    SynthesizedPattern,
)
from pensae.research.workflow import CounterDelta, NodeResult


class SynthesisStages:
    def __init__(
        self,
        context: ResearchRunContext,
        calls: BoundedExternalCalls,
        assembler: OpportunityAssembler,
    ) -> None:
        self.context = context
        self.calls = calls
        self.assembler = assembler

    async def synthesize_patterns(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        if len(self.context.memory.selected) < 2:
            self.context.memory.patterns = ()
            return NodeResult()
        signal_embeddings = await self.calls._embed(
            tuple(
                "\n".join(
                    (
                        item.selection.recurring_workflow,
                        item.selection.current_workaround,
                        item.selection.business_consequence,
                    )
                )
                for item in self.context.memory.selected
            ),
            bounds,
            role="signal_clustering",
        )
        spec = RoleSpec(
            role=RoleName.PROBLEM_ANALYST,
            prompt_version=self.context.protected.research.problem_analyst_prompt_version,
            output_type=PatternSynthesis,
            max_tokens=self.context.protected.research.problem_analyst_output_max_tokens,
            required=False,
            instruction=(
                "Synthesize one normalized cross-industry problem pattern from the supplied "
                "evidence-linked signals. Do not propose a solution or invent evidence."
            ),
        )
        patterns: list[SynthesizedPattern] = []
        usage = [0, 0, 0, 0]
        warnings: list[str] = []
        clusters = _supported_signal_pairs(
            self.context.memory.selected,
            signal_embeddings.vectors,
            threshold=self.context.protected.research.related_similarity_threshold,
            limit=bounds.patterns,
        )
        for cluster in clusters:
            signals = tuple(self.assembler._problem_signal(item) for item in cluster)
            result = await self.calls._model(
                spec,
                trusted_input={
                    "signals": tuple(signal.model_dump(mode="json") for signal in signals),
                    "required_fields": (
                        "pattern_summary",
                        "affected_user",
                        "recurring_workflow",
                        "current_workaround",
                        "desired_outcome",
                    ),
                },
                seed=260 + len(patterns),
                timeout_seconds=bounds.model_timeout_seconds,
            )
            usage[:] = add_usage(result, *usage)
            synthesis = result.value
            if synthesis is None:
                warnings.append("pattern_synthesis_discarded")
                continue
            fingerprint = pattern_fingerprint(
                synthesis, version=self.context.protected.research.fingerprint_version
            )
            canonical = problem_similarity_text(
                ProblemSimilarityInput(
                    problem=synthesis.pattern_summary,
                    affected_user=synthesis.affected_user,
                    current_workaround=synthesis.current_workaround,
                    desired_outcome=synthesis.desired_outcome,
                )
            )
            pattern_embedding = await self.calls._embed(
                (canonical,), bounds, role="pattern_similarity"
            )
            vector = pattern_embedding.vectors[0]
            exact_duplicate = (
                await self.context.store.find_pattern_fingerprint(fingerprint) is not None
            )
            retained_matches = await self.context.store.nearest_patterns(
                vector,
                limit=3,
                minimum_similarity=(self.context.protected.research.related_similarity_threshold),
            )
            current_similarity = max(
                (cosine(vector, item.embedding) for item in patterns), default=0.0
            )
            strongest_similarity = strongest_pattern_similarity(
                current_similarity,
                tuple(match.similarity for match in retained_matches),
            )
            current_exact = any(item.fingerprint == fingerprint for item in patterns)
            patterns.append(
                SynthesizedPattern(
                    fingerprint=fingerprint,
                    synthesis=synthesis,
                    selected=cluster,
                    embedding=vector,
                    exact_duplicate=exact_duplicate or current_exact,
                    insufficient_distinction=(
                        strongest_similarity
                        >= self.context.protected.research.possible_rediscovery_similarity_threshold
                    ),
                )
            )
        self.context.memory.patterns = tuple(patterns)
        self.calls._apply(CounterDelta(patterns=len(patterns)), bounds)
        return NodeResult(
            counters=CounterDelta(
                patterns=len(patterns),
                model_calls=usage[0] + 1 + len(patterns),
                repairs=usage[1],
                input_tokens=usage[2] + signal_embeddings.input_tokens,
                output_tokens=usage[3],
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def map_segments(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        spec = RoleSpec(
            role=RoleName.PRODUCT_STRATEGIST,
            prompt_version=self.context.protected.research.product_strategist_prompt_version,
            output_type=SegmentMap,
            max_tokens=self.context.protected.research.product_strategist_output_max_tokens,
            required=False,
            instruction=(
                "Map the validated problem pattern to plausible segments, affected users, "
                "buyers, workflow frequency/value, constraints, and reachability. Do not "
                "propose a solution."
            ),
        )
        mapped: list[PatternCandidate] = []
        usage = [0, 0, 0, 0]
        warnings: list[str] = []
        for index, pattern in enumerate(self.context.memory.patterns):
            result = await self.calls._model(
                spec,
                trusted_input={
                    "pattern": pattern.synthesis.model_dump(mode="json"),
                    "signals": tuple(
                        self.assembler._problem_signal(item).model_dump(mode="json")
                        for item in pattern.selected
                    ),
                    "maximum_segments": bounds.segments_per_pattern,
                },
                seed=280 + index,
                timeout_seconds=bounds.model_timeout_seconds,
            )
            usage[:] = add_usage(result, *usage)
            if result.value is None:
                warnings.append("segment_mapping_discarded")
                continue
            for segment in result.value.segments[: bounds.segments_per_pattern]:
                mapped.append(
                    PatternCandidate(
                        fingerprint=pattern.fingerprint,
                        candidate=PreliminaryCandidate(
                            pattern_summary=pattern.synthesis.pattern_summary,
                            target_segment=segment.target_segment,
                            likely_buyer=segment.likely_buyer,
                            signals=tuple(
                                self.assembler._problem_signal(item) for item in pattern.selected
                            ),
                            exact_duplicate=pattern.exact_duplicate,
                            insufficient_distinction=pattern.insufficient_distinction,
                        ),
                        selected=pattern.selected,
                        synthesis=pattern.synthesis,
                        segment=segment,
                        embedding=pattern.embedding,
                    )
                )
        self.context.memory.mapped = tuple(mapped)
        self.calls._apply(CounterDelta(segments=len(mapped)), bounds)
        return NodeResult(
            counters=CounterDelta(
                segments=len(mapped),
                model_calls=usage[0],
                repairs=usage[1],
                input_tokens=usage[2],
                output_tokens=usage[3],
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def apply_preliminary_gate(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        eligible = tuple(
            item
            for item in self.context.memory.mapped
            if evaluate_preliminary_gate(item.candidate).passed
        )
        survivors = tuple(sorted(eligible, key=_candidate_rank_key))[: bounds.preliminary_survivors]
        self.context.memory.survivors = survivors
        self.calls._apply(CounterDelta(preliminary_survivors=len(survivors)), bounds)
        return NodeResult(
            counters=CounterDelta(preliminary_survivors=len(survivors)),
            survivor_count=len(survivors),
            warnings=("no_gate_survivor",) if not survivors else (),
        )

    async def design_concepts(self, bounds: ProtectedWorkflowBounds) -> NodeResult:
        spec = cast(RoleSpec[SegmentSolution], self.context.specs[RoleName.PRODUCT_STRATEGIST])
        concepts: list[ConceptCandidate] = []
        usage = [0, 0, 0, 0]
        for index, pattern in enumerate(self.context.memory.survivors[: bounds.concepts]):
            result = await self.calls._model(
                spec,
                trusted_input={
                    "industry": self.context.industry,
                    "pattern": pattern.candidate.pattern_summary,
                    "target_segment": pattern.candidate.target_segment,
                    "likely_buyer": pattern.candidate.likely_buyer,
                    "preferred_technologies": (
                        self.context.saved_settings.research.preferred_technologies
                    ),
                },
                seed=300 + index,
                timeout_seconds=bounds.model_timeout_seconds,
            )
            usage[:] = add_usage(result, *usage)
            if result.value is not None:
                if (
                    result.value.target_segment != pattern.candidate.target_segment
                    or result.value.likely_buyer != pattern.candidate.likely_buyer
                ):
                    continue
                concepts.append(ConceptCandidate(pattern, result.value))
                self.calls._apply(CounterDelta(concepts=1), bounds)
        self.context.memory.concepts = tuple(concepts)
        return NodeResult(
            counters=CounterDelta(
                concepts=len(concepts),
                model_calls=usage[0],
                repairs=usage[1],
                input_tokens=usage[2],
                output_tokens=usage[3],
            ),
            warnings=("concept_design_discarded",) if not concepts else (),
            admitted_count=len(concepts),
        )


def _candidate_rank_key(item: PatternCandidate) -> tuple[object, ...]:
    """Rank stronger evidence first, with canonical stable tie breakers."""

    independent_sources = len({signal.source_id for signal in item.candidate.signals})
    total_confidence = sum(signal.confidence for signal in item.candidate.signals)
    return (
        -independent_sources,
        -total_confidence,
        item.fingerprint,
        item.candidate.target_segment.casefold(),
        item.candidate.likely_buyer.casefold(),
    )


def _supported_signal_pairs(
    selected: tuple[SelectedSource, ...],
    vectors: tuple[tuple[float, ...], ...],
    *,
    threshold: float,
    limit: int,
) -> tuple[tuple[SelectedSource, SelectedSource], ...]:
    """Form deterministic disjoint two-source clusters over real similarity edges."""

    if len(selected) != len(vectors):
        raise ValueError("signal clustering inputs must have matching lengths")
    ranked = sorted(
        range(len(selected)),
        key=lambda index: _selected_source_rank_key(selected[index]),
    )
    remaining = list(ranked)
    clusters: list[tuple[SelectedSource, SelectedSource]] = []
    while remaining and len(clusters) < limit:
        anchor = remaining.pop(0)
        candidates = [
            index
            for index in remaining
            if selected[anchor].selection.source_id != selected[index].selection.source_id
            and cosine(vectors[anchor], vectors[index]) >= threshold
        ]
        if not candidates:
            continue
        partner = min(
            candidates,
            key=lambda index: (
                -cosine(vectors[anchor], vectors[index]),
                _selected_source_rank_key(selected[index]),
            ),
        )
        remaining.remove(partner)
        clusters.append((selected[anchor], selected[partner]))
    return tuple(clusters)


def _selected_source_rank_key(item: SelectedSource) -> tuple[str, ...]:
    return (
        item.hit.canonical_url,
        item.selection.source_id,
        item.selection.recurring_workflow.casefold(),
        item.selection.current_workaround.casefold(),
        item.selection.business_consequence.casefold(),
    )
