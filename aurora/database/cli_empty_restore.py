"""Restore a verified forward-history SQLite backup to an absent database file.

No overwrite, no implicit snapshot reconstruction, and no server-backend restore.
The existing project migration snapshots must accompany the backup.
"""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config
from .sqlite_forward_history import read_history


def _target(url, root):
    parts = urlsplit(url)
    if parts.scheme not in ("sqlite", "sqlite+pysqlite") or parts.netloc or parts.query or parts.fragment:
        raise ValueError("Restore supports only a configured SQLite file")
    raw = unquote(parts.path)
    if raw in ("/:memory:", "//:memory:"):
        raise ValueError("In-memory databases cannot be restored")
    if raw.startswith("//"):
        path = Path(raw[1:]).expanduser()
    elif raw.startswith("/"):
        path = root / raw[1:]
    else:
        raise ValueError("Invalid SQLite database path")
    return path.absolute()


def _no_sidecars(path):
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError("Database has SQLite journal/WAL files; restore refused")


def restore_empty_sqlite(config_path, alias, source):
    """Publish a consistent backup only when the configured target is absent."""
    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise ValueError("Restore in production requires separate approval")
    config, root = _load_config(config_path)
    target = _target(config.resolve(alias), root)
    backup = Path(source).expanduser().absolute()
    if target == backup or target.resolve() == backup.resolve():
        raise ValueError("Restore source and destination must be different")
    if (target.exists() or target.is_symlink()):
        raise FileExistsError("Target database already exists; no files were changed")
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise ValueError("Target directory must already exist")
    if backup.is_symlink() or not backup.is_file():
        raise ValueError("Backup must be an existing regular SQLite file")
    _no_sidecars(target)
    _no_sidecars(backup)

    # Read-only SQLite opening never creates a missing/invalid input database.
    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as old:
        if old.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup failed SQLite integrity verification")
        if old.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Backup contains foreign-key violations")
        # The snapshot files are external to .sqlite3 backups; never restore a
        # forward-history DB that would immediately fail its own history checks.
        read_history(old, root, target)
        # O_EXCL prevents replacing an existing database or filesystem symlink.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        succeeded = False
        try:
            with closing(sqlite3.connect(target)) as restored:
                old.backup(restored)
                if restored.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Restored database failed integrity verification")
                read_history(restored, root, target)
            succeeded = True
        finally:
            if not succeeded:
                target.unlink(missing_ok=True)
    print(f"Restored database '{alias}' from verified backup to {target}.")
    return 0
