"""Read-only inspection of migration history stored in the target SQLite database."""
from __future__ import annotations
from contextlib import closing
from pathlib import Path
import sqlite3
from aurora.database.cli_inspection import _sqlite_path

_HISTORY = '_aurora_migration_history'
_COLUMNS = ('plan_sha256', 'database_alias', 'applied_at_utc', 'backup_filename', 'backup_sha256')


def read_applied_migrations(config, alias: str, *, base_dir: Path):
    """Return (exists, records) without creating SQLite files or tables.

    The database is opened with mode=ro. An absent history table is normal for
    databases that have not yet received an Aurora migration.
    """
    path = _sqlite_path(config, alias, base_dir).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('SQLite database path is not a regular file')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_HISTORY,)).fetchone()
        if not present:
            return False, ()
        quoted = '"' + _HISTORY + '"'
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({quoted})')}
        if not set(_COLUMNS).issubset(columns):
            raise ValueError('Target migration history table has an incompatible schema')
        records = conn.execute(
            f'SELECT {", ".join(_COLUMNS)} FROM {quoted} ORDER BY applied_at_utc, plan_sha256'
        ).fetchall()
        if any(record[1] != alias for record in records):
            raise ValueError('Migration history contains records for a different database alias')
        return True, tuple(records)
