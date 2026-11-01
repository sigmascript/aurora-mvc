"""Read-only AuroraSQL SQLite restore preflight.

This module does not authorize or execute restoration. A future restore
executor must revalidate after obtaining its exclusive recovery lock.
"""

from contextlib import closing
from pathlib import Path
import sqlite3

from .cli_plan import _load_config, _existing_sqlite_path


def _validate_sqlite_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Restore input must be an existing regular file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Restore input failed SQLite integrity check")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Restore input failed SQLite quick check")


def check_sqlite_restore(config_path, alias, backup_path):
    """Reject unsafe offline restore conditions without changing either file."""
    config, base = _load_config(config_path)
    target = _existing_sqlite_path(config.resolve(alias), base)
    backup = Path(backup_path).expanduser().absolute()
    if target.is_symlink() or backup.is_symlink():
        raise ValueError("Restore does not accept symbolic links")
    if target.resolve() == backup.resolve():
        raise ValueError("Backup must differ from target")
    for candidate, label in ((backup, "backup"), (target, "target")):
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(candidate) + suffix).exists() or Path(str(candidate) + suffix).is_symlink():
                raise ValueError(f"Restore {label} has a journal or WAL sidecar")
    _validate_sqlite_file(backup)
    _validate_sqlite_file(target)
    with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            raise ValueError("WAL-mode target requires offline checkpoint and mode change")
    return {"database_alias": alias, "target": str(target), "backup": str(backup),
            "status": "PREFLIGHT_ONLY", "restore_authorized": False}
