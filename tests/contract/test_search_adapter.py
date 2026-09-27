from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from pensae.infrastructure.search import (
    SearchContractError,
    SearxngSearchClient,
    canonicalize_result_url,
)


def test_canonical_url_normalizes_host_default_port_and_fragment() -> None:
    assert canonicalize_result_url("HTTPS://Example.COM:443/path?q=1#fragment") == (
        "https://example.com/path?q=1"
    )


@pytest.mark.anyio
async def test_search_uses_fixed_local_contract_deduplicates_and_exposes_warnings() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://Example.com:443/report#top",
                        "title": " Maintenance report ",
                        "content": "Discovery snippet only",
                        "engine": "brave",
                    },
                    {
                        "url": "https://example.com/report",
                        "title": "Duplicate",
                        "content": "duplicate snippet",
                        "engines": ["duckduckgo"],
                    },
                    {
                        "url": "file:///etc/passwd",
                        "title": "Unsafe",
                        "content": "ignored",
                    },
                ],
                "unresponsive_engines": [["duckduckgo", "timeout"]],
            },
        )

    batch = await SearxngSearchClient(
        base_url="http://127.0.0.1:8888", transport=httpx.MockTransport(handler)
    ).search("  maintenance   request workflow ")

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/search"
    assert dict(requests[0].url.params) == {"format": "json"}
    form = parse_qs(requests[0].content.decode("ascii"))
    assert form == {
        "q": ["maintenance request workflow"],
        "categories": ["general"],
        "language": ["en-US"],
        "engines": ["brave,duckduckgo,yandex"],
    }
    assert len(batch.results) == 1
    assert batch.results[0].canonical_url == "https://example.com/report"
    assert batch.results[0].engines == ("brave", "duckduckgo")
    assert [(warning.engine, warning.message) for warning in batch.warnings] == [
        ("duckduckgo", "timeout"),
        ("pensae", "ignored unsafe result URL: search result URL must use HTTP or HTTPS"),
    ]


@pytest.mark.anyio
async def test_search_retries_local_searxng_once_and_never_uses_a_fallback() -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    batch = await SearxngSearchClient(
        base_url="http://127.0.0.1:8888", transport=httpx.MockTransport(handler)
    ).search("workflow")

    assert attempts == 2
    assert batch.results == ()
    assert batch.retries == 1


@pytest.mark.anyio
async def test_search_fails_after_the_single_allowed_retry() -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    client = SearxngSearchClient(
        base_url="http://127.0.0.1:8888", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(SearchContractError, match="one retry") as error:
        await client.search("workflow")
    assert attempts == 2
    assert error.value.retries == 1


@pytest.mark.anyio
async def test_search_rejects_malformed_json_contract() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_results": []})

    client = SearxngSearchClient(
        base_url="http://127.0.0.1:8888", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(SearchContractError, match="results array"):
        await client.search("workflow")
