"""Phase 6.6.7: explicit SQLite snapshot creation and isolated restore testing.

No migration SQL or restoration over the source database is performed.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from aurora.database.migration_records import BackupManifest, verify_backup, _digest


@dataclass(frozen=True)
class RestoreValidation:
    ok: bool
    reason: str
    table_count: int = 0


def _integrity(connection: sqlite3.Connection) -> bool:
    return connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def validate_sqlite_restore(backup_path: str | Path, manifest: BackupManifest, *,
                            expected_plan_sha256: str | None = None) -> RestoreValidation:
    """Verify bytes, restore into an isolated temporary database, check integrity.

    Never replaces the source database. Does not prove application-level semantics.
    The backup must remain stable while this function runs.
    """
    verification = verify_backup(backup_path, manifest,
                                 expected_plan_sha256=expected_plan_sha256)
    if not verification.ok:
        return RestoreValidation(False, verification.reason)
    try:
        with TemporaryDirectory(prefix='aurora-restore-check-') as tmp:
            restored = Path(tmp) / 'restored.sqlite3'
            uri = Path(backup_path).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source:
                with closing(sqlite3.connect(str(restored), timeout=5)) as target:
                    source.backup(target)
                    if not _integrity(target):
                        return RestoreValidation(False, 'restore_integrity_failed')
                    tables = target.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                    return RestoreValidation(True, 'isolated_restore_verified', tables)
    except (sqlite3.Error, OSError, ValueError):
        return RestoreValidation(False, 'restore_failed')


def create_sqlite_backup(source_path: str | Path, destination_path: str | Path, *,
                         plan_sha256: str) -> BackupManifest:
    """Create a consistent SQLite snapshot at a NEW path; never overwrite files.

    SQLite's backup API handles committed WAL data. Caller must protect the
    destination directory against untrusted concurrent writers and retain the
    manifest separately. The source database is opened in read-only mode.
    """
    _digest(plan_sha256, 'plan_sha256')
    source = Path(source_path).resolve(strict=True)
    if not source.is_file():
        raise ValueError('source must be an existing regular file')
    destination = Path(destination_path).absolute()
    if source == destination.resolve():
        raise ValueError('backup destination must differ from source')
    if not destination.parent.is_dir():
        raise FileNotFoundError('destination parent directory does not exist')
    # Reserve the destination atomically. Existing files and symlinks are rejected.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        uri = source.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as live:
            with closing(sqlite3.connect(str(destination), timeout=5)) as backup:
                live.backup(backup)
                if not _integrity(backup):
                    raise ValueError('SQLite backup failed integrity_check')
        digest = sha256()
        size = 0
        with destination.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
                size += len(chunk)
        manifest = BackupManifest(destination.name, size, digest.hexdigest(), plan_sha256)
        validation = validate_sqlite_restore(destination, manifest,
                                             expected_plan_sha256=plan_sha256)
        if not validation.ok:
            raise ValueError('backup restore validation failed: ' + validation.reason)
        return manifest
    except BaseException:
        # We own this newly created path; close SQLite connections before removal.
        destination.unlink(missing_ok=True)
        raise
