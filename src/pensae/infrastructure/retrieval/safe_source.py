"""Fail-closed bounded public source retrieval with DNS pinning and robots policy."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import urllib.robotparser
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
import trafilatura
from aiohttp.abc import AbstractResolver, ResolveResult

from pensae.domain.evidence import normalize_extraction

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ACCEPTED_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
_USER_AGENT = "PensaeResearchBot/1.0"


class UnsafeSourceError(RuntimeError):
    """The source violates a security, robots, or protected-limit rule."""


class SourceUnavailableError(RuntimeError):
    """The source is ordinarily inaccessible and may become a run warning."""


@dataclass(frozen=True, slots=True)
class SourceLimits:
    global_connections: int = 4
    per_origin_connections: int = 1
    origin_delay_seconds: float = 1
    max_redirects: int = 5
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 10
    total_timeout_seconds: float = 20
    max_response_bytes: int = 3 * 1024 * 1024
    max_normalized_characters: int = 200_000
    robots_max_bytes: int = 512 * 1024
    robots_cache_seconds: float = 24 * 60 * 60
    max_robots_crawl_delay_seconds: float = 10

    def __post_init__(self) -> None:
        numeric_values = (
            self.global_connections,
            self.per_origin_connections,
            self.origin_delay_seconds,
            self.max_redirects,
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.total_timeout_seconds,
            self.max_response_bytes,
            self.max_normalized_characters,
            self.robots_max_bytes,
            self.robots_cache_seconds,
            self.max_robots_crawl_delay_seconds,
        )
        if any(value <= 0 for value in numeric_values):
            raise ValueError("all source limits must be positive")
        if self.per_origin_connections != 1:
            raise ValueError("safe retrieval requires one connection per origin")


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class RetrievedSource:
    final_url: str
    media_type: str
    extracted_text: str
    response_bytes: int


class PublicResolver(Protocol):
    async def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SourceTransport(Protocol):
    async def request(
        self,
        *,
        url: str,
        resolved_addresses: tuple[str, ...],
        byte_limit: int,
        limits: SourceLimits,
    ) -> TransportResponse: ...


class SystemPublicResolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(str(record[4][0]) for record in records))


class _PinnedResolver(AbstractResolver):
    def __init__(self, *, host: str, addresses: tuple[str, ...]) -> None:
        self._host = host
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host != self._host:
            raise OSError("pinned resolver refused an unexpected hostname")
        results: list[ResolveResult] = []
        for address in self._addresses:
            parsed = ipaddress.ip_address(address)
            address_family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            results.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=address_family,
                    proto=socket.IPPROTO_TCP,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return results

    async def close(self) -> None:
        return None


class AiohttpPinnedTransport:
    """One-request transport whose connector can resolve only prevalidated IPs."""

    async def request(
        self,
        *,
        url: str,
        resolved_addresses: tuple[str, ...],
        byte_limit: int,
        limits: SourceLimits,
    ) -> TransportResponse:
        parsed = urlsplit(url)
        if parsed.hostname is None:
            raise UnsafeSourceError("source URL has no hostname")
        resolver = _PinnedResolver(host=parsed.hostname, addresses=resolved_addresses)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            limit=1,
            limit_per_host=1,
        )
        timeout = aiohttp.ClientTimeout(
            total=limits.total_timeout_seconds,
            connect=limits.connect_timeout_seconds,
            sock_read=limits.read_timeout_seconds,
        )
        try:
            async with (
                aiohttp.ClientSession(
                    connector=connector,
                    connector_owner=True,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=timeout,
                    trust_env=False,
                    auto_decompress=False,
                ) as session,
                session.get(
                    url,
                    allow_redirects=False,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,text/plain",
                        "Accept-Encoding": "identity",
                        "User-Agent": _USER_AGENT,
                    },
                ) as response,
            ):
                if response.content_length is not None and response.content_length > byte_limit:
                    raise UnsafeSourceError("source response exceeds the byte limit")
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > byte_limit:
                        raise UnsafeSourceError("source response exceeds the byte limit")
                return TransportResponse(
                    status_code=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=bytes(body),
                )
        except UnsafeSourceError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise SourceUnavailableError("source request failed") from exc


@dataclass(frozen=True, slots=True)
class _RobotsDecision:
    expires_at: float
    parser: urllib.robotparser.RobotFileParser | None
    allowed_without_rules: bool
    crawl_delay: float


class SafeSourceClient:
    def __init__(
        self,
        *,
        resolver: PublicResolver | None = None,
        transport: SourceTransport | None = None,
        limits: SourceLimits | None = None,
    ) -> None:
        self._resolver = resolver or SystemPublicResolver()
        self._transport = transport or AiohttpPinnedTransport()
        self._limits = limits or SourceLimits()
        self._global = asyncio.Semaphore(self._limits.global_connections)
        self._origin_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, _RobotsDecision] = {}

    async def fetch(self, url: str) -> RetrievedSource:
        try:
            async with asyncio.timeout(self._limits.total_timeout_seconds):
                validated = await self._validate_and_resolve(url)
                await self._enforce_robots(validated)
                final_url, response = await self._follow_redirects(
                    validated, byte_limit=self._limits.max_response_bytes
                )
                return self._extract(final_url, response)
        except TimeoutError as exc:
            raise SourceUnavailableError("source retrieval exceeded the total timeout") from exc

    async def _follow_redirects(
        self,
        target: tuple[str, tuple[str, ...]],
        *,
        byte_limit: int,
        enforce_redirect_robots: bool = True,
    ) -> tuple[str, TransportResponse]:
        url, addresses = target
        for redirect_count in range(self._limits.max_redirects + 1):
            response = await self._paced_request(url, addresses, byte_limit=byte_limit)
            if len(response.body) > byte_limit:
                raise UnsafeSourceError("source response exceeds the byte limit")
            if response.status_code not in _REDIRECT_STATUSES:
                return url, response
            if redirect_count == self._limits.max_redirects:
                raise UnsafeSourceError("source exceeded the redirect limit")
            location = _header(response.headers, "location")
            if not location:
                raise SourceUnavailableError("redirect response omitted Location")
            url, addresses = await self._validate_and_resolve(urljoin(url, location))
            if enforce_redirect_robots:
                await self._enforce_robots((url, addresses))
        raise AssertionError("redirect loop exhausted unexpectedly")

    async def _validate_and_resolve(self, url: str) -> tuple[str, tuple[str, ...]]:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise UnsafeSourceError("source URL is malformed") from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise UnsafeSourceError("source URL must use HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeSourceError("source URL credentials are forbidden")
        if parsed.hostname is None:
            raise UnsafeSourceError("source URL must include a hostname")
        expected_port = 443 if scheme == "https" else 80
        if port is not None and port != expected_port:
            raise UnsafeSourceError("source URL must use the default scheme port")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        normalized_host = f"[{host}]" if ":" in host else host
        netloc = normalized_host
        normalized_url = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
        try:
            literal = ipaddress.ip_address(host)
            addresses = (str(literal),)
        except ValueError:
            try:
                async with asyncio.timeout(self._limits.connect_timeout_seconds):
                    addresses = await self._resolver.resolve(host, expected_port)
            except (OSError, TimeoutError) as exc:
                raise SourceUnavailableError("source DNS resolution failed") from exc
        if not addresses:
            raise SourceUnavailableError("source DNS resolution returned no addresses")
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeSourceError("resolver returned an invalid address") from exc
            if not parsed_address.is_global:
                raise UnsafeSourceError("source resolved to a non-public address")
        return normalized_url, tuple(dict.fromkeys(addresses))

    async def _paced_request(
        self, url: str, addresses: tuple[str, ...], *, byte_limit: int
    ) -> TransportResponse:
        origin = _origin(url)
        lock = self._origin_locks.setdefault(origin, asyncio.Lock())
        async with self._global, lock:
            now = time.monotonic()
            delay = self._limits.origin_delay_seconds
            robots = self._robots.get(origin)
            if robots is not None:
                delay = max(delay, robots.crawl_delay)
            remaining = delay - (now - self._last_request.get(origin, 0))
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                return await self._transport.request(
                    url=url,
                    resolved_addresses=addresses,
                    byte_limit=byte_limit,
                    limits=self._limits,
                )
            finally:
                self._last_request[origin] = time.monotonic()

    async def _enforce_robots(self, target: tuple[str, tuple[str, ...]]) -> None:
        url, _ = target
        origin = _origin(url)
        decision = self._robots.get(origin)
        now = time.monotonic()
        if decision is None or decision.expires_at <= now:
            robots_url, robots_addresses = await self._validate_and_resolve(f"{origin}/robots.txt")
            try:
                _, response = await self._follow_redirects(
                    (robots_url, robots_addresses),
                    byte_limit=self._limits.robots_max_bytes,
                    enforce_redirect_robots=False,
                )
            except SourceUnavailableError as exc:
                raise UnsafeSourceError("robots policy could not be verified") from exc
            decision = self._robots_decision(robots_url, response)
            self._robots[origin] = decision
        if decision.parser is not None and not decision.parser.can_fetch(_USER_AGENT, url):
            raise UnsafeSourceError("robots policy disallows this source")
        if not decision.allowed_without_rules and decision.parser is None:
            raise UnsafeSourceError("robots policy could not be verified")

    def _robots_decision(self, robots_url: str, response: TransportResponse) -> _RobotsDecision:
        expires_at = time.monotonic() + self._limits.robots_cache_seconds
        if 400 <= response.status_code < 500:
            return _RobotsDecision(expires_at, None, True, 0)
        if not 200 <= response.status_code < 300:
            return _RobotsDecision(expires_at, None, False, 0)
        text = response.body.decode("utf-8", errors="replace")
        parser = urllib.robotparser.RobotFileParser(robots_url)
        parser.parse(text.splitlines())
        raw_crawl_delay = parser.crawl_delay(_USER_AGENT) or parser.crawl_delay("*") or 0
        try:
            crawl_delay = float(raw_crawl_delay)
        except (TypeError, ValueError) as exc:
            raise UnsafeSourceError("robots crawl delay is malformed") from exc
        if crawl_delay > self._limits.max_robots_crawl_delay_seconds:
            raise UnsafeSourceError("robots crawl delay exceeds the protected limit")
        return _RobotsDecision(expires_at, parser, False, float(crawl_delay))

    def _extract(self, final_url: str, response: TransportResponse) -> RetrievedSource:
        if not 200 <= response.status_code < 300:
            raise SourceUnavailableError(f"source returned HTTP {response.status_code}")
        encoding = _header(response.headers, "content-encoding")
        if encoding and encoding.lower() != "identity":
            raise UnsafeSourceError("compressed source responses are forbidden")
        media_type = _media_type(_header(response.headers, "content-type"))
        if media_type not in _ACCEPTED_MEDIA_TYPES:
            raise UnsafeSourceError("source content type is not supported")
        decoded = response.body.decode("utf-8", errors="replace")
        if media_type == "text/plain":
            extracted = decoded
        else:
            parsed = trafilatura.extract(
                decoded,
                output_format="txt",
                include_comments=False,
                include_images=False,
                include_links=False,
                include_tables=True,
            )
            if parsed is None:
                raise SourceUnavailableError("source contained no extractable text")
            extracted = parsed
        normalized = normalize_extraction(extracted)
        if not normalized:
            raise SourceUnavailableError("source contained no normalized text")
        if len(normalized) > self._limits.max_normalized_characters:
            raise UnsafeSourceError("source exceeds the normalized character limit")
        return RetrievedSource(
            final_url=final_url,
            media_type=media_type,
            extracted_text=normalized,
            response_bytes=len(response.body),
        )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name), None)


def _media_type(value: str | None) -> str:
    if value is None:
        return ""
    message = Message()
    message["content-type"] = value
    return message.get_content_type().lower()
