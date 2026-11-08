"""Guarded mysqldump backup using configured or explicit MySQL credentials."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from secrets import token_hex
import stat
import subprocess
import tempfile

from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _run(command: list[str], *, label: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"MySQL {label} failed; check installed client tools and connectivity") from None

    if result.returncode:
        # Client diagnostics can include credentials and connection strings.
        raise ValueError(f"MySQL {label} failed (exit status {result.returncode}); verify credentials and privileges")

    return result


@contextmanager
def _client_authentication(url, login_path):
    """Provide client options without exposing URL passwords in process arguments.

    An ephemeral plaintext option file is allowed only on POSIX filesystems
    where owner-only permissions can be verified. Windows callers retain the
    supported login-path route until its ACL safety is independently tested.
    """
    if login_path is not None:
        if not _IDENTIFIER.fullmatch(login_path):
            raise ValueError("MySQL --login-path must be a configured profile name")

        yield [f"--login-path={login_path}"]
        return

    if os.name != "posix":
        raise ValueError("Automatic MySQL backup credentials require verified file permissions; "
                         "use --login-path on Windows")

    if url.password is None:
        raise ValueError("Configured MySQL backup requires a database URL password "
                         "or an explicit --login-path")

    password = unquote(url.password)

    if any(char in password for char in "\r\n\x00"):
        raise ValueError("Configured MySQL password cannot be encoded in a safe option file")

    # MySQL option-file parsing treats backslashes and quotes specially.
    escaped = password.replace("\\", "\\\\").replace('"', '\\"')
    descriptor, filename = tempfile.mkstemp(prefix=".aurora-mysql-auth-", suffix=".cnf")
    location = Path(filename)

    try:
        os.fchmod(descriptor, 0o600)

        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write('[client]\npassword="' + escaped + '"\n')

        permissions = location.stat()

        if (permissions.st_uid != os.getuid()
                or stat.S_IMODE(permissions.st_mode) != 0o600):
            raise PermissionError("MySQL credential file owner-only access is unverified")

        yield [f"--defaults-extra-file={location}"]
    finally:
        if descriptor >= 0:
            os.close(descriptor)

        location.unlink(missing_ok=True)


def backup_mysql_database(config_path: str, alias: str, destination: str | None,
                          login_path: str | None) -> Path:

    config, _ = _load_config(config_path)
    url = urlsplit(config.resolve(alias))

    if url.scheme != "mysql+pymysql":
        raise ValueError("MySQL backup requires a MySQL database alias")

    database = unquote(url.path.lstrip("/"))

    if not database or not _IDENTIFIER.fullmatch(database):
        raise ValueError("MySQL backup requires a simple database identifier")

    if not url.username or not url.hostname:
        raise ValueError("MySQL backup requires configured username and host")

    host = url.hostname
    port = url.port or 3306

    with _client_authentication(url, login_path) as authentication:
        return _backup_selected_mysql_database(url, alias, destination, database,
                                               host, port, authentication)


def _backup_selected_mysql_database(url, alias, destination, database, host, port,
                                    authentication):
    """Probe and dump using the same bounded authentication scope."""
    # MySQL option-file selectors must be first. Explicit host/user/port
    # additionally bind the backup client to AuroraSQL's selected URL.
    connection = [*authentication, "--protocol=TCP",
                  f"--host={host}", f"--port={port}", f"--user={unquote(url.username)}"]
    probe = _run(["mysql", *connection, "--batch", "--skip-column-names", database,
                  "--execute=SELECT DATABASE(), CURRENT_USER()"], label="connection check")
    fields = probe.stdout.strip().split("\t")

    if len(fields) != 2 or fields[0] != database or fields[1].split("@")[0] != unquote(url.username):
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
