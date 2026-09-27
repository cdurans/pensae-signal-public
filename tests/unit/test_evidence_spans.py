from __future__ import annotations

import unicodedata

import pytest

from pensae.domain.evidence import (
    EvidenceIntegrityError,
    construct_verified_evidence,
    create_transient_spans,
    normalize_extraction,
)


def test_normalization_and_excerpt_are_mechanical_and_bounded() -> None:
    decomposed = "Cafe\u0301"
    document = create_transient_spans(
        source_id="source-a",
        extracted_text=f"  {decomposed}   teams\r\ncoordinate   maintenance.  ",
        span_characters=24,
    )

    evidence = construct_verified_evidence(
        document,
        selected_span_ids=(document.spans[0].span_id, document.spans[1].span_id),
        supported_claim="Maintenance coordination is fragmented.",
    )

    assert document.normalized_text == unicodedata.normalize(
        "NFC", "Café teams\ncoordinate maintenance."
    )
    assert evidence.excerpt == document.normalized_text[:48]
    assert len(evidence.content_fingerprint) == 64
    assert "Maintenance coordination" not in evidence.excerpt


@pytest.mark.parametrize(
    "selection, message",
    [
        (("source-a:s9999",), "does not exist"),
        (("source-a:s0002", "source-a:s0001"), "ordered and contiguous"),
        (("source-a:s0001", "source-a:s0003"), "ordered and contiguous"),
    ],
)
def test_missing_reordered_or_noncontiguous_spans_are_rejected(
    selection: tuple[str, ...], message: str
) -> None:
    document = create_transient_spans(
        source_id="source-a", extracted_text="x" * 800, span_characters=200
    )

    with pytest.raises(EvidenceIntegrityError, match=message):
        construct_verified_evidence(document, selected_span_ids=selection, supported_claim="claim")


def test_excerpt_over_500_characters_is_rejected_without_truncation() -> None:
    document = create_transient_spans(
        source_id="source-a", extracted_text="x" * 600, span_characters=200
    )

    with pytest.raises(EvidenceIntegrityError, match="durable limit"):
        construct_verified_evidence(
            document,
            selected_span_ids=tuple(span.span_id for span in document.spans),
            supported_claim="claim",
        )


def test_empty_extraction_and_untrusted_source_identifier_are_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        create_transient_spans(source_id="source-a", extracted_text=" \r\n ")
    with pytest.raises(ValueError, match="opaque"):
        create_transient_spans(source_id="../../source", extracted_text="content")


def test_normalize_extraction_drops_blank_lines_and_collapses_horizontal_space() -> None:
    assert normalize_extraction("a\t b\n\n c  d") == "a b\nc d"
