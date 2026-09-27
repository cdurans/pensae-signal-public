from pathlib import Path
from urllib.parse import urlsplit

import pytest
from scripts.linux.configure_secrets import initialize


def test_new_installations_have_private_unique_matching_secrets(tmp_path: Path) -> None:
    template = Path(".env.example").read_bytes()
    values = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        (root / ".env.example").write_bytes(template)
        assert initialize(root)
        target = root / ".env"
        assert target.stat().st_mode & 0o777 == 0o600
        settings = dict(
            line.split("=", 1)
            for line in target.read_text().splitlines()
            if line and not line.startswith("#")
        )
        password = settings["POSTGRES_PASSWORD"]
        assert len(bytes.fromhex(password)) == 32
        assert len(bytes.fromhex(settings["SEARXNG_SECRET"])) == 32
        assert password != settings["SEARXNG_SECRET"]
        assert urlsplit(settings["PENSAE_POSTGRES_DSN"]).password == password
        values.append(password)
        assert (root / ".env.example").read_bytes() == template
    assert values[0] != values[1]


def test_existing_installation_and_symlinks_are_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_bytes(Path(".env.example").read_bytes())
    target = tmp_path / ".env"
    original = b"existing operator configuration\n"
    target.write_bytes(original)
    assert not initialize(tmp_path)
    assert target.read_bytes() == original
    target.unlink()
    other = tmp_path / "other"
    other.write_bytes(original)
    target.symlink_to(other)
    assert not initialize(tmp_path)
    assert other.read_bytes() == original
    target.unlink()
    target.symlink_to(tmp_path / "missing")
    assert not initialize(tmp_path)
    assert not (tmp_path / "missing").exists()


@pytest.mark.anyio
async def test_compose_teardown_works_without_installation_secrets(monkeypatch):
    from scripts.linux import launcher

    environments = []

    class Completed:
        async def wait(self):
            return 0

    async def spawn(*argv, **kwargs):
        environments.append(kwargs.get("env"))
        return Completed()

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.delenv("SEARXNG_SECRET", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    await launcher.run_fixed(("docker", "compose", "down", "--remove-orphans"))
    await launcher.run_fixed(("docker", "compose", "up", "--detach", "--wait"))
    assert len(bytes.fromhex(environments[0]["SEARXNG_SECRET"])) == 32
    assert len(bytes.fromhex(environments[0]["POSTGRES_PASSWORD"])) == 32
    assert environments[1] is None
