"""SearXNG adapters are introduced in their owning phase."""

from .client import (
    SEARCH_ENGINES,
    SearchBatch,
    SearchContractError,
    SearchHit,
    SearchWarning,
    SearxngSearchClient,
    canonicalize_result_url,
)

__all__ = [
    "SEARCH_ENGINES",
    "SearchBatch",
    "SearchContractError",
    "SearchHit",
    "SearchWarning",
    "SearxngSearchClient",
    "canonicalize_result_url",
]
