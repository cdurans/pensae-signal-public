"""Transient in-memory state for one research workflow run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pensae.domain.opportunity import PreliminaryCandidate
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchHit
from pensae.opportunities import EvidenceInput
from pensae.research.schemas import (
    PatternSynthesis,
    SegmentAssessment,
    SegmentSolution,
    SignalSelection,
)


@dataclass(frozen=True, slots=True)
class SelectedSource:
    id: UUID
    hit: SearchHit
    retrieved: RetrievedSource
    selection: SignalSelection
    evidence: tuple[EvidenceInput, ...]


@dataclass(frozen=True, slots=True)
class FocusedSource:
    id: UUID
    hit: SearchHit
    retrieved: RetrievedSource
    evidence: tuple[EvidenceInput, ...]
    untrusted_spans: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class PatternCandidate:
    fingerprint: str
    candidate: PreliminaryCandidate
    selected: tuple[SelectedSource, ...]
    synthesis: PatternSynthesis
    segment: SegmentAssessment
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SynthesizedPattern:
    fingerprint: str
    synthesis: PatternSynthesis
    selected: tuple[SelectedSource, ...]
    embedding: tuple[float, ...]
    exact_duplicate: bool
    insufficient_distinction: bool


@dataclass(frozen=True, slots=True)
class ConceptCandidate:
    pattern: PatternCandidate
    strategy: SegmentSolution
