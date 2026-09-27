from pathlib import Path

import pytest
from pydantic import ValidationError

from pensae.config.settings import BootstrapSettings, Environment


@pytest.fixture(autouse=True)
def isolate_operator_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


def test_defaults_are_loopback_and_protected() -> None:
    settings = BootstrapSettings.model_validate({})

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.app_host == "127.0.0.1"
    assert settings.canonical_origin == "http://127.0.0.1:8000"
    assert settings.llama_executable is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("chat_url", "http://0.0.0.0:8085"),
        ("embedding_url", "http://192.168.1.10:8086"),
        ("redis_url", "redis://localhost:6379/0"),
        ("postgres_dsn", "postgresql+psycopg://u:p@10.0.0.5/db"),
    ],
)
def test_non_loopback_service_configuration_is_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="IPv4 loopback"):
        BootstrapSettings.model_validate({field: value})


def test_filesystem_root_is_not_a_runtime_directory() -> None:
    with pytest.raises(ValidationError, match="filesystem root"):
        BootstrapSettings.model_validate({"data_dir": Path("/")})
