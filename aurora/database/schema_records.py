"""Complete, data-only SQLite schema records for successful migrations."""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .snapshots import schema_snapshot


_SAFE_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")


def _record_directory(project_root, database_file):
    """Choose a physical-database directory without trusting configured aliases."""
    name = Path(database_file).stem

    if not _SAFE_DATABASE.fullmatch(name) or name in (".", ".."):
        raise ValueError("SQLite database filename cannot safely name a migration directory")

    root = Path(project_root) / "migrations"
    directory = root / name

    for candidate in (root, directory):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise ValueError("Migration snapshot directory must be an ordinary directory")

    return directory


def prepare_schema_record(project_root, database_file, models):
    """Check that a schema record can be written before approving database DDL."""
    directory = _record_directory(project_root, database_file)
    snapshot = schema_snapshot(*models, dialect="sqlite") if models else {"tables": []}

    if not directory.parent.parent.is_dir():
        raise ValueError("Project directory is missing")

    return directory, snapshot


def write_schema_record(project_root, database_file, models, title):
    """Write one complete schema record after committed SQLite DDL.

    A failed record write raises explicitly: the caller must not claim that the
    database operation was rolled back or retry the DDL automatically.
    """
    directory, snapshot = prepare_schema_record(project_root, database_file, models)
    database = Path(database_file)

    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        physical = [
            {"type": kind, "name": name, "table": table, "sql": sql}
            for kind, name, table, sql in rows
            if not name.startswith("aurorasql_")
        ]
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed; schema record not written")

    model_tables = {table["name"] for table in snapshot["tables"]}
    physical_tables = {row["name"] for row in physical if row["type"] == "table"}

    if not model_tables.issubset(physical_tables):
        raise RuntimeError("Database tables differ from model schema; snapshot refused")

    content = {
        "database": database.stem,
        "engine": "sqlite",
        "title": title,
        "models": snapshot["tables"],
        "physical_schema": physical,
    }
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    for sequence in range(1000):
        suffix = "" if sequence == 0 else f"-{sequence}"
        candidate = directory / f"{timestamp}{suffix}.json"

        try:
            with candidate.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
            return candidate
        except FileExistsError:
            continue
        except OSError as error:
            # The exclusive-create path belongs to this attempt; never leave
            # a truncated record that could later be mistaken for evidence.
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

            raise RuntimeError(
                "Database operation may have succeeded, but its schema snapshot "
                "could not be written. Inspect database history before retrying."
            ) from error

    raise RuntimeError("No available timestamped schema snapshot filename")
