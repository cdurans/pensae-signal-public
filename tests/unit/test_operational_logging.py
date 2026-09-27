from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

import pytest

from pensae.diagnostics import OperationalLogger, configure_operational_logging


def _log_files(path: Path) -> list[Path]:
    return sorted(path.glob("pensae.jsonl*"))


def test_operational_logging_is_allowlisted_utc_jsonl_and_prohibits_payload_fields(
    tmp_path: Path,
) -> None:
    logger = configure_operational_logging(tmp_path, verbose=True)
    request_id = uuid4()
    logger.emit(
        "request_completed",
        request_id=request_id,
        component="api",
        status="completed",
        duration_ms=4.25,
        status_code=200,
    )
    with pytest.raises(ValueError, match="prohibited"):
        logger.emit("unsafe", note="private operator note")
    with pytest.raises(ValueError, match="bounded identifier"):
        logger.emit("unsafe", component="https://example.test/?secret=value")

    payload = json.loads((tmp_path / "pensae.jsonl").read_text(encoding="utf-8"))
    assert payload["request_id"] == str(request_id)
    assert payload["timestamp"].endswith("Z")
    assert payload["event"] == "request_completed"
    assert set(payload) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "component",
        "status",
        "duration_ms",
        "status_code",
    }


def test_verbose_stack_trace_excludes_messages_paths_and_prohibited_content(
    tmp_path: Path,
) -> None:
    configure_operational_logging(tmp_path, verbose=True)
    raw = logging.getLogger("pensae.operations")
    secret = "prompt model-output excerpt search-query nonce cookie operator-note"
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        raw.exception(secret)

    text = (tmp_path / "pensae.jsonl").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert secret not in text
    assert str(tmp_path) not in text
    assert payload["event"] == "log_event"
    assert payload["error_type"] == "RuntimeError"
    assert payload["trace"]


def test_rotation_keeps_exactly_five_files(tmp_path: Path) -> None:
    configure_operational_logging(tmp_path, rotation_size_mib=1, retained_files=5)
    raw = logging.getLogger("pensae.operations")
    formatter = raw.handlers[-1].formatter
    raw.removeHandler(raw.handlers[-1])
    rotating = RotatingFileHandler(
        tmp_path / "pensae.jsonl",
        maxBytes=180,
        backupCount=4,
        encoding="utf-8",
    )
    rotating.pensae_managed = True  # type: ignore[attr-defined]
    rotating.setFormatter(formatter)
    raw.addHandler(rotating)
    logger = OperationalLogger(raw)
    for _ in range(40):
        logger.emit("rotation_probe", component="diagnostics", status="completed")
    for handler in raw.handlers:
        handler.flush()

    assert len(_log_files(tmp_path)) == 5
