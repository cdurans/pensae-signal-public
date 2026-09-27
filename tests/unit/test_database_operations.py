from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import pensae.operations.database as database_operations
from pensae.config.settings import BootstrapSettings, Environment
from pensae.operations.database import (
    BackupManager,
    CommandResult,
    DatabaseOperationError,
)


class RecordingDatabaseRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        self.revision_reads = 0

    def __call__(self, argv: object, payload: bytes | None) -> CommandResult:
        parts = tuple(argv)  # type: ignore[arg-type]
        self.calls.append((parts, payload))
        if "psql" in parts and "SELECT version_num FROM alembic_version" in parts:
            self.revision_reads += 1
            return CommandResult(
                0, b"20260722_0004\n" if self.revision_reads == 1 else b"20260722_0005\n"
            )
        if "psql" in parts:
            return CommandResult(0, b"t\n")
        return CommandResult(0)


class SuccessfulBackupManager(BackupManager):
    def _migrate_fresh_database(self, database: str) -> CommandResult:
        assert database.startswith("pensae_restore_")
        return CommandResult(0)

    @staticmethod
    def _known_revisions() -> tuple[set[str], str]:
        return {"20260722_0004", "20260722_0005"}, "20260722_0005"


class RestoreCleanupFailureRunner(RecordingDatabaseRunner):
    def __init__(self, *, raise_cleanup: bool) -> None:
        super().__init__()
        self.raise_cleanup = raise_cleanup

    def __call__(self, argv: object, payload: bytes | None) -> CommandResult:
        parts = tuple(argv)  # type: ignore[arg-type]
        if "dropdb" in parts:
            self.calls.append((parts, payload))
            if self.raise_cleanup:
                raise subprocess.SubprocessError("prohibited cleanup detail")
            return CommandResult(1)
        if "pg_restore" in parts and "--list" not in parts:
            self.calls.append((parts, payload))
            return CommandResult(1)
        return super().__call__(parts, payload)


def _settings(tmp_path: Path) -> BootstrapSettings:
    return BootstrapSettings.model_validate(
        {
            "environment": Environment.TEST,
            "data_dir": tmp_path / "data",
            "ownership_dir": tmp_path / "ownership",
        }
    )


def _dump(manager: BackupManager, name: str) -> Path:
    manager.backup_dir.mkdir(parents=True, exist_ok=True)
    path = manager.backup_dir / name
    path.write_bytes(b"PGDMP-valid-fixture")
    return path


def test_rotation_keeps_the_newest_five_valid_custom_dumps(tmp_path: Path) -> None:
    runner = RecordingDatabaseRunner()
    manager = BackupManager(_settings(tmp_path), runner=runner)
    for index in range(7):
        _dump(manager, f"pensae-20260722T12000{index}Z-0000000{index}.dump")
    malformed = manager.backup_dir / "pensae-20260722T130000Z-deadbeef.dump"
    malformed.write_bytes(b"not a dump")

    retained = manager.rotate()

    assert len(retained) == 5
    assert all(path.exists() for path in retained)
    assert malformed.exists()
    assert len(list(manager.backup_dir.glob("*.dump"))) == 6
    assert all(
        call[0][:5] == ("docker", "compose", "exec", "-T", "postgres") for call in runner.calls
    )


def test_rotation_uses_file_chronology_when_names_share_a_timestamp(tmp_path: Path) -> None:
    manager = BackupManager(_settings(tmp_path), runner=RecordingDatabaseRunner())
    dumps = [_dump(manager, f"pensae-20260722T120000Z-0000000{index}.dump") for index in range(7)]
    for index, dump in enumerate(dumps):
        os.utime(dump, ns=(1_000_000_000 + index, 1_000_000_000 + index))

    retained = manager.rotate()

    assert retained == tuple(reversed(dumps[2:]))
    assert all(not dump.exists() for dump in dumps[:2])


def test_restore_rejects_running_app_unsafe_path_and_wrong_confirmation(tmp_path: Path) -> None:
    runner = RecordingDatabaseRunner()
    settings = _settings(tmp_path)
    manager = BackupManager(settings, runner=runner)
    dump = _dump(manager, "pensae-20260722T120000Z-deadbeef.dump")
    ownership = settings.ownership_dir / "app-process.json"
    ownership.parent.mkdir(parents=True)
    ownership.write_text("{}", encoding="utf-8")
    with pytest.raises(DatabaseOperationError, match="make stop"):
        manager.restore(str(dump), f"RESTORE {dump.name}")
    ownership.unlink()
    with pytest.raises(DatabaseOperationError, match="unsafe"):
        manager.restore("../outside.dump", "RESTORE outside.dump")
    with pytest.raises(DatabaseOperationError, match="exactly match"):
        manager.restore(str(dump), "yes")


def test_restore_accepts_exact_documented_make_relative_path_and_other_managed_forms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = BootstrapSettings.model_validate(
        {
            "environment": Environment.TEST,
            "data_dir": Path(".pensae/data"),
            "ownership_dir": Path(".pensae/ownership"),
        }
    )
    runner = RecordingDatabaseRunner()
    manager = SuccessfulBackupManager(
        settings,
        runner=runner,
        now=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    dump = _dump(manager, "pensae-20260722T120000Z-deadbeef.dump")
    documented = Path(".pensae/data/backups") / dump.name

    result = manager.restore(str(documented), f"RESTORE {dump.name}")

    assert result.final_revision == "20260722_0005"
    assert manager._resolve_selected_dump(dump.name) == dump
    assert manager._resolve_selected_dump(str(dump)) == dump


def test_restore_rejects_traversal_symlink_foreign_and_malformed_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = BootstrapSettings.model_validate(
        {
            "environment": Environment.TEST,
            "data_dir": Path(".pensae/data"),
            "ownership_dir": Path(".pensae/ownership"),
        }
    )
    manager = BackupManager(settings, runner=RecordingDatabaseRunner())
    managed = _dump(manager, "pensae-20260722T120000Z-deadbeef.dump")
    foreign = tmp_path / "foreign" / "pensae-20260722T120001Z-deadbeef.dump"
    foreign.parent.mkdir()
    foreign.write_bytes(managed.read_bytes())
    malformed = manager.backup_dir / "manual.dump"
    malformed.write_bytes(managed.read_bytes())
    symlink = manager.backup_dir / "pensae-20260722T120002Z-deadbeef.dump"
    symlink.symlink_to(managed)
    missing = manager.backup_dir / "pensae-20260722T120003Z-deadbeef.dump"
    backup_alias = tmp_path / "backup-alias"
    backup_alias.symlink_to(manager.backup_dir, target_is_directory=True)

    unsafe_values = (
        f".pensae/data/backups/../backups/{managed.name}",
        str(foreign),
        str(malformed),
        str(symlink),
        str(missing),
        str(backup_alias / managed.name),
        "~pensae_restore_user_that_does_not_exist/managed.dump",
    )
    for unsafe in unsafe_values:
        with pytest.raises(
            DatabaseOperationError, match=r"unsafe|regular managed dump|does not exist"
        ):
            manager._resolve_selected_dump(unsafe)


def test_restore_rejects_a_malformed_managed_dump_before_database_creation(
    tmp_path: Path,
) -> None:
    runner = RecordingDatabaseRunner()
    manager = BackupManager(_settings(tmp_path), runner=runner)
    malformed = manager.backup_dir / "pensae-20260722T120000Z-deadbeef.dump"
    malformed.parent.mkdir(parents=True)
    malformed.write_bytes(b"not-a-postgresql-custom-dump")

    with pytest.raises(DatabaseOperationError, match="not a valid PostgreSQL custom dump"):
        manager.restore(malformed.name, f"RESTORE {malformed.name}")

    assert not any("createdb" in call[0] for call in runner.calls)


def test_restore_validates_and_migrates_only_a_generated_fresh_database(tmp_path: Path) -> None:
    runner = RecordingDatabaseRunner()
    manager = SuccessfulBackupManager(
        _settings(tmp_path),
        runner=runner,
        now=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    dump = _dump(manager, "pensae-20260722T120000Z-deadbeef.dump")

    result = manager.restore(dump.name, f"RESTORE {dump.name}")

    assert result.source_revision == "20260722_0004"
    assert result.final_revision == "20260722_0005"
    assert result.database_name.startswith("pensae_restore_20260722_120000_")
    assert any("createdb" in call[0] for call in runner.calls)
    assert any("pg_restore" in call[0] and call[1] == dump.read_bytes() for call in runner.calls)
    assert not any("dropdb" in call[0] for call in runner.calls)


@pytest.mark.parametrize("raise_cleanup", [False, True])
def test_restore_reports_when_generated_database_cleanup_fails(
    tmp_path: Path, *, raise_cleanup: bool
) -> None:
    runner = RestoreCleanupFailureRunner(raise_cleanup=raise_cleanup)
    manager = BackupManager(
        _settings(tmp_path),
        runner=runner,
        now=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    dump = _dump(manager, "pensae-20260722T120000Z-deadbeef.dump")

    with pytest.raises(DatabaseOperationError) as captured:
        manager.restore(dump.name, f"RESTORE {dump.name}")

    drop_call = next(call for call, _payload in runner.calls if "dropdb" in call)
    generated_database = drop_call[-1]
    message = str(captured.value)
    assert generated_database.startswith("pensae_restore_20260722_120000_")
    assert generated_database in message
    assert "may remain" in message
    assert "make stop" in message
    assert "dropdb" in message
    assert str(dump) not in message
    assert "pensae:pensae" not in message
    assert "prohibited cleanup detail" not in message


def test_nonempty_probe_uses_fixed_compose_psql_arguments(tmp_path: Path) -> None:
    runner = RecordingDatabaseRunner()
    manager = BackupManager(_settings(tmp_path), runner=runner)

    assert manager.database_is_nonempty()
    argv = runner.calls[0][0]
    assert argv[:6] == ("docker", "compose", "exec", "-T", "postgres", "psql")
    assert "--no-psqlrc" in argv


def test_backup_timeout_force_kills_and_reaps_its_owned_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class StubbornBackupProcess:
        def __init__(self) -> None:
            self.wait_calls = 0
            self.terminate_calls = 0
            self.kill_calls = 0

        def wait(self, *, timeout: int) -> int:
            self.wait_calls += 1
            if self.wait_calls < 3:
                raise subprocess.TimeoutExpired("pg_dump", timeout)
            return -9

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

    process = StubbornBackupProcess()
    monkeypatch.setattr(database_operations.subprocess, "Popen", lambda *_args, **_kwargs: process)
    manager = BackupManager(_settings(tmp_path), runner=RecordingDatabaseRunner())

    with pytest.raises(DatabaseOperationError, match="exceeded its deadline"):
        manager.backup()

    assert process.wait_calls == 3
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert list(manager.backup_dir.iterdir()) == []
