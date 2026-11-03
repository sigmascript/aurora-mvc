"""Read-only MySQL restore preflight. This module never executes dump SQL."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from aurora.database.cli_plan import _load_config
from .cli_mysql_backup import _run

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


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
        return (f"MySQL restore preflight (READ ONLY)\n"
                f"PASS: Backup is a nonempty regular .sql file with a completion footer ({self.size_bytes} bytes).\n"
                f"PASS: Connected database and account match alias '{self.alias}' (target: {self.target}).\n"
                f"PASS: Target metadata is readable ({self.table_count} base tables).\n"
                f"{state}: Staging database '{self.staging}' "
                f"{'does not exist' if self.staging_available else 'already exists; choose another name'}.\n"
                "UNVERIFIED: Dump provenance, SQL safety, logical consistency, privileges for future writes, "
                "application downtime, and cutover safety.\n"
                "NO SQL EXECUTED. No database changes made. This is a legacy read-only preflight; guarded staging-only import is a separate mode. Production restore remains unsupported.")


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


def check_mysql_restore(config_path: str, alias: str, source: str,
                        login_path: str | None, staging_database: str | None = None) -> MySQLRestoreReport:
    """Inspect MySQL recovery prerequisites without executing a restore."""

    if not login_path or not _IDENTIFIER.fullmatch(login_path):
        raise ValueError("MySQL restore preflight requires --login-path NAME")

    config, _ = _load_config(config_path)
    url = urlsplit(config.resolve(alias))

    if url.scheme != "mysql+pymysql":
        raise ValueError("MySQL restore preflight requires a MySQL database alias")

    target = unquote(url.path.lstrip("/"))

    if not target or not _IDENTIFIER.fullmatch(target) or not url.username or not url.hostname:
        raise ValueError("MySQL restore preflight requires a simple database name, username, and host")

    stage = staging_database or f"{target}_aurora_stage"

    if not _IDENTIFIER.fullmatch(stage) or len(stage) > 64 or stage == target:
        raise ValueError("Invalid staging database name; use a distinct identifier of at most 64 characters")

    backup, size = _backup_file(source)
    connection = [f"--login-path={login_path}", "--protocol=TCP", f"--host={url.hostname}",
                  f"--port={url.port or 3306}", f"--user={unquote(url.username)}"]
    probe = _run(["mysql", *connection, "--batch", "--skip-column-names", target,
                  "--execute=SELECT DATABASE(), CURRENT_USER()"], label="restore connection check")
    fields = probe.stdout.strip().split("\t")

    if len(fields) != 2 or fields[0] != target or fields[1].split("@")[0] != unquote(url.username):
        raise ValueError("MySQL restore connection does not match configured database and account")

    metadata = _run(["mysql", *connection, "--batch", "--skip-column-names", target,
                     "--execute=SELECT COUNT(*) FROM information_schema.TABLES "
                     "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"],
                    label="restore metadata inspection")

    try:
        count = int(metadata.stdout.strip())
    except ValueError:
        raise ValueError("MySQL restore target metadata could not be verified") from None
    # Name is validated before interpolation; no untrusted SQL fragments.
    staging = _run(["mysql", *connection, "--batch", "--skip-column-names", target,
                    "--execute=SELECT COUNT(*) FROM information_schema.SCHEMATA "
                    f"WHERE SCHEMA_NAME = '{stage}'"], label="staging collision inspection")

    if staging.stdout.strip() not in ("0", "1"):
        raise ValueError("MySQL restore staging availability could not be verified")

    return MySQLRestoreReport(alias, target, stage, backup, size, count, staging.stdout.strip() == "0")
