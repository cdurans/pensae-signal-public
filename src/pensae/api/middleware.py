"""Loopback mutation security and content-safe request correlation."""

from __future__ import annotations

import time
from contextlib import suppress
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pensae.config.settings import BootstrapSettings
from pensae.diagnostics import OperationalLogger

FORWARDED_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
)
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_MUTATION_BYTES = 65_536


def _security_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"


class LocalSecurityMiddleware:
    def __init__(self, app: ASGIApp, *, settings: BootstrapSettings, nonce: str) -> None:
        self._app = app
        self._settings = settings
        self._nonce = nonce

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        _security_headers(response)
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get("host") != self._settings.canonical_host_header:
            await self._reject(
                scope, receive, send, status_code=400, detail="invalid local Host header"
            )
            return
        if any(name in headers for name in FORWARDED_HEADERS):
            await self._reject(
                scope, receive, send, status_code=400, detail="forwarded headers are forbidden"
            )
            return
        method = str(scope.get("method", "GET")).upper()
        downstream_receive = receive
        if method in MUTATION_METHODS:
            content_length = headers.get("content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    await self._reject(
                        scope, receive, send, status_code=400, detail="invalid content length"
                    )
                    return
                if declared_length > MAX_MUTATION_BYTES:
                    await self._reject(
                        scope, receive, send, status_code=413, detail="mutation body is too large"
                    )
                    return
            chunks = bytearray()
            more_body = True
            while more_body:
                request_message = await receive()
                if request_message["type"] == "http.disconnect":
                    return
                chunks.extend(request_message.get("body", b""))
                if len(chunks) > MAX_MUTATION_BYTES:
                    await self._reject(
                        scope, receive, send, status_code=413, detail="mutation body is too large"
                    )
                    return
                more_body = bool(request_message.get("more_body", False))
            replayed = False

            async def replay_body() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(chunks), "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            downstream_receive = replay_body
            content_type = headers.get("content-type", "").partition(";")[0].strip().lower()
            if content_type != "application/json":
                await self._reject(
                    scope,
                    downstream_receive,
                    send,
                    status_code=415,
                    detail="mutations require application/json",
                )
                return
            if headers.get("x-pensae-session") != self._nonce:
                await self._reject(
                    scope,
                    downstream_receive,
                    send,
                    status_code=403,
                    detail="invalid session nonce",
                )
                return
            origin_ok = headers.get("origin") == self._settings.canonical_origin
            fetch_metadata_ok = headers.get("sec-fetch-site") == "same-origin"
            if not (origin_ok or fetch_metadata_ok):
                await self._reject(
                    scope,
                    downstream_receive,
                    send,
                    status_code=403,
                    detail="cross-origin mutation denied",
                )
                return

        async def send_with_security(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["Cache-Control"] = "no-store"
                response_headers["Content-Security-Policy"] = (
                    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                    "form-action 'self'; object-src 'none'"
                )
                response_headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
                )
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Cross-Origin-Resource-Policy"] = "same-origin"
            await send(message)

        await self._app(scope, downstream_receive, send_with_security)


class RequestCorrelationMiddleware:
    """Generate local request IDs without accepting or recording request payloads."""

    def __init__(self, app: ASGIApp, *, logger: OperationalLogger | None = None) -> None:
        self._app = app
        self._logger = logger or OperationalLogger()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid4()
        started = time.perf_counter()
        status_code = 500
        response_started = False
        response_complete = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_complete, response_started, status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_started = True
                MutableHeaders(scope=message)["X-Request-ID"] = str(request_id)
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        except Exception as exc:
            self._logger.emit(
                "request_failed",
                request_id=request_id,
                component="api",
                status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                status_code=500,
                error_type=type(exc).__name__,
            )
            if not response_started:
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "request failed safely", "request_id": str(request_id)},
                )
                _security_headers(response)
                await response(scope, receive, send_with_request_id)
            elif not response_complete:
                # The peer may already be gone. Either way, contain the secret-bearing
                # downstream exception instead of handing its context to the server logger.
                with suppress(Exception):
                    await send_with_request_id(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    )
            return
        self._logger.emit(
            "request_completed",
            request_id=request_id,
            component="api",
            status="completed",
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            status_code=status_code,
        )
