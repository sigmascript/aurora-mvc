"""Explicit optional backup execution for SQLite, MySQL and PostgreSQL.

Backups are developer-initiated and never required for migrations.
Restore execution is not provided here.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

from aurora.core.project_policy import project_is_production
from .cli_check import _load_config
from .migration_history import sqlite_has_forward_history as has_forward_history
from .migration_history import sqlite_read_history as read_history

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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

    if project_is_production(Path(args.config).expanduser().resolve().parent):
        raise ValueError("AuroraSQL backup in production requires separate approval")

    from .backup_catalog import (
        _sqlite_catalog_format,
        destination_fingerprint,
        inspect_sqlite_catalog,
        prepare_artifact,
        reconcile_sqlite_catalog,
        record_artifact,
        validate_title,
    )

    validate_title(getattr(args, "title", None))
    config, config_dir = _load_config(args.config)
    url = config.resolve(args.database)
    source = _sqlite_source(url, config_dir)
    # A new backup must not conceal conflicting or damaged older evidence.
    from .backup_catalog import list_artifacts

    identity = destination_fingerprint(url, config_dir)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        if _sqlite_catalog_format(db) == "legacy":
            raise ValueError(
                "Legacy __backups__ catalog requires an explicitly reviewed transition"
            )
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
    record_artifact(
        config_dir,
        args.database,
        "sqlite",
        backup_id,
        destination,
        getattr(args, "title", None),
        identity,
    )
    reconcile_sqlite_catalog(source, config_dir, args.database, identity)
    return destination


def _run(command: list[str], *, label: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(
            f"MySQL {label} failed; check installed client tools and connectivity"
        ) from None

    if result.returncode:
        # Client diagnostics can include credentials and connection strings.
        raise ValueError(
            f"MySQL {label} failed (exit status {result.returncode}); verify credentials and privileges"
        )

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
        raise ValueError(
            "Automatic MySQL backup credentials require verified file permissions; "
            "use --login-path on Windows"
        )

    if url.password is None:
        raise ValueError(
            "Configured MySQL backup requires a database URL password or an explicit --login-path"
        )

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

        if permissions.st_uid != os.getuid() or stat.S_IMODE(permissions.st_mode) != 0o600:
            raise PermissionError("MySQL credential file owner-only access is unverified")

        yield [f"--defaults-extra-file={location}"]
    finally:
        if descriptor >= 0:
            os.close(descriptor)

        location.unlink(missing_ok=True)


def backup_mysql_database(
    config_path: str,
    alias: str,
    destination: str | None,
    login_path: str | None,
    expected_url: str | None = None,
) -> Path:

    config, _ = _load_config(config_path)
    selected = config.resolve(alias)
    if expected_url is not None and selected != expected_url:
        raise ValueError("Configured MySQL backup destination changed during selection")
    url = urlsplit(selected)

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
        return _backup_selected_mysql_database(
            url, alias, destination, database, host, port, authentication
        )


def _backup_selected_mysql_database(url, alias, destination, database, host, port, authentication):
    """Probe and dump using the same bounded authentication scope."""
    # MySQL option-file selectors must be first. Explicit host/user/port
    # additionally bind the backup client to AuroraSQL's selected URL.
    connection = [
        *authentication,
        "--protocol=TCP",
        f"--host={host}",
        f"--port={port}",
        f"--user={unquote(url.username)}",
    ]
    probe = _run(
        [
            "mysql",
            *connection,
            "--batch",
            "--skip-column-names",
            database,
            "--execute=SELECT DATABASE(), CURRENT_USER()",
        ],
        label="connection check",
    )
    fields = probe.stdout.strip().split("\t")

    if (
        len(fields) != 2
        or fields[0] != database
        or fields[1].split("@")[0] != unquote(url.username)
    ):
        raise ValueError("MySQL connection does not match configured database and account")

    engines = _run(
        [
            "mysql",
            *connection,
            "--batch",
            "--skip-column-names",
            database,
            "--execute=SELECT DISTINCT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'",
        ],
        label="storage engine inspection",
    )

    if any(
        line.strip().upper() != "INNODB" for line in engines.stdout.splitlines() if line.strip()
    ):
        raise ValueError("Consistent online backup supports InnoDB tables only")

    if destination is None:
        raise ValueError("Managed MySQL backup destination is required")

    output = Path(destination).expanduser().absolute()

    if not output.parent.is_dir() or output.is_symlink() or output.exists():
        raise FileExistsError("Backup destination must be a new file in an existing directory")

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            prefix=".aurora-mysql-", suffix=".sql", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)

        _run(
            [
                "mysqldump",
                *connection,
                "--single-transaction",
                "--quick",
                "--skip-add-locks",
                "--routines",
                "--triggers",
                "--events",
                "--no-tablespaces",
                "--set-gtid-purged=OFF",
                "--comments",
                f"--result-file={temporary}",
                database,
            ],
            label="backup",
        )

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


def _run_client(arguments, *, label):
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(
            f"PostgreSQL {label} failed; check client tools and connectivity"
        ) from None

    if result.returncode:
        # PostgreSQL client diagnostics can include user-controlled URLs.
        raise ValueError(f"PostgreSQL {label} failed; check credentials and permissions")

    return result.stdout


def backup_postgres_database(config_path, alias, destination, expected_url=None):
    """Create and validate one new pg_dump archive for an explicit alias.

    Verify the external CLI's database/user identity before launching pg_dump.
    The operation never changes the source database or overwrites an archive.
    """
    if not destination:
        raise ValueError("PostgreSQL backup requires --to NEW_FILE.dump")

    config, _ = _load_config(config_path)
    selected = config.resolve(alias)
    if expected_url is not None and selected != expected_url:
        raise ValueError("Configured PostgreSQL backup destination changed during selection")
    url = urlsplit(selected)

    if url.scheme != "postgresql+psycopg" or not url.hostname or not url.username:
        raise ValueError("PostgreSQL backup requires a named PostgreSQL connection")

    database = unquote(url.path.lstrip("/"))
    user = unquote(url.username)

    if not _ROLE.fullmatch(database) or not _ROLE.fullmatch(user):
        raise ValueError("PostgreSQL backup requires ordinary database and account names")

    output = Path(destination).expanduser().absolute()

    if (
        output.suffix != ".dump"
        or not output.parent.is_dir()
        or output.is_symlink()
        or output.exists()
    ):
        raise ValueError("PostgreSQL backup requires a new .dump file in an existing directory")

    common = [
        "--host",
        url.hostname,
        "--port",
        str(url.port or 5432),
        "--username",
        user,
        "--dbname",
        database,
        "--no-password",
    ]
    identity = _run_client(
        [
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--field-separator=|",
            *common,
            "--command",
            "SELECT current_database(), current_user",
        ],
        label="connection identity check",
    ).strip()

    if identity != f"{database}|{user}":
        raise ValueError("PostgreSQL backup client selected an unexpected database or account")

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            prefix=".aurora-postgres-", suffix=".dump", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)

        _run_client(
            [
                "pg_dump",
                *common,
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(temporary),
            ],
            label="backup",
        )

        if temporary.stat().st_size < 64:
            raise ValueError("PostgreSQL backup archive is unexpectedly small")

        with temporary.open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise ValueError("PostgreSQL custom archive signature is invalid")

        catalog = _run_client(["pg_restore", "--list", str(temporary)], label="archive inspection")

        if not any(line.lstrip().startswith(";") for line in catalog.splitlines()):
            raise ValueError("PostgreSQL archive catalog cannot be verified")

        # Atomic hard-link publication refuses any destination created meanwhile.
        os.link(temporary, output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
