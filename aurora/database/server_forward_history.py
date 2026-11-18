"""Read-only five-column server history verification.

Legacy server journals are never silently converted; unverified physical state,
missing files, and interrupted operations block further DDL.
"""

import json
import re
from pathlib import Path

from .inspection import inspect_table
from .snapshot_paths import snapshot_directory

_FILE = re.compile(r"\d{8}-\d{6}(?:-\d{1,3})?\.json\Z")
_COLUMNS = ("id", "title", "snapshot", "date", "warnings")
TABLE = "__migrations__"
LEGACY_TABLE = "migrations"


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
        if name in (TABLE, LEGACY_TABLE, "__backups__"):
            if name == "__backups__":
                from .server_backup_catalog import _catalog_exists
                if not _catalog_exists(connection):
                    raise RuntimeError("Unverified internal backup catalog")
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
    return snapshot_directory(root, alias)


def history_table(connection):
    present = set(_objects(connection)) & {TABLE, LEGACY_TABLE}
    if len(present) > 1:
        raise RuntimeError("Both migrations and __migrations__ exist; manual history reconciliation required")
    return next(iter(present), None)


def has_history(connection):
    return history_table(connection) is not None


def _verify_table(connection, name):
    # Compare the exact public field order, not merely the presence of the names.
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table ORDER BY ORDINAL_POSITION",
            {"table": name})
    else:
        rows = connection.fetch_all(
            "SELECT a.attname AS name FROM pg_catalog.pg_attribute a "
            "WHERE a.attrelid=to_regclass(:table) AND a.attnum>0 "
            "AND NOT a.attisdropped ORDER BY a.attnum", {"table": name})
    if tuple(row["name"] for row in rows) != _COLUMNS:
        raise RuntimeError("Server migrations history has an unexpected table definition")
    table = inspect_table(connection, name)
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
    selected = history_table(connection)
    if selected is None or any(name.startswith("aurorasql_") for name in objects):
        raise RuntimeError("Unrecognized or mixed server migration histories")
    _verify_table(connection, selected)
    rows = connection.fetch_all(
        f"SELECT id,title,snapshot,date,warnings FROM {selected} ORDER BY id")
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


def adopt_history(connection, root, alias, *, identity, parsed):
    """Adopt fully verified legacy server history; never touch model tables or files.

    Caller must separately confirm the operation. Recheck identity and all
    historical witnesses while holding the backend migration lock.
    """
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .coordination import _PG_LOCK_KEY, migration_lock

    if connection.dialect not in ("mysql", "postgresql") or identity is None or parsed is None:
        raise ValueError("Verified server database identity is required")
    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Cannot adopt history inside another migration")
    if history_table(connection) != LEGACY_TABLE:
        raise RuntimeError("There is no single legacy server history table to adopt")

    before = read_history(connection, root, alias)
    identity_fn = (_mysql_database_identity if connection.dialect == "mysql"
                   else _postgres_database_identity)
    if identity_fn(connection, parsed) != identity:
        raise RuntimeError("Server database identity changed before history adoption")

    def rename_locked():
        if identity_fn(connection, parsed) != identity or history_table(connection) != LEGACY_TABLE:
            raise RuntimeError("Server history identity changed during adoption")
        if read_history(connection, root, alias) != before:
            raise RuntimeError("Server history changed during adoption")
        try:
            connection.execute_write("ALTER TABLE migrations RENAME TO __migrations__")
            if history_table(connection) != TABLE or read_history(connection, root, alias) != before:
                raise RuntimeError("Server history adoption verification failed")
        except Exception as exc:
            if connection.dialect == "mysql":
                raise RuntimeError(
                    "MySQL history adoption outcome uncertain after DDL began; "
                    "inspect physical history tables, records and snapshots before retry"
                ) from exc
            raise

    if connection.dialect == "postgresql":
        with connection.transaction():
            row = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY})
            if not row or row["acquired"] is not True:
                raise RuntimeError("PostgreSQL history adoption lock unavailable")
            rename_locked()
    else:
        with migration_lock(connection):
            rename_locked()
    return len(before[0])
