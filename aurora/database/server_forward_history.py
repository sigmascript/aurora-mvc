"""Read-only five-column server history verification.

Legacy server journals are never silently converted; unverified physical state,
missing files, and interrupted operations block further DDL.
"""

import json
import re
from pathlib import Path

from .inspection import inspect_table

_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
_FILE = re.compile(r"\d{8}-\d{6}(?:-\d{1,3})?\.json\Z")
_COLUMNS = ("id", "title", "snapshot", "date", "warnings")


def _objects(connection):
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE()")
        if any(item["kind"] != "BASE TABLE" for item in rows):
            raise RuntimeError("Unrecognized MySQL schema objects; migration refused")
    elif connection.dialect == "postgresql":
        rows = connection.fetch_all(
            "SELECT c.relname AS name, c.relkind AS kind FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f')")
        if any(item["kind"] != "r" for item in rows):
            raise RuntimeError("Unrecognized PostgreSQL schema objects; migration refused")
    else:
        raise ValueError("Server history requires MySQL or PostgreSQL")
    return sorted(row["name"] for row in rows)


def _physical(connection):
    """Independently observe current tables, columns, keys and indexes."""
    result = []
    for name in _objects(connection):
        if name == "migrations":
            continue
        table = inspect_table(connection, name)
        if not table.exists or not table.uniqueness_verified:
            raise RuntimeError("Cannot verify server table metadata: " + name)
        result.append({
            "table": name,
            "columns": [[col.name, col.sql_type, col.nullable, col.primary_key,
                         col.default_sql] for col in table.columns],
            "indexes": sorted(table.indexes),
            "unique_columns": sorted(table.unique_columns),
            "foreign_keys": [list(fk) for fk in table.foreign_keys],
        })
    return result


def _directory(root, alias):
    if not isinstance(alias, str) or not _SAFE_NAME.fullmatch(alias) or alias in (".", ".."):
        raise ValueError("Database name cannot safely identify a snapshot directory")
    base = Path(root)
    folder = base / "migrations" / alias
    if not base.is_dir():
        raise ValueError("Project directory is missing")
    for path in (folder.parent, folder):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RuntimeError("Migration snapshot directory is unsafe")
    return folder


def has_history(connection):
    return "migrations" in _objects(connection)


def _verify_table(connection):
    # Compare the exact public field order, not merely the presence of the names.
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='migrations' ORDER BY ORDINAL_POSITION")
    else:
        rows = connection.fetch_all(
            "SELECT a.attname AS name FROM pg_catalog.pg_attribute a "
            "WHERE a.attrelid=to_regclass('migrations') AND a.attnum>0 "
            "AND NOT a.attisdropped ORDER BY a.attnum")
    if tuple(row["name"] for row in rows) != _COLUMNS:
        raise RuntimeError("Server migrations history has an unexpected table definition")
    table = inspect_table(connection, "migrations")
    if not table.exists or tuple(column.name for column in table.columns) != _COLUMNS:
        raise RuntimeError("Server migrations history columns are invalid")
    if (not table.columns[0].primary_key
            or any(column.nullable or column.primary_key for column in table.columns[1:4])
            or table.columns[0].nullable
            or not table.columns[4].nullable
            or table.columns[4].primary_key):
        raise RuntimeError("Server migrations history column constraints are invalid")


def read_history(connection, root, alias):
    """Read all snapshots and compare the latest physical witness; never write."""
    objects = _objects(connection)
    if "migrations" not in objects or any(name.startswith("aurorasql_") for name in objects):
        raise RuntimeError("Unrecognized or mixed server migration histories")
    _verify_table(connection)
    rows = connection.fetch_all(
        "SELECT id,title,snapshot,date,warnings FROM migrations ORDER BY id")
    if not rows:
        raise RuntimeError("Server history has no successful initial migration")
    directory = _directory(root, alias)
    latest = None
    seen_snapshots = set()

    for number, row in enumerate(rows, 1):
        filename = row["snapshot"]
        if (row["id"] != number or not row["title"] or not row["date"] or
                not isinstance(filename, str) or not _FILE.fullmatch(filename)):
            raise RuntimeError("Server migration history is incomplete or malformed")
        if filename in seen_snapshots:
            raise RuntimeError("Server migration history reuses a snapshot filename")
        seen_snapshots.add(filename)

        if row["warnings"] is not None:
            try:
                warnings = json.loads(row["warnings"])
                if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
                    raise ValueError("Invalid warnings")
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Server migration warnings are malformed") from exc
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Server migration snapshot missing: {filename}")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Server migration snapshot unreadable: {filename}") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"Server migration snapshot invalid: {filename}")

        models = snapshot.get("models")
        physical = snapshot.get("physical_schema")
        model_names = (
            [table.get("name") for table in models if isinstance(table, dict)]
            if isinstance(models, list) else []
        )
        table_names = (
            [table.get("table") for table in physical if isinstance(table, dict)]
            if isinstance(physical, list) else []
        )

        if (snapshot.get("database") != alias
                or snapshot.get("engine") != connection.dialect
                or snapshot.get("title") != row["title"]
                or not isinstance(models, list)
                or not isinstance(physical, list)
                or any(not isinstance(table, dict) or not isinstance(table.get("name"), str)
                       for table in models)
                or any(not isinstance(table, dict) or not isinstance(table.get("table"), str)
                       for table in physical)
                or len(model_names) != len(set(model_names))
                or len(table_names) != len(set(table_names))
                or sorted(model_names) != sorted(table_names)):
            raise RuntimeError(f"Server migration snapshot invalid: {filename}")
        latest = snapshot
    if latest["physical_schema"] != _physical(connection):
        raise RuntimeError("Server physical schema differs from last migration snapshot; manual reconciliation required")
    return rows, latest
