"""Guarded mysqldump backup using an existing MySQL login-path profile."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from secrets import token_hex
import subprocess
import tempfile

from aurora.database.cli_inspection import load_cli_database_config


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _run(command: list[str], *, label: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"MySQL {label} failed; check installed client tools and connectivity") from None
    if result.returncode:
        # Client diagnostics can include credentials and connection strings.
        raise ValueError(f"MySQL {label} failed (exit status {result.returncode}); verify login-path and privileges")
    return result


def backup_mysql_database(config_path: str, alias: str, destination: str | None,
                          login_path: str | None) -> Path:
    if not login_path or not _IDENTIFIER.fullmatch(login_path):
        raise ValueError("MySQL backup requires --login-path NAME (configured with mysql_config_editor)")
    config, _ = load_cli_database_config(config_path)
    url = config.resolve(alias)
    if url.drivername != "mysql+pymysql":
        raise ValueError("MySQL backup requires a MySQL database alias")
    database = url.database
    if not database or not _IDENTIFIER.fullmatch(database):
        raise ValueError("MySQL backup requires a simple database identifier")
    if not url.username or not url.host:
        raise ValueError("MySQL backup requires configured username and host")
    host = url.host
    port = url.port or 3306
    # Explicit host/user/port ensure a login profile cannot silently redirect
    # a backup to a different account or server. Password remains in the profile.
    connection = [f"--login-path={login_path}", "--protocol=TCP",
                  f"--host={host}", f"--port={port}", f"--user={url.username}"]
    probe = _run(["mysql", *connection, "--batch", "--skip-column-names", database,
                  "--execute=SELECT DATABASE(), CURRENT_USER()"], label="connection check")
    fields = probe.stdout.strip().split("\t")
    if len(fields) != 2 or fields[0] != database or fields[1].split("@")[0] != url.username:
        raise ValueError("MySQL connection does not match configured database and account")
    engines = _run(["mysql", *connection, "--batch", "--skip-column-names", database,
                    "--execute=SELECT DISTINCT ENGINE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"],
                   label="storage engine inspection")
    if any(line.strip().upper() != "INNODB" for line in engines.stdout.splitlines() if line.strip()):
        raise ValueError("Consistent online backup supports InnoDB tables only")
    if destination is None:
        folder = Path.cwd() / "backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = str(folder / f"{alias}-{stamp}-{token_hex(4)}.sql")
    output = Path(destination).expanduser().absolute()
    if not output.parent.is_dir() or output.is_symlink() or output.exists():
        raise FileExistsError("Backup destination must be a new file in an existing directory")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".aurora-mysql-", suffix=".sql",
                                         dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
        _run(["mysqldump", *connection, "--single-transaction", "--quick", "--skip-add-locks", "--routines",
              "--triggers", "--events", "--no-tablespaces", "--set-gtid-purged=OFF",
              "--comments", f"--result-file={temporary}", database], label="backup")
        if temporary.stat().st_size == 0:
            raise ValueError("MySQL backup was empty")
        # The footer is emitted only on normal mysqldump completion.
        with temporary.open("rb") as stream:
            stream.seek(max(0, temporary.stat().st_size - 512))
            if b"-- Dump completed on" not in stream.read():
                raise ValueError("MySQL backup completion marker missing")
        # Hard-link publication refuses an existing destination atomically.
        os.link(temporary, output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
