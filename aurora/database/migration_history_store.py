"""Phase 6.6.8: explicitly initialized, separate SQLite audit history.

This module never opens or modifies the application database. The caller must
provide a dedicated history path and explicitly initialize it. It is not an
execution approval mechanism and does not execute migration SQL.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from aurora.database.migration_records import MigrationHistoryRecord

_SCHEMA = """CREATE TABLE migration_history (
 migration_id TEXT PRIMARY KEY NOT NULL,
 plan_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('planned','applied','failed','rolled_back')),
 recorded_at_utc TEXT NOT NULL,
 backup_sha256 TEXT
)"""


def initialize_history(path: str | Path) -> None:
    """Explicitly create a NEW, dedicated history file; never overwrite."""
    target = Path(path)
    if target.exists():
        raise FileExistsError(f'history file already exists: {target}')
    if not target.parent.is_dir():
        raise FileNotFoundError(f'history parent directory missing: {target.parent}')
    # mode=rwc could open an existing file; x mode reserves the name first.
    with target.open('xb'):
        pass
    try:
        with closing(sqlite3.connect(target)) as conn:
            with conn:
                conn.execute(_SCHEMA)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _open_existing(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f'history database missing: {target}')
    # URI mode=rw rejects missing paths; don't accidentally create a new DB.
    return sqlite3.connect(target.resolve().as_uri() + '?mode=rw', uri=True)


def append_history(path: str | Path, record: MigrationHistoryRecord) -> None:
    """Insert one immutable audit row. Duplicate IDs are rejected."""
    if not isinstance(record, MigrationHistoryRecord):
        raise TypeError('record must be a MigrationHistoryRecord')
    with closing(_open_existing(path)) as conn:
        with conn:
            conn.execute("""INSERT INTO migration_history
                (migration_id, plan_sha256, state, recorded_at_utc, backup_sha256)
                VALUES (?, ?, ?, ?, ?)""",
                (record.migration_id, record.plan_sha256, record.state,
                 record.recorded_at_utc, record.backup_sha256))


def read_history(path: str | Path) -> tuple[MigrationHistoryRecord, ...]:
    """Read immutable records without creating a missing database."""
    with closing(_open_existing(path)) as conn:
        rows = conn.execute("""SELECT migration_id, plan_sha256, state, recorded_at_utc,
                        backup_sha256 FROM migration_history ORDER BY rowid""").fetchall()
    return tuple(MigrationHistoryRecord(*row) for row in rows)
