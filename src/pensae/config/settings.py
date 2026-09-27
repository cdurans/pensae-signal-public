from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    RELEASE = "release"


def _require_loopback_url(value: object) -> str:
    url = str(value)
    parsed = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    if parsed.hostname is None:
        raise ValueError("URL must include a host")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("service host must be an IPv4 loopback address") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("service host must be an IPv4 loopback address")
    return url


LoopbackUrl = Annotated[str, BeforeValidator(_require_loopback_url)]


class BootstrapSettings(BaseSettings):
    """Protected installation values read once when the Fedora process starts."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PENSAE_",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    app_host: Literal["127.0.0.1"] = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1024, le=65535)
    postgres_dsn: LoopbackUrl = "postgresql+psycopg://pensae@127.0.0.1:5432/pensae"
    redis_url: LoopbackUrl = "redis://127.0.0.1:6379/0"
    searxng_url: LoopbackUrl = "http://127.0.0.1:8888"
    chat_url: LoopbackUrl = "http://127.0.0.1:8085"
    embedding_url: LoopbackUrl = "http://127.0.0.1:8086"
    data_dir: Path = Path(".pensae/data")
    log_dir: Path = Path(".pensae/logs")
    ownership_dir: Path = Path(".pensae/ownership")
    verbose_logging: bool = False
    llama_executable: Path | None = None
    chat_model_path: Path | None = None
    embedding_model_path: Path | None = None

    @field_validator("data_dir", "log_dir", "ownership_dir")
    @classmethod
    def reject_root_runtime_directory(cls, value: Path) -> Path:
        resolved = value.expanduser()
        if resolved == Path("/"):
            raise ValueError("runtime directory cannot be filesystem root")
        return resolved

    @property
    def canonical_origin(self) -> str:
        return f"http://{self.app_host}:{self.app_port}"

    @property
    def canonical_host_header(self) -> str:
        return f"{self.app_host}:{self.app_port}"
