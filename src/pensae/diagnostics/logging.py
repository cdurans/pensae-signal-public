"""Allowlisted console and rotating JSONL diagnostics."""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import UUID

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_FIELDS = frozenset(
    {
        "request_id",
        "run_id",
        "opportunity_id",
        "component",
        "stage",
        "model_role",
        "status",
        "duration_ms",
        "status_code",
        "queries",
        "retrieved_pages",
        "signals",
        "patterns",
        "concepts",
        "opportunities",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "committed_count",
        "error_type",
    }
)


def _safe_value(name: str, value: object) -> str | int | float | bool | None:
    if value is None or type(value) in {int, float, bool}:
        return value  # type: ignore[return-value]
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(f"operational field {name} has an unsupported value type")
    if name in {"request_id", "run_id", "opportunity_id"}:
        UUID(value)
        return value
    if name == "error_type":
        if not value.isidentifier() or len(value) > 96:
            raise ValueError("error_type must be a bounded class identifier")
        return value
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"operational field {name} must be a bounded identifier")
    return value


class OperationalLogger:
    """Emit only closed-schema local operational events."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("pensae.operations")

    def emit(self, event: str, **fields: object) -> None:
        if not _IDENTIFIER.fullmatch(event):
            raise ValueError("operational event must be a bounded identifier")
        unknown = set(fields) - _SAFE_FIELDS
        if unknown:
            raise ValueError(f"prohibited operational log fields: {', '.join(sorted(unknown))}")
        safe = {name: _safe_value(name, value) for name, value in fields.items()}
        self._logger.info("operational_event", extra={"pensae_event": event, "safe_fields": safe})


class _JsonAllowlistFormatter(logging.Formatter):
    def __init__(self, *, verbose: bool) -> None:
        super().__init__()
        self._verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "pensae_event", "log_event")
        if not isinstance(event, str) or not _IDENTIFIER.fullmatch(event):
            event = "log_event"
        raw_fields = getattr(record, "safe_fields", {})
        fields: dict[str, Any] = {}
        if isinstance(raw_fields, dict):
            for name, value in raw_fields.items():
                if name in _SAFE_FIELDS:
                    try:
                        fields[name] = _safe_value(name, value)
                    except (TypeError, ValueError):
                        continue
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "event": event,
            **fields,
        }
        if self._verbose and record.exc_info and record.exc_info[2] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            payload["trace"] = [
                f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(record.exc_info[2])[-12:]
            ]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class _ConsoleAllowlistFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "pensae_event", "log_event")
        fields = getattr(record, "safe_fields", {})
        component = fields.get("component", "pensae") if isinstance(fields, dict) else "pensae"
        status = (
            fields.get("status", record.levelname.lower())
            if isinstance(fields, dict)
            else record.levelname.lower()
        )
        return f"{component}: {event} [{status}]"


def configure_operational_logging(
    log_dir: Path,
    *,
    rotation_size_mib: int = 10,
    retained_files: int = 5,
    verbose: bool = False,
) -> OperationalLogger:
    """Configure one concise console and exactly ``retained_files`` JSONL files."""

    if rotation_size_mib < 1 or retained_files < 1:
        raise ValueError("log rotation bounds must be positive")
    directory = log_dir.expanduser().resolve()
    if directory == Path("/"):
        raise ValueError("log directory cannot be filesystem root")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    logger = logging.getLogger("pensae.operations")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if getattr(handler, "pensae_managed", False):
            logger.removeHandler(handler)
            handler.close()
    console = logging.StreamHandler()
    console.setFormatter(_ConsoleAllowlistFormatter())
    console.pensae_managed = True  # type: ignore[attr-defined]
    file_handler = RotatingFileHandler(
        directory / "pensae.jsonl",
        maxBytes=rotation_size_mib * 1024 * 1024,
        backupCount=max(0, retained_files - 1),
        encoding="utf-8",
    )
    file_handler.setFormatter(_JsonAllowlistFormatter(verbose=verbose))
    file_handler.pensae_managed = True  # type: ignore[attr-defined]
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return OperationalLogger(logger)
