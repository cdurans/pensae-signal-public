"""Atomic ownership record tests use only pytest temporary paths."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from pensae.infrastructure.model_runtime import (
    AtomicOwnershipStore,
    ModelRole,
    OwnershipFormatError,
    OwnershipRecord,
)


def _record(role: ModelRole) -> OwnershipRecord:
    return OwnershipRecord(
        role=role,
        pid=101 if role is ModelRole.CHAT else 102,
        start_identity=f"boot-id:{role.value}-ticks",
        executable="/opt/pensae/llama-server",
        port=8085 if role is ModelRole.CHAT else 8086,
        launcher_instance="launcher-atomic-test",
    )


def test_atomic_store_round_trips_complete_records_with_restrictive_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "model-ownership.json"
    store = AtomicOwnershipStore(path)
    expected = {
        ModelRole.CHAT: _record(ModelRole.CHAT),
        ModelRole.EMBEDDING: _record(ModelRole.EMBEDDING),
    }

    store.replace(expected)

    assert store.load() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert decoded["version"] == 1
    assert decoded["records"] == [
        {
            "executable": "/opt/pensae/llama-server",
            "launcher_instance": "launcher-atomic-test",
            "pid": 101,
            "port": 8085,
            "role": "chat",
            "start_identity": "boot-id:chat-ticks",
        },
        {
            "executable": "/opt/pensae/llama-server",
            "launcher_instance": "launcher-atomic-test",
            "pid": 102,
            "port": 8086,
            "role": "embedding",
            "start_identity": "boot-id:embedding-ticks",
        },
    ]


def test_store_replacement_removes_obsolete_role_atomically(tmp_path: Path) -> None:
    path = tmp_path / "ownership.json"
    store = AtomicOwnershipStore(path)
    store.replace(
        {
            ModelRole.CHAT: _record(ModelRole.CHAT),
            ModelRole.EMBEDDING: _record(ModelRole.EMBEDDING),
        }
    )

    store.replace({ModelRole.EMBEDDING: _record(ModelRole.EMBEDDING)})

    assert store.load() == {ModelRole.EMBEDDING: _record(ModelRole.EMBEDDING)}


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"version":2,"records":[]}',
        '{"version":1,"records":{}}',
        '{"version":1,"records":[{"role":"chat"}]}',
    ],
)
def test_malformed_ownership_file_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "ownership.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(OwnershipFormatError):
        AtomicOwnershipStore(path).load()


def test_missing_ownership_file_means_no_owned_processes(tmp_path: Path) -> None:
    assert AtomicOwnershipStore(tmp_path / "missing.json").load() == {}


def test_ownership_path_must_be_protected_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        AtomicOwnershipStore(Path("runtime/ownership.json"))
