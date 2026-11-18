"""Fail-closed AuroraSQL SQLite backup; never overwrites a destination.

Restore execution remains disabled; other backends have separate guarded clients.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config
from .sqlite_forward_history import has_forward_history, read_history


def _sqlite_source(url, config_dir):
    parsed = urlsplit(url)

    if parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Only SQLite backup is currently supported")

    if parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Unsupported SQLite URL")

    path = unquote(parsed.path)

    if path == "/:memory:":
        raise ValueError("In-memory SQLite cannot be backed up with this command")

    name = path[2:] if path.startswith("///") else path[1:]
    source = Path(name).expanduser()

    if not source.is_absolute():
        source = config_dir / source

    if source.is_symlink():
        raise ValueError("SQLite backup source must not be a symbolic link")

    if not source.is_file():
        raise ValueError("SQLite source file does not exist")

    return source.resolve(strict=True)


def _integrity(connection):
    rows = connection.execute("PRAGMA integrity_check").fetchall()

    if rows != [("ok",)]:
        raise ValueError("SQLite integrity check failed")


def create_managed_sqlite_backup(args):
    """Create one verified managed artifact; return its immutable path."""

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise ValueError("AuroraSQL backup in production requires separate approval")

    from .backup_artifacts import (prepare_artifact, record_artifact,
                                   destination_fingerprint, validate_title)

    validate_title(getattr(args, "title", None))
    config, config_dir = _load_config(args.config)
    url = config.resolve(args.database)
    source = _sqlite_source(url, config_dir)
    # A new backup must not conceal conflicting or damaged older evidence.
    from .backup_artifacts import list_artifacts
    from .sqlite_backup_catalog import inspect_sqlite_catalog, _catalog_format
    identity = destination_fingerprint(url, config_dir)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        if _catalog_format(db) == "legacy":
            raise ValueError("Legacy __backups__ catalog requires an explicitly reviewed transition")
    existing = list_artifacts(config_dir, args.database, identity)
    existing = inspect_sqlite_catalog(source, config_dir, args.database, identity, existing)
    if any(item["status"] != "ARTIFACT_VERIFIED" for item in existing):
        raise ValueError("Existing backup evidence requires manual reconciliation")
    backup_id, destination = prepare_artifact(config_dir, args.database, "sqlite")

    if destination.suffix != ".sqlite3":
        raise ValueError("Backup destination must end in .sqlite3")

    if not destination.parent.is_dir():
        raise ValueError("Backup destination parent directory must exist")

    if destination.resolve() == source:
        raise ValueError("Backup destination must differ from source")
    # O_EXCL rejects existing files and symlinks; never overwrite user data.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    completed = False

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            _integrity(original)
            if has_forward_history(original):
                read_history(original, config_dir, source)

            with closing(sqlite3.connect(destination)) as copy:
                with copy:
                    original.backup(copy)
                    _integrity(copy)
                    if has_forward_history(copy):
                        read_history(copy, config_dir, source)

        completed = True
    finally:
        if not completed:
            destination.unlink(missing_ok=True)

    identity = destination_fingerprint(url, config_dir)
    record_artifact(config_dir, args.database, "sqlite", backup_id, destination,
                    getattr(args, "title", None), identity)
    from .sqlite_backup_catalog import reconcile_sqlite_catalog
    reconcile_sqlite_catalog(source, config_dir, args.database, identity)
    return destination


def run_backup(args):
    """Public backup reports immutable artifact identity, without user paths."""
    destination = create_managed_sqlite_backup(args)
    backup_id = destination.stem
    print(json.dumps({"status": "BACKED_UP", "database_alias": args.database,
                      "backup_id": backup_id, "destination": str(destination),
                      "verified": True}, sort_keys=True))
    return 0
