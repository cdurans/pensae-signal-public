from __future__ import annotations

import os
import socket
import subprocess
import time

import httpx
import psycopg
import pytest
import redis.asyncio as redis

pytestmark = pytest.mark.integration

_REDIS_PROTECTED_PORT = 6379


def _integration_enabled() -> bool:
    return os.environ.get("PENSAE_INTEGRATION") == "1"


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
@pytest.mark.anyio
async def test_postgres_pgvector_redis_and_searxng_are_healthy() -> None:
    dsn = os.environ["PENSAE_POSTGRES_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    async with (
        await psycopg.AsyncConnection.connect(dsn) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        vector_version = await cursor.fetchone()
    assert vector_version == ("0.8.2",)

    redis_client = redis.from_url(os.environ["PENSAE_REDIS_URL"])
    try:
        assert await redis_client.ping() is True
    finally:
        await redis_client.aclose()

    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        searxng = await client.get(os.environ["PENSAE_SEARXNG_URL"])
    assert searxng.status_code < 500


def _tcp_listeners(port: int) -> set[tuple[str, str]]:
    listeners: set[tuple[str, str]] = set()
    for family, path in (("ipv4", "/proc/net/tcp"), ("ipv6", "/proc/net/tcp6")):
        with open(path, encoding="ascii") as table:
            next(table)
            for line in table:
                fields = line.split()
                local_address, local_port = fields[1].split(":")
                if fields[3] == "0A" and int(local_port, 16) == port:
                    listeners.add((family, local_address))
    return listeners


def _wait_for_listener(port: int, *, present: bool, timeout_seconds: float = 10) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if bool(_tcp_listeners(port)) is present:
            return True
        time.sleep(0.1)
    return bool(_tcp_listeners(port)) is present


def _compose_redis(
    *arguments: str, timeout_seconds: float = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Docker argv and protected service name
        (
            "/usr/bin/docker",
            "compose",
            "--project-name",
            os.environ["COMPOSE_PROJECT_NAME"],
            *arguments,
            "redis",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
def test_compose_services_listen_only_on_ipv4_loopback() -> None:
    for port in (5432, 6379, 8888):
        assert _tcp_listeners(port) == {("ipv4", "0100007F")}


@pytest.mark.skipif(not _integration_enabled(), reason="requires disposable Compose profile")
def test_host_network_port_conflict_refuses_without_signaling_owner() -> None:
    stopped = _compose_redis("stop", "--timeout", "10")
    assert stopped.returncode == 0, stopped.stderr
    assert _wait_for_listener(_REDIS_PROTECTED_PORT, present=False)

    conflict: subprocess.CompletedProcess[str] | None = None
    owner_survived = False
    recovery: subprocess.CompletedProcess[str] | None = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", _REDIS_PROTECTED_PORT))
            blocker.listen()
            conflict = _compose_redis(
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "10",
            )
            with socket.create_connection(("127.0.0.1", _REDIS_PROTECTED_PORT), timeout=1):
                owner_survived = True
    finally:
        recovery = _compose_redis(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "30",
        )

    assert conflict is not None
    assert conflict.returncode != 0
    assert owner_survived
    assert recovery is not None
    assert recovery.returncode == 0, recovery.stderr
    assert _wait_for_listener(_REDIS_PROTECTED_PORT, present=True)
