from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

CORPUS_ROOT = Path("tests/fixtures/offline_corpus")


@pytest.fixture(scope="session")
def corpus() -> dict[str, Any]:
    return json.loads((CORPUS_ROOT / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sources(corpus: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in corpus["sources"]}


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Make accidental network-marked evaluation tests visibly unsupported."""

    if item.get_closest_marker("network") is not None:
        pytest.fail("offline evaluation tests must not carry a network marker")


@pytest.fixture(autouse=True)
def no_network_dependency(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail if a corpus test accidentally opens a socket."""

    def forbidden_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline evaluation attempted network access")

    monkeypatch.setattr("socket.socket.connect", forbidden_socket)
    yield
