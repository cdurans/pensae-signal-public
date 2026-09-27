from __future__ import annotations

import asyncio
from collections import defaultdict, deque

import pytest

from pensae.infrastructure.retrieval import (
    SafeSourceClient,
    SourceLimits,
    SourceUnavailableError,
    TransportResponse,
    UnsafeSourceError,
)

PUBLIC_IP = "93.184.216.34"


class FakeResolver:
    def __init__(self, addresses: dict[str, tuple[str, ...]] | None = None) -> None:
        self.addresses = addresses or {}
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        return self.addresses.get(host, (PUBLIC_IP,))


class FakeTransport:
    def __init__(self) -> None:
        self.responses: dict[str, deque[TransportResponse | BaseException]] = defaultdict(deque)
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    def queue(self, url: str, *responses: TransportResponse | BaseException) -> None:
        self.responses[url].extend(responses)

    async def request(
        self,
        *,
        url: str,
        resolved_addresses: tuple[str, ...],
        byte_limit: int,
        limits: SourceLimits,
    ) -> TransportResponse:
        del limits
        self.calls.append((url, resolved_addresses, byte_limit))
        response = self.responses[url].popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def _response(
    status: int,
    body: str = "",
    *,
    content_type: str = "text/plain; charset=utf-8",
    **headers: str,
) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        headers={"content-type": content_type, **headers},
        body=body.encode(),
    )


def _limits(**updates: object) -> SourceLimits:
    values: dict[str, object] = {
        "origin_delay_seconds": 0.001,
        "connect_timeout_seconds": 0.1,
        "read_timeout_seconds": 0.1,
        "total_timeout_seconds": 0.5,
        "robots_cache_seconds": 60,
    }
    values.update(updates)
    return SourceLimits(**values)  # type: ignore[arg-type]


def _client(
    transport: FakeTransport,
    *,
    resolver: FakeResolver | None = None,
    limits: SourceLimits | None = None,
) -> SafeSourceClient:
    return SafeSourceClient(
        resolver=resolver or FakeResolver(),
        transport=transport,
        limits=limits or _limits(),
    )


@pytest.mark.anyio
async def test_fetch_checks_robots_pins_dns_and_returns_normalized_plain_text() -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue(
        "https://example.com/report",
        _response(200, "  Maintenance   requests\r\narrive through email.  "),
    )

    result = await _client(transport).fetch("HTTPS://Example.COM:443/report#fragment")

    assert result.final_url == "https://example.com/report"
    assert result.extracted_text == "Maintenance requests\narrive through email."
    assert [call[0] for call in transport.calls] == [
        "https://example.com/robots.txt",
        "https://example.com/report",
    ]
    assert all(call[1] == (PUBLIC_IP,) for call in transport.calls)
    assert transport.calls[0][2] == 512 * 1024
    assert transport.calls[1][2] == 3 * 1024 * 1024


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "HTTP or HTTPS"),
        ("https://user:secret@example.com/report", "credentials"),
        ("https://example.com:8443/report", "default scheme port"),
        ("http://127.0.0.1/report", "non-public"),
        ("http://169.254.169.254/latest/meta-data", "non-public"),
    ],
)
async def test_unsafe_url_forms_are_blocked_before_transport(url: str, message: str) -> None:
    transport = FakeTransport()

    with pytest.raises(UnsafeSourceError, match=message):
        await _client(transport).fetch(url)
    assert transport.calls == []


@pytest.mark.anyio
async def test_any_nonpublic_dns_answer_fails_closed() -> None:
    transport = FakeTransport()
    resolver = FakeResolver({"example.com": (PUBLIC_IP, "10.0.0.5")})

    with pytest.raises(UnsafeSourceError, match="non-public"):
        await _client(transport, resolver=resolver).fetch("https://example.com/report")
    assert transport.calls == []


@pytest.mark.anyio
async def test_redirect_target_is_resolved_and_revalidated_before_following() -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue(
        "https://example.com/report",
        _response(302, location="http://127.0.0.1/internal"),
    )

    with pytest.raises(UnsafeSourceError, match="non-public"):
        await _client(transport).fetch("https://example.com/report")
    assert [call[0] for call in transport.calls] == [
        "https://example.com/robots.txt",
        "https://example.com/report",
    ]


@pytest.mark.anyio
async def test_cross_origin_redirect_enforces_destination_robots_before_target() -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue(
        "https://example.com/report",
        _response(302, location="https://destination.example/private/report"),
    )
    transport.queue(
        "https://destination.example/robots.txt",
        _response(200, "User-agent: *\nDisallow: /private"),
    )

    with pytest.raises(UnsafeSourceError, match="disallows"):
        await _client(transport).fetch("https://example.com/report")

    assert [call[0] for call in transport.calls] == [
        "https://example.com/robots.txt",
        "https://example.com/report",
        "https://destination.example/robots.txt",
    ]


@pytest.mark.anyio
async def test_redirect_limit_is_bounded() -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue("https://example.com/a", _response(302, location="/b"))
    transport.queue("https://example.com/b", _response(302, location="/c"))

    with pytest.raises(UnsafeSourceError, match="redirect limit"):
        await _client(transport, limits=_limits(max_redirects=1)).fetch("https://example.com/a")


@pytest.mark.anyio
async def test_robots_disallow_and_unverifiable_policy_fail_closed() -> None:
    disallowed = FakeTransport()
    disallowed.queue(
        "https://example.com/robots.txt",
        _response(200, "User-agent: *\nDisallow: /private"),
    )
    with pytest.raises(UnsafeSourceError, match="disallows"):
        await _client(disallowed).fetch("https://example.com/private/report")

    unavailable = FakeTransport()
    unavailable.queue("https://example.com/robots.txt", _response(503))
    with pytest.raises(UnsafeSourceError, match="could not be verified"):
        await _client(unavailable).fetch("https://example.com/report")


@pytest.mark.anyio
async def test_excessive_robots_crawl_delay_is_blocked() -> None:
    transport = FakeTransport()
    transport.queue(
        "https://example.com/robots.txt",
        _response(200, "User-agent: *\nCrawl-delay: 11\nAllow: /"),
    )

    with pytest.raises(UnsafeSourceError, match="crawl delay"):
        await _client(transport).fetch("https://example.com/report")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response(200, "binary", content_type="application/pdf"), "content type"),
        (
            _response(200, "compressed", **{"content-encoding": "gzip"}),
            "compressed",
        ),
        (_response(200, "x" * 101), "byte limit"),
    ],
)
async def test_content_and_byte_limits_fail_closed(
    response: TransportResponse, message: str
) -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue("https://example.com/report", response)

    with pytest.raises(UnsafeSourceError, match=message):
        await _client(transport, limits=_limits(max_response_bytes=100)).fetch(
            "https://example.com/report"
        )


@pytest.mark.anyio
async def test_normalized_character_limit_is_enforced() -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue("https://example.com/report", _response(200, "x" * 101))

    with pytest.raises(UnsafeSourceError, match="character limit"):
        await _client(transport, limits=_limits(max_normalized_characters=100)).fetch(
            "https://example.com/report"
        )


@pytest.mark.anyio
async def test_public_page_is_not_retried_after_ordinary_failure() -> None:
    transport = FakeTransport()
    transport.queue("https://example.com/robots.txt", _response(404))
    transport.queue("https://example.com/report", _response(503))

    with pytest.raises(SourceUnavailableError, match="HTTP 503"):
        await _client(transport).fetch("https://example.com/report")
    assert sum(call[0].endswith("/report") for call in transport.calls) == 1


class HangingTransport(FakeTransport):
    async def request(
        self,
        *,
        url: str,
        resolved_addresses: tuple[str, ...],
        byte_limit: int,
        limits: SourceLimits,
    ) -> TransportResponse:
        del url, resolved_addresses, byte_limit, limits
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.anyio
async def test_total_timeout_is_bounded() -> None:
    with pytest.raises(SourceUnavailableError, match="total timeout"):
        await _client(HangingTransport(), limits=_limits(total_timeout_seconds=0.01)).fetch(
            "https://example.com/report"
        )
