"""Capability ports used by the research workflow."""

from __future__ import annotations

from typing import Protocol

from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchBatch


class Searcher(Protocol):
    async def search(self, query: str, *, result_limit: int = 10) -> SearchBatch: ...


class Retriever(Protocol):
    async def fetch(self, url: str) -> RetrievedSource: ...
