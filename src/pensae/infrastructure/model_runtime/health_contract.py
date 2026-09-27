from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

import httpx


class LlamaHealthState(StrEnum):
    READY = "ready"
    STARTING = "starting"
    UNKNOWN = "unknown"


def classify_llama_health(response: httpx.Response) -> LlamaHealthState:
    """Classify only the pinned llama.cpp ready/loading health contracts."""

    try:
        payload = response.json()
    except ValueError:
        return LlamaHealthState.UNKNOWN
    if (
        response.status_code == 200
        and isinstance(payload, Mapping)
        and payload.get("status") == "ok"
    ):
        return LlamaHealthState.READY
    if response.status_code != 503 or not isinstance(payload, Mapping):
        return LlamaHealthState.UNKNOWN
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return LlamaHealthState.UNKNOWN
    if (
        error.get("code") == 503
        and error.get("message") == "Loading model"
        and error.get("type") == "unavailable_error"
    ):
        return LlamaHealthState.STARTING
    return LlamaHealthState.UNKNOWN
