"""Safe fixed-argv PostgreSQL backup and fresh-database restore."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory

from pensae.config.settings import BootstrapSettings

_DUMP_NAME = re.compile(r"^pensae-\d{8}T\d{6}Z-[0-9a-f]{8}\.dump$")
_SQL_NONEMPTY = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
    "WHERE c.relkind='r' AND n.nspname='public' AND c.relname <> 'alembic_version')"
)
_SQL_REVISION = "SELECT version_num FROM alembic_version"


class DatabaseOperationError(RuntimeError):
    """A safe local database operation was refused or failed."""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    database_name: str
    source_revision: str
    final_revision: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""


CommandRunner = Callable[[Sequence[str], bytes | None], CommandResult]
FileCommandRunner = Callable[[Sequence[str], Path], CommandResult]


def _run_command(argv: Sequence[str], input_bytes: bytes | None = None) -> CommandResult:
    if not argv or any(not isinstance(part, str) or not part or "\x00" in part for part in argv):
        raise ValueError("database command argv must contain safe non-empty elements")
    completed = subprocess.run(  # noqa: S603 - callers supply closed fixed-argv commands.
        tuple(argv),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        timeout=300,
    )
    return CommandResult(completed.returncode, completed.stdout)


def _run_file_command(argv: Sequence[str], input_path: Path) -> CommandResult:
    if not argv or any(not isinstance(part, str) or not part or "\x00" in part for part in argv):
        raise ValueError("database command argv must contain safe non-empty elements")
    with input_path.open("rb") as input_file:
        completed = subprocess.run(  # noqa: S603 - callers supply closed fixed-argv commands.
            tuple(argv),
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[3],
            check=False,
            timeout=300,
        )
    return CommandResult(completed.returncode, completed.stdout)


class BackupManager:
    """Create, validate, rotate, and restore only PostgreSQL custom dumps."""

    def __init__(
        self,
        settings: BootstrapSettings,
        *,
        runner: CommandRunner = _run_command,
        file_runner: FileCommandRunner | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._file_runner = file_runner or (
            _run_file_command
            if runner is _run_command
            else lambda argv, path: runner(argv, path.read_bytes())
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._backup_dir = settings.data_dir.expanduser().resolve() / "backups"
        if self._backup_dir == Path("/"):
            raise ValueError("backup directory cannot be filesystem root")
        parsed = urlsplit(
            settings.postgres_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        )
        self._user = parsed.username or "pensae"
        self._database = parsed.path.removeprefix("/") or "pensae"

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    def database_is_nonempty(self) -> bool:
        result = self._run_psql(self._database, _SQL_NONEMPTY)
        if result.returncode != 0:
            raise DatabaseOperationError("could not inspect the PostgreSQL migration state")
        return result.stdout.strip() == b"t"

    def backup(self, *, only_if_nonempty: bool = False) -> Path | None:
        if only_if_nonempty and not self.database_is_nonempty():
            return None
        self._backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._backup_dir, 0o700)
        stamp = self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = self._backup_dir / f"pensae-{stamp}-{uuid4().hex[:8]}.dump"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=self._backup_dir
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                process = subprocess.Popen(  # noqa: S603 - _compose returns a fixed argv prefix.
                    self._compose(
                        "pg_dump",
                        f"--username={self._user}",
                        f"--dbname={self._database}",
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                    ),
                    cwd=Path(__file__).resolve().parents[3],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    returncode = process.wait(timeout=300)
                except subprocess.TimeoutExpired as exc:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired as kill_exc:
                            raise DatabaseOperationError(
                                "timed-out PostgreSQL backup child could not be reaped"
                            ) from kill_exc
                    raise DatabaseOperationError("PostgreSQL backup exceeded its deadline") from exc
                output.flush()
                os.fsync(output.fileno())
            if returncode != 0 or temporary.stat().st_size == 0:
                raise DatabaseOperationError("PostgreSQL custom-format backup failed")
            if not self._validate_dump(temporary):
                raise DatabaseOperationError("PostgreSQL backup validation failed")
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            self.rotate()
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def rotate(self) -> tuple[Path, ...]:
        if not self._backup_dir.exists():
            return ()
        valid = [
            path
            for path in self._backup_dir.iterdir()
            if self._is_safe_dump_path(path) and self._validate_dump(path)
        ]
        valid.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        for old in valid[5:]:
            old.unlink()
        return tuple(valid[:5])

    def restore(self, backup_value: str, confirmation: str) -> RestoreResult:
        if self._app_ownership_path().exists():
            raise DatabaseOperationError(
                "Pensae Signal ownership exists; run `make stop` before restore"
            )
        selected = self._resolve_selected_dump(backup_value)
        expected_confirmation = f"RESTORE {selected.name}"
        if confirmation != expected_confirmation:
            raise DatabaseOperationError(
                f"typed confirmation must exactly match: {expected_confirmation}"
            )
        if not self._validate_dump(selected):
            raise DatabaseOperationError("selected backup is not a valid PostgreSQL custom dump")
        fresh_database = (
            f"pensae_restore_{self._now().astimezone(UTC):%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
        )
        created = self._runner(
            self._compose("createdb", f"--username={self._user}", fresh_database), None
        )
        if created.returncode != 0:
            raise DatabaseOperationError("could not create the fresh restore database")
        try:
            restored = self._file_runner(
                self._compose(
                    "pg_restore",
                    f"--username={self._user}",
                    f"--dbname={fresh_database}",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                ),
                selected,
            )
            if restored.returncode != 0:
                raise DatabaseOperationError("restore into the fresh database failed")
            revision_result = self._run_psql(fresh_database, _SQL_REVISION)
            if revision_result.returncode != 0:
                raise DatabaseOperationError("restored database has no readable migration revision")
            source_revision = revision_result.stdout.decode("ascii", errors="strict").strip()
            known_revisions, current_head = self._known_revisions()
            if source_revision not in known_revisions:
                raise DatabaseOperationError("restored database revision is incompatible")
            migrated = self._migrate_fresh_database(fresh_database)
            if migrated.returncode != 0:
                raise DatabaseOperationError("fresh restored database migration failed")
            final = self._run_psql(fresh_database, _SQL_REVISION)
            final_revision = final.stdout.decode("ascii", errors="strict").strip()
            if final.returncode != 0 or final_revision != current_head:
                raise DatabaseOperationError(
                    "fresh restored database did not reach the current head"
                )
            return RestoreResult(fresh_database, source_revision, final_revision)
        except BaseException as restore_error:
            try:
                cleanup = self._runner(
                    self._compose("dropdb", f"--username={self._user}", fresh_database), None
                )
                cleanup_failed = cleanup.returncode != 0
            except BaseException:
                cleanup_failed = True
            if cleanup_failed:
                raise DatabaseOperationError(
                    "restore failed and automatic cleanup failed; generated database "
                    f"{fresh_database} may remain. Run `make stop`, verify Pensae Signal is "
                    "stopped, "
                    "then inspect and remove that database using the documented dropdb procedure"
                ) from restore_error
            raise

    def _migrate_fresh_database(self, database: str) -> CommandResult:
        parsed = urlsplit(
            self._settings.postgres_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        )
        fresh_path = f"/{database}"
        fresh_dsn = urlunsplit(parsed._replace(path=fresh_path)).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        environment = dict(os.environ)
        environment["PENSAE_POSTGRES_DSN"] = fresh_dsn
        completed = subprocess.run(
            (sys.executable, "-m", "alembic", "upgrade", "head"),
            cwd=Path(__file__).resolve().parents[3],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
        )
        return CommandResult(completed.returncode)

    def _validate_dump(self, path: Path) -> bool:
        with path.open("rb") as dump:
            if dump.read(5) != b"PGDMP":
                return False
        result = self._file_runner(self._compose("pg_restore", "--list"), path)
        return result.returncode == 0

    def _resolve_selected_dump(self, value: str) -> Path:
        if not value or "\x00" in value:
            raise DatabaseOperationError("backup path is missing or unsafe")
        try:
            supplied = Path(value).expanduser()
        except RuntimeError as exc:
            raise DatabaseOperationError("backup path is missing or unsafe") from exc
        if ".." in supplied.parts:
            raise DatabaseOperationError("backup path is missing or unsafe")
        if supplied.is_absolute():
            candidate = supplied
        elif supplied.parent == Path("."):
            candidate = self._backup_dir / supplied.name
        else:
            candidate = Path.cwd() / supplied
        candidate = Path(os.path.abspath(candidate))
        if candidate.parent != self._backup_dir or candidate.is_symlink():
            raise DatabaseOperationError("backup path must name a regular managed dump")
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise DatabaseOperationError("backup path does not exist") from exc
        if resolved != candidate or not self._is_safe_dump_path(resolved):
            raise DatabaseOperationError("backup path must name a regular managed dump")
        return resolved

    def _is_safe_dump_path(self, path: Path) -> bool:
        return (
            path.parent == self._backup_dir
            and _DUMP_NAME.fullmatch(path.name) is not None
            and path.is_file()
            and not path.is_symlink()
        )

    def _run_psql(self, database: str, query: str) -> CommandResult:
        return self._runner(
            self._compose(
                "psql",
                f"--username={self._user}",
                f"--dbname={database}",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--command",
                query,
            ),
            None,
        )

    @staticmethod
    def _compose(*database_argv: str) -> tuple[str, ...]:
        return ("docker", "compose", "exec", "-T", "postgres", *database_argv)

    def _app_ownership_path(self) -> Path:
        return self._settings.ownership_dir.expanduser().resolve() / "app-process.json"

    @staticmethod
    def _known_revisions() -> tuple[set[str], str]:
        configuration = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
        script = ScriptDirectory.from_config(configuration)
        heads = script.get_heads()
        if len(heads) != 1:
            raise DatabaseOperationError("repository must have exactly one migration head")
        return {revision.revision for revision in script.walk_revisions()}, heads[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pensae Signal native-Fedora PostgreSQL backup and restore"
    )
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("--if-nonempty", action="store_true")
    args = parser.parse_args(argv)
    manager = BackupManager(BootstrapSettings())
    try:
        if args.command == "backup":
            result = manager.backup(only_if_nonempty=args.if_nonempty)
            print(
                "backup skipped: database is empty"
                if result is None
                else f"backup created: {result}"
            )
        else:
            backup = os.environ.get("PENSAE_RESTORE_BACKUP", "")
            confirmation = os.environ.get("PENSAE_RESTORE_CONFIRMATION", "")
            result = manager.restore(backup, confirmation)
            print(
                "restore completed into fresh database "
                f"{result.database_name} at revision {result.final_revision}"
            )
    except (DatabaseOperationError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"database operation refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
