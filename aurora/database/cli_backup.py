"""Guarded SQLite, MySQL and PostgreSQL backup command helpers.

Never overwrite destination artifacts; restore execution remains disabled.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aurora.core.project_policy import project_is_production

# SQLite catalog helpers remain importable for existing integrations.
from .backup_catalog import (
    _sqlite_catalog_format as _catalog_format,
    _sqlite_catalog_exists as _catalog_exists,
    _sqlite_manifest_rows as _manifest_rows,
    _sqlite_read_rows as _read_rows,
    inspect_sqlite_catalog,
    reconcile_sqlite_catalog,
)
from .backup_catalog import _root, list_artifacts
from .cli_check import _load_config, _existing_sqlite_path
from .migration_history import sqlite_has_forward_history as has_forward_history
from .migration_history import sqlite_read_history as read_history


def _sqlite_source(url, config_dir):
    from .backups import _sqlite_source as implementation

    return implementation(url, config_dir)


def _integrity(connection):
    from .backups import _integrity as implementation

    return implementation(connection)


def create_managed_sqlite_backup(args):
    from .backups import create_managed_sqlite_backup as implementation

    return implementation(args)


def run_backup(args):
    """Public backup reports immutable artifact identity, without user paths."""
    destination = create_managed_sqlite_backup(args)
    backup_id = destination.stem
    print(
        json.dumps(
            {
                "status": "BACKED_UP",
                "database_alias": args.database,
                "backup_id": backup_id,
                "destination": str(destination),
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


# Server backup clients use separate credential and evidence safeguards.

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _run(command: list[str], *, label: str) -> subprocess.CompletedProcess:
    from .backups import _run as implementation

    return implementation(command, label=label)


def _client_authentication(url, login_path):
    from .backups import _client_authentication as implementation

    return implementation(url, login_path)


def backup_mysql_database(
    config_path: str,
    alias: str,
    destination: str | None,
    login_path: str | None,
    expected_url: str | None = None,
) -> Path:
    from .backups import backup_mysql_database as implementation

    return implementation(config_path, alias, destination, login_path, expected_url)


def _backup_selected_mysql_database(url, alias, destination, database, host, port, authentication):
    from .backups import _backup_selected_mysql_database as implementation

    return implementation(url, alias, destination, database, host, port, authentication)


# PostgreSQL backup client.

_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _run_client(arguments, *, label):
    from .backups import _run_client as implementation

    return implementation(arguments, label=label)


def backup_postgres_database(config_path, alias, destination, expected_url=None):
    from .backups import backup_postgres_database as implementation

    return implementation(config_path, alias, destination, expected_url)


# Read-only restore preflight; restore execution is disabled.
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

    with closing(
        sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    ) as db:
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            raise ValueError("WAL-mode target requires offline checkpoint and mode change")

    return {
        "database_alias": alias,
        "target": str(target),
        "backup": str(backup),
        "status": "PREFLIGHT_ONLY",
        "restore_authorized": False,
    }


# Read-only MySQL restore preflight; no dump SQL execution.


@dataclass(frozen=True)
class MySQLRestoreReport:
    alias: str
    target: str
    staging: str
    backup: Path
    size_bytes: int
    table_count: int
    staging_available: bool

    def format_text(self) -> str:
        state = "PASS" if self.staging_available else "BLOCKED"
        return (
            f"MySQL restore preflight (READ ONLY)\n"
            f"PASS: Backup is a nonempty regular .sql file with a completion footer ({self.size_bytes} bytes).\n"
            f"PASS: Connected database and account match alias '{self.alias}' (target: {self.target}).\n"
            f"PASS: Target metadata is readable ({self.table_count} base tables).\n"
            f"{state}: Staging database '{self.staging}' "
            f"{'does not exist' if self.staging_available else 'already exists; choose another name'}.\n"
            "UNVERIFIED: Dump provenance, SQL safety, logical consistency, privileges for future writes, "
            "application downtime, and cutover safety.\n"
            "NO SQL EXECUTED. No database changes made. All restore execution, including staging imports, is disabled. This report does not authorize recovery writes."
        )


def _backup_file(source: str) -> tuple[Path, int]:
    candidate = Path(source).expanduser()

    if candidate.is_symlink():
        raise ValueError("MySQL restore backup symlinks are not supported")

    try:
        backup = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("MySQL restore backup file not found") from None

    if not backup.is_file() or backup.suffix.lower() != ".sql":
        raise ValueError("MySQL restore requires an existing regular .sql backup file")

    size = backup.stat().st_size

    if size == 0:
        raise ValueError("MySQL restore backup file is empty")

    with backup.open("rb") as stream:
        head = stream.read(4096)
        stream.seek(max(0, size - 4096))
        tail = stream.read()
    # Heuristics only: a completion marker does NOT authenticate a dump.
    if b"MySQL dump" not in head or b"-- Dump completed on" not in tail:
        raise ValueError("MySQL restore requires a completed mysqldump-style backup")

    return backup, size


def check_mysql_restore(
    config_path: str,
    alias: str,
    source: str,
    login_path: str | None,
    staging_database: str | None = None,
) -> MySQLRestoreReport:
    """Inspect MySQL recovery prerequisites without executing a restore."""

    if not login_path or not _IDENTIFIER.fullmatch(login_path):
        raise ValueError("MySQL restore preflight requires --login-path NAME")

    config, _ = _load_config(config_path)
    url = urlsplit(config.resolve(alias))

    if url.scheme != "mysql+pymysql":
        raise ValueError("MySQL restore preflight requires a MySQL database alias")

    target = unquote(url.path.lstrip("/"))

    if not target or not _IDENTIFIER.fullmatch(target) or not url.username or not url.hostname:
        raise ValueError(
            "MySQL restore preflight requires a simple database name, username, and host"
        )

    stage = staging_database or f"{target}_aurora_stage"

    if not _IDENTIFIER.fullmatch(stage) or len(stage) > 64 or stage == target:
        raise ValueError(
            "Invalid staging database name; use a distinct identifier of at most 64 characters"
        )

    backup, size = _backup_file(source)
    connection = [
        f"--login-path={login_path}",
        "--protocol=TCP",
        f"--host={url.hostname}",
        f"--port={url.port or 3306}",
        f"--user={unquote(url.username)}",
    ]
    probe = _run(
        [
            "mysql",
            *connection,
            "--batch",
            "--skip-column-names",
            target,
            "--execute=SELECT DATABASE(), CURRENT_USER()",
        ],
        label="restore connection check",
    )
    fields = probe.stdout.strip().split("\t")

    if len(fields) != 2 or fields[0] != target or fields[1].split("@")[0] != unquote(url.username):
        raise ValueError("MySQL restore connection does not match configured database and account")

    metadata = _run(
        [
            "mysql",
            *connection,
            "--batch",
            "--skip-column-names",
            target,
            "--execute=SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'",
        ],
        label="restore metadata inspection",
    )

    try:
        count = int(metadata.stdout.strip())
    except ValueError:
        raise ValueError("MySQL restore target metadata could not be verified") from None
    # Name is validated before interpolation; no untrusted SQL fragments.
    staging = _run(
        [
            "mysql",
            *connection,
            "--batch",
            "--skip-column-names",
            target,
            "--execute=SELECT COUNT(*) FROM information_schema.SCHEMATA "
            f"WHERE SCHEMA_NAME = '{stage}'",
        ],
        label="staging collision inspection",
    )

    if staging.stdout.strip() not in ("0", "1"):
        raise ValueError("MySQL restore staging availability could not be verified")

    return MySQLRestoreReport(
        alias, target, stage, backup, size, count, staging.stdout.strip() == "0"
    )
