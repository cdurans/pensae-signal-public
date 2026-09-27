"""Mechanical evidence construction from transient normalized source text."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WHITESPACE = re.compile(r"[^\S\n]+")


class EvidenceIntegrityError(ValueError):
    """Raised when model-selected span references cannot form durable evidence."""


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    source_id: str
    ordinal: int
    start: int
    end: int
    text: str

    @property
    def span_id(self) -> str:
        return f"{self.source_id}:s{self.ordinal:04d}"


@dataclass(frozen=True, slots=True)
class TransientSpanDocument:
    """Normalized extraction retained only for the active processing operation."""

    source_id: str
    normalized_text: str
    content_fingerprint: str
    spans: tuple[EvidenceSpan, ...]


@dataclass(frozen=True, slots=True)
class VerifiedEvidence:
    """Minimum durable evidence produced without accepting model-written quotes."""

    source_id: str
    span_ids: tuple[str, ...]
    excerpt: str
    supported_claim: str
    content_fingerprint: str


def normalize_extraction(value: str) -> str:
    """Apply the frozen NFC, line-ending, and whitespace normalization contract."""

    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_WHITESPACE.sub(" ", line).strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line)


def create_transient_spans(
    *, source_id: str, extracted_text: str, span_characters: int = 240
) -> TransientSpanDocument:
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("source_id must be a short opaque identifier")
    if span_characters <= 0 or span_characters > 500:
        raise ValueError("span_characters must be between 1 and 500")
    normalized = normalize_extraction(extracted_text)
    if not normalized:
        raise ValueError("normalized extraction cannot be empty")
    spans = tuple(
        EvidenceSpan(
            source_id=source_id,
            ordinal=ordinal,
            start=start,
            end=min(start + span_characters, len(normalized)),
            text=normalized[start : start + span_characters],
        )
        for ordinal, start in enumerate(range(0, len(normalized), span_characters), start=1)
    )
    return TransientSpanDocument(
        source_id=source_id,
        normalized_text=normalized,
        content_fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        spans=spans,
    )


def construct_verified_evidence(
    document: TransientSpanDocument,
    *,
    selected_span_ids: tuple[str, ...],
    supported_claim: str,
    excerpt_limit: int = 500,
) -> VerifiedEvidence:
    """Construct an exact excerpt from ordered, contiguous spans of one source."""

    if not selected_span_ids:
        raise EvidenceIntegrityError("at least one span is required")
    if not supported_claim.strip():
        raise EvidenceIntegrityError("supported claim cannot be empty")
    by_id = {span.span_id: span for span in document.spans}
    try:
        selected = tuple(by_id[span_id] for span_id in selected_span_ids)
    except KeyError as exc:
        raise EvidenceIntegrityError("selected span does not exist") from exc
    if any(span.source_id != document.source_id for span in selected):
        raise EvidenceIntegrityError("cross-source evidence is forbidden")
    ordinals = tuple(span.ordinal for span in selected)
    expected = tuple(range(ordinals[0], ordinals[0] + len(ordinals)))
    if ordinals != expected:
        raise EvidenceIntegrityError("selected spans must be ordered and contiguous")
    excerpt = document.normalized_text[selected[0].start : selected[-1].end]
    if len(excerpt) > excerpt_limit:
        raise EvidenceIntegrityError("verified excerpt exceeds the durable limit")
    if excerpt != document.normalized_text[selected[0].start : selected[-1].end]:
        raise EvidenceIntegrityError("verified excerpt does not match normalized source text")
    return VerifiedEvidence(
        source_id=document.source_id,
        span_ids=selected_span_ids,
        excerpt=excerpt,
        supported_claim=supported_claim.strip(),
        content_fingerprint=document.content_fingerprint,
    )
