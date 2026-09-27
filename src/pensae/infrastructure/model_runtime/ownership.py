"""Atomic, restrictive launcher ownership persistence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol, cast

from .types import ModelRole, OwnershipRecord


class OwnershipStore(Protocol):
    """Narrow storage contract used by the supervisor and deterministic fakes."""

    def load(self) -> dict[ModelRole, OwnershipRecord]: ...

    def replace(self, records: Mapping[ModelRole, OwnershipRecord]) -> None: ...


class OwnershipFormatError(RuntimeError):
    """Raised when an ownership file cannot be trusted."""


class AtomicOwnershipStore:
    """Persist all ownership records through one fsync + atomic replacement.

    The containing directory is mode ``0700`` and the JSON file is mode
    ``0600``. A malformed file fails closed rather than silently discarding the
    only durable evidence that a launcher may own a process.
    """

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("ownership path must be absolute")
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[ModelRole, OwnershipRecord]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            decoded_object: object = json.loads(raw)
            if not isinstance(decoded_object, dict):
                raise OwnershipFormatError("ownership file must be an object")
            decoded = cast(dict[str, object], decoded_object)
            if decoded.get("version") != 1:
                raise OwnershipFormatError("unsupported ownership file format")
            items_object = decoded.get("records")
            if not isinstance(items_object, list):
                raise OwnershipFormatError("ownership records must be a list")
            items = cast(list[object], items_object)
            records: dict[ModelRole, OwnershipRecord] = {}
            for untyped_item in items:
                if not isinstance(untyped_item, dict):
                    raise OwnershipFormatError("ownership record must be an object")
                item = cast(dict[str, object], untyped_item)
                record = self._decode_record(item)
                if record.role in records:
                    raise OwnershipFormatError(f"duplicate ownership role: {record.role.value}")
                records[record.role] = record
            return records
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise OwnershipFormatError("invalid ownership file") from error

    def replace(self, records: Mapping[ModelRole, OwnershipRecord]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        payload = {
            "version": 1,
            "records": [
                {
                    "role": record.role.value,
                    "pid": record.pid,
                    "start_identity": record.start_identity,
                    "executable": record.executable,
                    "port": record.port,
                    "launcher_instance": record.launcher_instance,
                }
                for _, record in sorted(records.items(), key=lambda item: item[0].value)
            ],
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
            os.chmod(self._path, 0o600)
            directory_descriptor = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary_path.unlink()
            raise

    @staticmethod
    def _decode_record(item: dict[str, object]) -> OwnershipRecord:
        role = ModelRole(_required_str(item, "role"))
        pid = _required_int(item, "pid")
        port = _required_int(item, "port")
        if pid <= 0 or not 1 <= port <= 65_535:
            raise OwnershipFormatError("ownership PID or port is out of range")
        return OwnershipRecord(
            role=role,
            pid=pid,
            start_identity=_required_str(item, "start_identity"),
            executable=_required_str(item, "executable"),
            port=port,
            launcher_instance=_required_str(item, "launcher_instance"),
        )


def _required_str(item: dict[str, object], key: str) -> str:
    value = item[key]
    if not isinstance(value, str) or not value:
        raise OwnershipFormatError(f"ownership {key} must be a non-empty string")
    return value


def _required_int(item: dict[str, object], key: str) -> int:
    value = item[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise OwnershipFormatError(f"ownership {key} must be an integer")
    return value
