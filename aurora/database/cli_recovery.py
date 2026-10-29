"""Guarded, offline SQLite backup and restore commands.

Restoration requires the application to be stopped. SQLite file locking checks
are best-effort and cannot prevent a process starting after the check.
"""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone
from secrets import token_hex

from aurora.database.cli_inspection import load_cli_database_config
from aurora.database.sqlite_backup import create_sqlite_backup


_STANDALONE_PLAN = sha256(b"aurora-standalone-backup-v1").hexdigest()


def _database(config_path: str, alias: str) -> Path:
    config, base = load_cli_database_config(config_path)
    url = config.resolve(alias)
    if url.get_backend_name() != "sqlite":
        raise ValueError("Standalone backup and restore support file-backed SQLite only")
    if not url.database or url.database == ":memory:":
        raise ValueError("An existing file-backed SQLite database is required")
    path = Path(url.database).expanduser()
    path = path if path.is_absolute() else base / path
    if path.is_symlink():
        raise ValueError("Database symlinks are not supported for recovery")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database must be an existing regular file")
    return path


def _validated_snapshot(path: Path) -> None:
    """Reject damaged backups and databases without creating them."""
    if not path.is_file() or path.is_symlink():
        raise ValueError("Backup must be an existing regular file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite quick check failed")


def _offline_target(path: Path) -> None:
    """Refuse WAL sidecars and databases held by another SQLite writer."""
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise ValueError("Database has journal/WAL sidecar; stop all processes and checkpoint it first")
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            raise ValueError("WAL-mode database cannot be restored offline; checkpoint and switch journal mode first")
        try:
            db.execute("BEGIN EXCLUSIVE")
            db.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise ValueError("Database is in use; stop the application before restoring") from exc
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise ValueError("Database journal/WAL sidecar appeared during validation")


def backup_database(config_path: str, alias: str, destination: str | None = None,
                    *, login_path: str | None = None) -> Path:
    config, _ = load_cli_database_config(config_path)
    if config.resolve(alias).get_backend_name() == "mysql":
        from aurora.database.cli_mysql_backup import backup_mysql_database
        return backup_mysql_database(config_path, alias, destination, login_path)
    target = _database(config_path, alias)
    if destination is None:
        folder = Path.cwd() / "backups"
        folder.mkdir(exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = str(folder / f"{alias}-{name}-{token_hex(4)}.sqlite3")
    output = Path(destination).expanduser().absolute()
    create_sqlite_backup(target, output, plan_sha256=_STANDALONE_PLAN)
    return output


def restore_database(config_path: str, alias: str, source: str, confirm) -> tuple[Path, Path] | None:
    """Interactive offline restore; preserves a recovery copy before replacement.

    Caller must ensure the application is stopped for the *entire* operation.
    A lock check alone cannot enforce that requirement against uncooperative processes.
    """
    target = _database(config_path, alias)
    backup = Path(source).expanduser()
    if backup.is_symlink():
        raise ValueError("Backup symlinks are not supported for recovery")
    backup = backup.resolve(strict=True)
    if backup == target:
        raise ValueError("Backup and target database must differ")
    _validated_snapshot(backup)
    _offline_target(target)
    print(f"Database alias: {alias}\nTarget: {target}\nBackup: {backup}")
    print("WARNING: This will replace all current database records and schema.")
    print("Stop the application and all other database users before proceeding.")
    if not confirm():
        print("Restore cancelled; target unchanged.")
        return None
    # Prevent accidental concurrent invocations of this Aurora command.
    lock = Path(str(target) + ".aurora-restore.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    recovery = target.with_name(target.name + ".pre-restore.sqlite3")
    stage = None
    replaced = False
    try:
        _offline_target(target)
        _validated_snapshot(backup)
        # Recovery snapshot must never overwrite an earlier recovery artifact.
        create_sqlite_backup(target, recovery, plan_sha256=_STANDALONE_PLAN)
        # Materialize the validated backup in the target directory, then swap.
        with tempfile.NamedTemporaryFile(prefix=".aurora-restore-", suffix=".sqlite3",
                                         dir=target.parent, delete=False) as temporary:
            stage = Path(temporary.name)
        try:
            with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True, timeout=2)) as src:
                with closing(sqlite3.connect(str(stage), timeout=2)) as dst:
                    src.backup(dst)
            _validated_snapshot(stage)
            _offline_target(target)
            # Replacing a closed SQLite file is atomic on the same filesystem.
            os.replace(stage, target)
            replaced = True
            stage = None
            _validated_snapshot(target)
        except BaseException:
            if replaced:
                # Keep recovery file intact; attempt a fresh staging copy.
                try:
                    with tempfile.NamedTemporaryFile(prefix=".aurora-recover-", suffix=".sqlite3",
                                                     dir=target.parent, delete=False) as temporary:
                        rollback_stage = Path(temporary.name)
                    try:
                        with closing(sqlite3.connect(recovery.as_uri() + "?mode=ro", uri=True)) as src:
                            with closing(sqlite3.connect(str(rollback_stage))) as dst:
                                src.backup(dst)
                        _validated_snapshot(rollback_stage)
                        os.replace(rollback_stage, target)
                    finally:
                        rollback_stage.unlink(missing_ok=True)
                except BaseException as rollback_error:
                    raise RuntimeError(f"Restore failed and automatic recovery failed; preserved snapshot: {recovery}") from rollback_error
            raise
        return target, recovery
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
