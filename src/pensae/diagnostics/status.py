"""Safe launcher ownership metadata exposed by local status."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OwnershipState(StrEnum):
    NONE = "none"
    OWNED = "owned"
    INVALID = "invalid"


class LauncherOwnershipStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    app: OwnershipState = OwnershipState.NONE
    chat: OwnershipState = OwnershipState.NONE
    embedding: OwnershipState = OwnershipState.NONE
    failure_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
