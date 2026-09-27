"""Typed local SearXNG JSON search with deterministic URL deduplication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

_SUPPORTED_SCHEMES = frozenset({"http", "https"})
SEARCH_ENGINES: Final = ("brave", "duckduckgo", "yandex")


class SearchContractError(RuntimeError):
    """Raised when the fixed local SearXNG contract is unavailable or malformed."""

    def __init__(self, message: str, *, retries: int = 0) -> None:
        super().__init__(message)
        if retries not in (0, 1):
            raise ValueError("SearXNG failure permits at most one retry")
        self.retries = retries


@dataclass(frozen=True, slots=True)
class SearchWarning:
    engine: str
    message: str


@dataclass(frozen=True, slots=True)
class SearchHit:
    canonical_url: str
    title: str
    snippet: str
    engines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchBatch:
    query: str
    results: tuple[SearchHit, ...]
    warnings: tuple[SearchWarning, ...]
    retries: int = 0

    def __post_init__(self) -> None:
        if self.retries not in (0, 1):
            raise ValueError("SearXNG search permits at most one retry")


def canonicalize_result_url(value: str) -> str:
    """Create a conservative retrieval candidate key without changing query semantics."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("search result URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError("search result URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("search result URL cannot contain credentials")
    if parsed.hostname is None:
        raise ValueError("search result URL must include a host")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit(SplitResult(scheme, netloc, path, parsed.query, ""))


class SearxngSearchClient:
    """Serialized client for the one configured local SearXNG endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("search timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._lock = asyncio.Lock()

    async def search(self, query: str, *, result_limit: int = 10) -> SearchBatch:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise ValueError("search query cannot be empty")
        if not 1 <= result_limit <= 10:
            raise ValueError("search result limit must be between 1 and 10")
        async with self._lock:
            payload, retries = await self._request_with_one_retry(normalized_query)
        return _parse_search_payload(
            normalized_query, payload, result_limit=result_limit, retries=retries
        )

    async def _request_with_one_retry(self, query: str) -> tuple[object, int]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{self._base_url}/search",
                        params={"format": "json"},
                        data={
                            "q": query,
                            "categories": "general",
                            "language": "en-US",
                            "engines": ",".join(SEARCH_ENGINES),
                        },
                        headers={"Accept": "application/json"},
                    )
                if response.status_code >= 500:
                    raise SearchContractError(f"local SearXNG returned HTTP {response.status_code}")
                response.raise_for_status()
                return response.json(), attempt
            except (httpx.HTTPError, ValueError, SearchContractError) as exc:
                last_error = exc
                if attempt == 1:
                    break
        raise SearchContractError(
            "local SearXNG search failed after one retry", retries=1
        ) from last_error


def _parse_search_payload(
    query: str, payload: object, *, result_limit: int, retries: int = 0
) -> SearchBatch:
    if not isinstance(payload, dict):
        raise SearchContractError("SearXNG response must be a JSON object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise SearchContractError("SearXNG response must contain a results array")
    warnings = list(_parse_engine_warnings(payload.get("unresponsive_engines", [])))
    deduplicated: dict[str, SearchHit] = {}
    for raw in raw_results:
        if len(deduplicated) >= result_limit:
            break
        if not isinstance(raw, dict):
            warnings.append(SearchWarning("pensae", "ignored malformed search result"))
            continue
        raw_url = raw.get("url")
        if not isinstance(raw_url, str):
            warnings.append(SearchWarning("pensae", "ignored result without a URL"))
            continue
        try:
            canonical_url = canonicalize_result_url(raw_url)
        except ValueError as exc:
            warnings.append(SearchWarning("pensae", f"ignored unsafe result URL: {exc}"))
            continue
        raw_title = raw.get("title")
        raw_snippet = raw.get("content")
        title = raw_title if isinstance(raw_title, str) else "Untitled result"
        snippet = raw_snippet if isinstance(raw_snippet, str) else ""
        engines = _result_engines(raw)
        existing = deduplicated.get(canonical_url)
        if existing is None:
            deduplicated[canonical_url] = SearchHit(
                canonical_url=canonical_url,
                title=" ".join(title.split())[:500],
                snippet=" ".join(snippet.split())[:1_000],
                engines=engines,
            )
        else:
            merged_engines = tuple(dict.fromkeys((*existing.engines, *engines)))
            deduplicated[canonical_url] = replace(existing, engines=merged_engines)
    return SearchBatch(
        query=query,
        results=tuple(deduplicated.values()),
        warnings=tuple(warnings),
        retries=retries,
    )


def _result_engines(raw: dict[object, object]) -> tuple[str, ...]:
    raw_engines = raw.get("engines")
    if isinstance(raw_engines, list):
        values = tuple(value for value in raw_engines if isinstance(value, str) and value)
        if values:
            return tuple(dict.fromkeys(values))
    engine = raw.get("engine")
    if isinstance(engine, str) and engine:
        return (engine,)
    return ()


def _parse_engine_warnings(raw: object) -> tuple[SearchWarning, ...]:
    if not isinstance(raw, list):
        raise SearchContractError("SearXNG unresponsive_engines must be an array")
    warnings: list[SearchWarning] = []
    for item in raw:
        if (
            isinstance(item, list)
            and len(item) >= 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            warnings.append(SearchWarning(engine=item[0], message=item[1]))
        elif isinstance(item, str):
            warnings.append(SearchWarning(engine=item, message="engine unavailable"))
        else:
            warnings.append(SearchWarning(engine="unknown", message="malformed engine warning"))
    return tuple(warnings)
