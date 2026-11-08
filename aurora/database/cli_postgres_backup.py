"""Guarded PostgreSQL custom-format backup, with no restore execution.

Client authentication must already be configured for libpq (for example,
PGPASSFILE). Credentials from a database URL are never placed in arguments,
logs, or a subprocess environment assembled by AuroraSQL.
"""

from pathlib import Path
import os
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config


_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _run_client(arguments, *, label):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True,
                                timeout=180, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(f"PostgreSQL {label} failed; check client tools and connectivity") from None

    if result.returncode:
        # PostgreSQL client diagnostics can include user-controlled URLs.
        raise ValueError(f"PostgreSQL {label} failed; check credentials and permissions")

    return result.stdout


def backup_postgres_database(config_path, alias, destination):
    """Create and validate one new pg_dump archive for an explicit alias.

    Verify the external CLI's database/user identity before launching pg_dump.
    The operation never changes the source database or overwrites an archive.
    """
    if not destination:
        raise ValueError("PostgreSQL backup requires --to NEW_FILE.dump")

    config, _ = _load_config(config_path)
    url = urlsplit(config.resolve(alias))

    if url.scheme != "postgresql+psycopg" or not url.hostname or not url.username:
        raise ValueError("PostgreSQL backup requires a named PostgreSQL connection")

    database = unquote(url.path.lstrip("/"))
    user = unquote(url.username)

    if not _ROLE.fullmatch(database) or not _ROLE.fullmatch(user):
        raise ValueError("PostgreSQL backup requires ordinary database and account names")

    output = Path(destination).expanduser().absolute()

    if (output.suffix != ".dump" or not output.parent.is_dir()
            or output.is_symlink() or output.exists()):
        raise ValueError("PostgreSQL backup requires a new .dump file in an existing directory")

    common = ["--host", url.hostname, "--port", str(url.port or 5432),
              "--username", user, "--dbname", database, "--no-password"]
    identity = _run_client(
        ["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--field-separator=|",
         *common, "--command", "SELECT current_database(), current_user"],
        label="connection identity check",
    ).strip()

    if identity != f"{database}|{user}":
        raise ValueError("PostgreSQL backup client selected an unexpected database or account")

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(prefix=".aurora-postgres-", suffix=".dump",
                                         dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)

        _run_client(["pg_dump", *common, "--format=custom", "--no-owner", "--no-acl",
                     "--file", str(temporary)], label="backup")

        if temporary.stat().st_size < 64:
            raise ValueError("PostgreSQL backup archive is unexpectedly small")

        with temporary.open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise ValueError("PostgreSQL custom archive signature is invalid")

        catalog = _run_client(["pg_restore", "--list", str(temporary)],
                              label="archive inspection")

        if not any(line.lstrip().startswith(";") for line in catalog.splitlines()):
            raise ValueError("PostgreSQL archive catalog cannot be verified")

        # Atomic hard-link publication refuses any destination created meanwhile.
        os.link(temporary, output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
