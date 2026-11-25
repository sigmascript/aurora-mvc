"""Guarded ordinary-index successors for verified five-column server histories.

No legacy journal fallback. PostgreSQL executes transactionally; once MySQL DDL
starts, a failure is uncertain and requires physical/history reconciliation.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re
import sqlite3
from urllib.parse import urlsplit

from aurora.core.project_policy import project_is_production

from .migration_coordination import _PG_LOCK_KEY, migration_lock
from .schema import validate_init_models
from .migration_history import history_table, read_history
from .initialization import _write_snapshot
from .snapshots import schema_snapshot
from .schema_inspection import inspect_table
from .schema_inspection import verify_server_declared_schema


def _quote(name, dialect):
    marker = "`" if dialect == "mysql" else '"'
    return marker + name.replace(marker, marker * 2) + marker


def _index_catalog(connection, table, name, *, drop_safety=False):
    """Return a verified ordinary B-tree index's ordered columns, or None.

    Complex, constraint-backed, expression, partial, included, descending,
    non-B-tree or FK-supporting indexes are deliberately not eligible to drop.
    """
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name, "
            "NON_UNIQUE AS non_unique, SEQ_IN_INDEX AS position, "
            "INDEX_TYPE AS method, SUB_PART AS prefix_length, "
            "COLLATION AS direction FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table AND INDEX_NAME=:name "
            "ORDER BY SEQ_IN_INDEX",
            {"table": table, "name": name},
        )
        if not rows:
            return None
        if name == "PRIMARY" or any(
            row["non_unique"] != 1
            or row["method"].upper() != "BTREE"
            or row["prefix_length"] is not None
            or row["direction"] != "A"
            or row["position"] != position
            or row["column_name"] is None
            for position, row in enumerate(rows, 1)
        ):
            raise ValueError("Selected index is not an eligible ordinary B-tree index")
        columns = tuple(row["column_name"] for row in rows)
        # InnoDB may require a secondary index to support a foreign key.
        # Reject any leading FK column instead of relying on a DDL failure.
        fks = connection.fetch_all(
            "SELECT COLUMN_NAME AS column_name FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            {"table": table},
        )
        if drop_safety and any(row["column_name"] == columns[0] for row in fks):
            raise ValueError("Index may support a foreign-key constraint; removal refused")
        return columns

    rows = connection.fetch_all(
        "SELECT i.indisunique AS is_unique, i.indisprimary AS is_primary, "
        "i.indisvalid AS is_valid, i.indisready AS is_ready, "
        "i.indpred IS NOT NULL AS is_partial, i.indexprs IS NOT NULL AS is_expression, "
        "i.indnkeyatts AS key_count, i.indnatts AS total_count, "
        "am.amname AS method, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c "
        "WHERE c.conindid=i.indexrelid) AS constraint_backed "
        "FROM pg_catalog.pg_index i "
        "JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid "
        "JOIN pg_catalog.pg_am am ON am.oid=ci.relam "
        "WHERE i.indrelid=to_regclass(:table) AND ci.relname=:name",
        {"table": table, "name": name},
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("Index catalog identity is ambiguous")
    info = rows[0]
    if (
        info["is_unique"]
        or info["is_primary"]
        or info["is_partial"]
        or info["is_expression"]
        or info["constraint_backed"]
        or info["is_valid"] is not True
        or info["is_ready"] is not True
        or info["method"] != "btree"
        or info["key_count"] != info["total_count"]
    ):
        raise ValueError("Selected index is not an eligible ordinary B-tree index")
    keys = connection.fetch_all(
        "SELECT a.attname AS column_name, "
        "pg_catalog.pg_get_indexdef(i.indexrelid, n.ordinal, TRUE) AS definition "
        "FROM pg_catalog.pg_index i "
        "JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid "
        "CROSS JOIN LATERAL generate_series(1, i.indnkeyatts) AS n(ordinal) "
        "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid "
        "AND a.attnum=i.indkey[n.ordinal-1] "
        "WHERE i.indrelid=to_regclass(:table) AND ci.relname=:name "
        "ORDER BY n.ordinal",
        {"table": table, "name": name},
    )
    if len(keys) != info["key_count"] or any(
        key["column_name"] is None
        or key["definition"] not in (key["column_name"], _quote(key["column_name"], "postgresql"))
        for key in keys
    ):
        raise ValueError("Complex index definition requires separate review")
    columns = tuple(key["column_name"] for key in keys)
    fks = connection.fetch_all(
        "SELECT a.attname AS column_name FROM pg_catalog.pg_constraint c "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid "
        "AND a.attnum=c.conkey[1] "
        "WHERE c.conrelid=to_regclass(:table) AND c.contype='f'",
        {"table": table},
    )
    if drop_safety and any(row["column_name"] == columns[0] for row in fks):
        raise ValueError("Index may support a foreign-key constraint; removal refused")
    return columns


def _review(connection, models, alias, identity, operation, root):
    from .schema_inspection import inspect_table

    if operation["kind"] not in ("create_index", "drop_index"):
        raise ValueError("Only ordinary index operations are supported")
    if operation["kind"] == "create_index" and operation["unique"]:
        raise ValueError("UNIQUE index changes require separate constraint review")
    ordered = validate_init_models(models, dialect=connection.dialect, allow_extended=True)
    history, snapshot = read_history(connection, root, alias)
    declared = schema_snapshot(*ordered, dialect=connection.dialect, allow_v2=True)["tables"]
    if declared != snapshot["models"]:
        raise ValueError("Models differ from the last verified server snapshot")
    table = operation["table"]
    if table not in {model.__table_name__ for model in ordered}:
        raise ValueError("Index target is not a registered model table")
    observed = inspect_table(connection, table)
    if not observed.exists or not observed.uniqueness_verified:
        raise ValueError("Selected table cannot be independently verified")
    name = operation["name"]
    present = _index_catalog(connection, table, name, drop_safety=operation["kind"] == "drop_index")
    if operation["kind"] == "create_index":
        if present is not None or name in observed.indexes:
            raise ValueError("An index already uses this name")
        if connection.dialect == "postgresql":
            # PostgreSQL indexes share a namespace with tables and sequences.
            occupied = connection.fetch_first(
                "SELECT 1 AS occupied FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=:name",
                {"name": name},
            )
            if occupied is not None:
                raise ValueError("A server schema object already uses the index name")
        requested = tuple(operation["columns"])
        available = {col.name for col in observed.columns}
        if any(column not in available for column in requested):
            raise ValueError("Index references a column that does not exist")
        sql = (
            "CREATE INDEX "
            + _quote(name, connection.dialect)
            + " ON "
            + _quote(table, connection.dialect)
            + " ("
            + ", ".join(_quote(col, connection.dialect) for col in requested)
            + ")"
        )
    else:
        if present is None:
            raise ValueError("The named ordinary index does not exist on the selected table")
        requested = present
        if connection.dialect == "mysql":
            sql = "ALTER TABLE " + _quote(table, "mysql") + " DROP INDEX " + _quote(name, "mysql")
        else:
            sql = "DROP INDEX " + _quote(name, "postgresql")
    statement = {
        "kind": operation["kind"],
        "table": table,
        "name": name,
        "columns": requested,
        "statement": sql,
    }
    payload = json.dumps(
        {
            "alias": alias,
            "identity": identity,
            "dialect": connection.dialect,
            "history_length": len(history),
            "operation": statement,
            "predecessor": snapshot["physical_schema"],
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return statement, sha256(payload.encode("utf-8")).hexdigest(), len(history)


def _apply(
    connection, models, root, alias, identity, parsed, operation, expected, title="Untitled"
):
    from .connections import _mysql_database_identity, _postgres_database_identity

    database_identity = (
        _mysql_database_identity if connection.dialect == "mysql" else _postgres_database_identity
    )

    def locked():
        if database_identity(connection, parsed) != identity:
            raise RuntimeError("Server database identity changed after review")
        fresh, digest, count = _review(connection, models, alias, identity, operation, root)
        if not hmac.compare_digest(digest, expected):
            raise RuntimeError("Server index proposal changed after review")
        try:
            connection.execute_write(fresh["statement"])
            after = _index_catalog(connection, operation["table"], operation["name"])
            if (operation["kind"] == "create_index" and after != fresh["columns"]) or (
                operation["kind"] == "drop_index" and after is not None
            ):
                raise RuntimeError("Server index postcondition differs from approved review")
            # The history reader verifies the full preceding physical schema.
            # The next snapshot and history row must then form one successor.
            path = _write_snapshot(connection, root, alias, models, title)
            row = {
                "id": count + 1,
                "title": title,
                "snapshot": path.name,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "warnings": None,
            }
            connection.execute_write(
                "INSERT INTO " + history_table(connection) + " (id,title,snapshot,date,warnings) "
                "VALUES (:id,:title,:snapshot,:date,:warnings)",
                row,
            )
            recorded, _ = read_history(connection, root, alias)
            if len(recorded) != row["id"] or any(
                recorded[-1][key] != value for key, value in row.items()
            ):
                raise RuntimeError("Server index history completion could not be verified")
        except Exception as exc:
            if connection.dialect == "mysql":
                raise RuntimeError(
                    "MySQL index migration outcome uncertain after DDL began; "
                    "inspect physical schema, snapshots and history before retry"
                ) from exc
            raise
        return path

    if connection.dialect == "postgresql":
        with connection.transaction():
            acquired = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not acquired or acquired["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            return locked()
    with migration_lock(connection):
        return locked()


def run_server_index_operation(args, document, config, root, *, simple_confirm, interactive):
    """Review verified history; require confirmation before one guarded DDL."""
    from .cli_check import _select_models
    from .connections import _mysql_database_identity, _postgres_database_identity

    operation = document["operation"]
    if args.apply or args.approve or args.backup:
        raise PermissionError(
            "Server indexes use one ordinary confirmation; advanced flags are unsupported"
        )
    from .migration_review import migration_title

    title = migration_title(getattr(args, "title", None) or "Untitled", "Untitled")
    if not args.dry_run and project_is_production(root):
        raise PermissionError("Server index execution is disabled in production")
    _, models = _select_models(args, root)
    parsed = urlsplit(config.resolve(args.database))
    with config.connect(args.database) as connection:
        if connection.dialect not in ("mysql", "postgresql"):
            raise ValueError("Server index migration requires MySQL or PostgreSQL")
        identity = (
            _mysql_database_identity(connection, parsed)
            if connection.dialect == "mysql"
            else _postgres_database_identity(connection, parsed)
        )
        review, digest, _ = _review(connection, models, args.database, identity, operation, root)
        if args.dry_run:
            if args.format == "json":
                print(
                    json.dumps(
                        {
                            "database_alias": args.database,
                            "dialect": connection.dialect,
                            "operation": operation,
                            "status": "READY_FOR_REVIEW",
                            "review_fingerprint": digest,
                            "read_only": True,
                            "execution_allowed": False,
                        },
                        sort_keys=True,
                        indent=2,
                    )
                )
            else:
                print(f"Database: {args.database} ({connection.dialect}; read-only review)")
                print(f"Operation: {operation['kind']} on {operation['table']}")
                print("Status: READY_FOR_REVIEW; no changes made")
            return 2 if args.check else 0
        if not (simple_confirm or interactive):
            raise PermissionError("Index migration requires explicit confirmation")
        print(f"AuroraSQL migration — {args.database} ({connection.dialect})")
        print(f"  {operation['kind']}: {operation['table']}")
        print("  Reviewed SHA-256: " + digest)
        if interactive and input("Apply these changes? [y/N]: ").strip().lower() != "y":
            print("Migration cancelled; no database changes made.")
            return 0
        result = _apply(
            connection, models, root, args.database, identity, parsed, operation, digest, title
        )
    print(f"Applied {connection.dialect} {operation['kind']} on {args.database}.")
    print("Schema snapshot: " + str(result))
    return 0


# Named UNIQUE constraints have stricter identity and dependency contracts than ordinary indexes.


def _constraint_name(table, column):
    name = f"aurora_uq_{table}_{column}"
    if len(name.encode("utf-8")) > 63:
        raise ValueError("UNIQUE identifier exceeds the supported portable length")
    return name


def _constraint_columns(connection, table, name):
    """Read exact named single-column server UNIQUE identity; refuse ambiguity."""
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT tc.CONSTRAINT_TYPE AS kind, k.COLUMN_NAME AS col, "
            "k.ORDINAL_POSITION AS position FROM information_schema.TABLE_CONSTRAINTS tc "
            "JOIN information_schema.KEY_COLUMN_USAGE k ON "
            "k.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND k.TABLE_NAME=tc.TABLE_NAME "
            "AND k.CONSTRAINT_NAME=tc.CONSTRAINT_NAME "
            "WHERE tc.TABLE_SCHEMA=DATABASE() AND tc.TABLE_NAME=:table "
            "AND tc.CONSTRAINT_NAME=:name ORDER BY k.ORDINAL_POSITION",
            {"table": table, "name": name},
        )
        if not rows:
            return None
        if any(
            row["kind"] != "UNIQUE" or row["position"] != i or not row["col"]
            for i, row in enumerate(rows, 1)
        ):
            raise ValueError("Named constraint has an unexpected definition")
        indexes = connection.fetch_all(
            "SELECT COLUMN_NAME AS col, NON_UNIQUE AS non_unique, "
            "INDEX_TYPE AS method, SUB_PART AS prefix_length, COLLATION AS direction "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=:table AND INDEX_NAME=:name ORDER BY SEQ_IN_INDEX",
            {"table": table, "name": name},
        )
        if len(indexes) != len(rows) or any(
            idx["col"] != row["col"]
            or idx["non_unique"] != 0
            or idx["method"].upper() != "BTREE"
            or idx["prefix_length"] is not None
            or idx["direction"] != "A"
            for idx, row in zip(indexes, rows)
        ):
            raise ValueError("UNIQUE index identity is not independently verified")
        return tuple(row["col"] for row in rows)
    rows = connection.fetch_all(
        "SELECT c.contype AS kind, c.convalidated AS valid, c.condeferrable AS deferred, "
        "a.attname AS col, i.indisvalid AS index_valid, am.amname AS method, "
        "i.indpred IS NOT NULL AS partial, i.indexprs IS NOT NULL AS expression, "
        "i.indnkeyatts AS keys, i.indnatts AS total, array_length(c.conkey, 1) AS column_count "
        "FROM pg_catalog.pg_constraint c "
        "JOIN pg_catalog.pg_class t ON t.oid=c.conrelid "
        "JOIN pg_catalog.pg_namespace ns ON ns.oid=t.relnamespace "
        "JOIN pg_catalog.pg_index i ON i.indexrelid=c.conindid "
        "JOIN pg_catalog.pg_class ix ON ix.oid=i.indexrelid "
        "JOIN pg_catalog.pg_am am ON am.oid=ix.relam "
        "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid "
        "AND a.attnum=c.conkey[1] "
        "WHERE ns.nspname=current_schema() AND t.relname=:table AND c.conname=:name",
        {"table": table, "name": name},
    )
    if not rows:
        return None
    if (
        len(rows) != 1
        or rows[0]["kind"] != "u"
        or rows[0]["valid"] is not True
        or rows[0]["deferred"] is not False
        or rows[0]["index_valid"] is not True
        or rows[0]["method"] != "btree"
        or rows[0]["partial"] is not False
        or rows[0]["expression"] is not False
        or rows[0]["keys"] != 1
        or rows[0]["total"] != 1
        or rows[0]["column_count"] != 1
        or not rows[0]["col"]
    ):
        raise ValueError("Named UNIQUE constraint has an unsupported definition")
    return (rows[0]["col"],)


def is_single_unique_model_delta(previous, desired):
    """Select the dedicated reviewer only for an isolated UNIQUE flag change."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return False
    old, new = json.loads(json.dumps(previous)), json.loads(json.dumps(desired))
    if len(old) != len(new):
        return False
    toggles = 0
    for table_before, table_after in zip(old, new):
        table_before.pop("create_sql_preview", None)
        table_after.pop("create_sql_preview", None)
        if table_before.get("name") != table_after.get("name"):
            return False
        if len(table_before.get("columns", [])) != len(table_after.get("columns", [])):
            return False
        for old_col, new_col in zip(
            table_before.get("columns", []), table_after.get("columns", [])
        ):
            if old_col.get("unique") != new_col.get("unique"):
                toggles += 1
                old_col["unique"] = new_col.get("unique")
    return toggles == 1 and old == new


def _verify_simple_constraints(connection, table, *, existing_unique):
    """Reject hidden constraints/triggers before any MySQL nontransactional DDL."""
    if connection.dialect == "mysql":
        kinds = connection.fetch_all(
            "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": table},
        )
        expected = ["PRIMARY KEY"] + (["UNIQUE"] if existing_unique else [])
        if sorted(row["kind"] for row in kinds) != sorted(expected):
            raise ValueError("Target table contains unsupported constraints")
        trigger = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
            {"table": table},
        )
        if trigger is not None:
            raise ValueError("Target table triggers require separate review")
    else:
        kinds = connection.fetch_all(
            "SELECT contype AS kind FROM pg_catalog.pg_constraint "
            "WHERE conrelid=to_regclass(:table)",
            {"table": table},
        )
        expected = ["p"] + (["u"] if existing_unique else [])
        if sorted(row["kind"] for row in kinds) != sorted(expected):
            raise ValueError("Target table contains unsupported constraints")
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal) AS triggers, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {"table": table},
        )
        if (
            metadata is None
            or metadata["persistence"] != "p"
            or metadata["partitioned"]
            or metadata["inherited"]
            or metadata["rls"]
            or metadata["triggers"] != 0
            or metadata["parents"] != 0
        ):
            raise ValueError("Target table has unsupported PostgreSQL features")


def review_unique_change(connection, models, root, alias, identity):
    """Require exactly one model UNIQUE flag change and an exact old schema."""
    dialect = connection.dialect
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, predecessor = read_history(connection, root, alias)
    declared = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)["tables"]
    prior = predecessor["models"]
    if len(declared) != len(prior):
        raise ValueError("UNIQUE transition cannot add or remove model tables")
    changes = []
    # Snapshot table and field layouts remain authoritative, including order.
    for old_table, new_table in zip(prior, declared):
        if set(old_table) != set(new_table) or old_table.get("name") != new_table.get("name"):
            raise ValueError("UNIQUE transition contains unrelated model changes")
        for key in old_table:
            if key in ("columns", "create_sql_preview"):
                continue
            if old_table[key] != new_table[key]:
                raise ValueError("UNIQUE transition contains unrelated model changes")
        old_cols, new_cols = old_table.get("columns"), new_table.get("columns")
        if (
            not isinstance(old_cols, list)
            or not isinstance(new_cols, list)
            or len(old_cols) != len(new_cols)
        ):
            raise ValueError("UNIQUE transition changes model columns")
        for old, new in zip(old_cols, new_cols):
            if set(old) != set(new) or any(old[k] != new[k] for k in old if k != "unique"):
                raise ValueError("UNIQUE transition contains unrelated column changes")
            if old["unique"] != new["unique"]:
                if old["primary_key"] or new["primary_key"]:
                    raise ValueError("Primary-key uniqueness cannot be changed here")
                changes.append((new_table["name"], new["name"], new["unique"]))
    if len(changes) != 1:
        raise ValueError("Exactly one existing single-column UNIQUE change is required")
    table, column, enable = changes[0]
    observed = inspect_table(connection, table)
    if not observed.exists or not observed.uniqueness_verified or not observed.constraints_verified:
        raise ValueError("Target table metadata cannot be verified")
    name = _constraint_name(table, column)
    expected_indexes = {"PRIMARY" if dialect == "mysql" else f"{table}_pkey"}
    if not enable:
        expected_indexes.add(name)
    if set(observed.indexes) != expected_indexes:
        raise ValueError("Other indexes or constraints require separate UNIQUE review")
    from .foreign_keys import _foreign_key_dependency

    if _foreign_key_dependency(connection, table):
        raise ValueError("Foreign-key dependencies require separate UNIQUE review")
    _verify_simple_constraints(connection, table, existing_unique=not enable)
    existing = _constraint_columns(connection, table, name)
    if enable:
        if existing is not None or column in observed.unique_columns:
            raise ValueError("Column is already UNIQUE")
        # Use the server's own collation/equality semantics, not Python string equality.
        duplicate = connection.fetch_first(
            f"SELECT 1 AS present FROM {_quote(table, dialect)} "
            f"WHERE {_quote(column, dialect)} IS NOT NULL "
            f"GROUP BY {_quote(column, dialect)} HAVING COUNT(*) > 1 LIMIT 1"
        )
        if duplicate is not None:
            raise ValueError(f"Existing duplicate values prevent UNIQUE on {table}.{column}")
        sql = (
            f"ALTER TABLE {_quote(table, dialect)} ADD CONSTRAINT "
            f"{_quote(name, dialect)} UNIQUE ({_quote(column, dialect)})"
        )
        kind = "add_unique"
    else:
        if existing != (column,) or column not in observed.unique_columns:
            raise ValueError("Only an exact Aurora-owned single-column UNIQUE can be removed")
        sql = (
            f"ALTER TABLE {_quote(table, dialect)} DROP INDEX {_quote(name, dialect)}"
            if dialect == "mysql"
            else f"ALTER TABLE {_quote(table, dialect)} DROP CONSTRAINT {_quote(name, dialect)}"
        )
        kind = "drop_unique"
    operation = {"kind": kind, "table": table, "column": column, "name": name, "sql": sql}
    fingerprint = sha256(
        json.dumps(
            {
                "alias": alias,
                "dialect": dialect,
                "identity": identity,
                "history": len(history),
                "predecessor": predecessor["physical_schema"],
                "operation": operation,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return operation, fingerprint, len(history)


def execute_unique_change(
    connection,
    models,
    root,
    alias,
    identity,
    parsed,
    operation,
    expected_digest,
    title,
    configured_url,
):
    """Recheck under lock; execute one DDL, snapshot and append five-column history."""
    from .connections import _mysql_database_identity, _postgres_database_identity

    identify = (
        _mysql_database_identity if connection.dialect == "mysql" else _postgres_database_identity
    )
    dialect = connection.dialect

    def locked():
        if identify(connection, parsed) != identity:
            raise RuntimeError("UNIQUE database identity changed after review")
        fresh, digest, count = review_unique_change(connection, models, root, alias, identity)
        if fresh != operation or not hmac.compare_digest(digest, expected_digest):
            raise RuntimeError("UNIQUE proposal changed after review")
        try:
            connection.execute_write(fresh["sql"])
            actual = _constraint_columns(connection, fresh["table"], fresh["name"])
            present = actual == (fresh["column"],)
            if present != (fresh["kind"] == "add_unique"):
                raise RuntimeError("UNIQUE postcondition was not verified")
            verify_server_declared_schema(
                connection, models, history_tables={history_table(connection)}
            )
            path = _write_snapshot(connection, root, alias, models, title)
            record = {
                "id": count + 1,
                "title": title,
                "snapshot": path.name,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "warnings": None,
            }
            connection.execute_write(
                "INSERT INTO " + history_table(connection) + " (id,title,snapshot,date,warnings) "
                "VALUES (:id,:title,:snapshot,:date,:warnings)",
                record,
            )
            rows, _ = read_history(connection, root, alias)
            if len(rows) != count + 1 or any(
                rows[-1][key] != value for key, value in record.items()
            ):
                raise RuntimeError("UNIQUE forward history completion could not be verified")
        except Exception as exc:
            if dialect == "mysql":
                raise RuntimeError(
                    "MySQL UNIQUE outcome uncertain after DDL began; "
                    "inspect physical schema and history before retry"
                ) from exc
            raise
        return path

    if dialect == "postgresql":
        with connection.transaction():
            lock = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not lock or lock["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            return locked()
    with migration_lock(connection):
        return locked()


def run_unique_change(args, connection, models, root, alias, identity, parsed, configured_url):
    """Single interactive reviewed model transition; no automatic general drift adoption."""
    from .migration_review import migration_title

    if project_is_production(root):
        raise PermissionError("Server UNIQUE migration execution disabled in production")
    if any(getattr(args, option, None) for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("UNIQUE changes require interactive ordinary approval")
    operation, digest, count = review_unique_change(connection, models, root, alias, identity)
    if operation["kind"] == "drop_unique":
        if getattr(args, "confirm", None) in ("yes", "y"):
            raise PermissionError(
                "UNIQUE removal requires interactive target-specific acknowledgment"
            )
    print(f"AuroraSQL migration — {alias} ({connection.dialect})")
    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")
    print("  Reviewed SHA-256: " + digest)
    if operation["kind"] == "drop_unique":
        print(
            "  RECOMMENDATION: Create a backup separately before removing UNIQUE; backup is optional."
        )
        print("  Aurora will not automatically create or restore backups.")
    if getattr(args, "confirm", None) not in ("yes", "y"):
        import sys

        if (
            not sys.stdin.isatty()
            or input("Apply this UNIQUE change? [y/N]: ").strip().lower() != "y"
        ):
            print("Migration cancelled; no database changes made.")
            return 0
    if operation["kind"] == "drop_unique":
        phrase = f"DROP UNIQUE {alias}.{operation['table']}.{operation['column']}"
        print("  Removing UNIQUE permits future duplicate values.")
        if input("Type " + phrase + " to acknowledge: ").strip() != phrase:
            print("UNIQUE removal cancelled; no database changes made.")
            return 0
    path = execute_unique_change(
        connection,
        models,
        root,
        alias,
        identity,
        parsed,
        operation,
        digest,
        migration_title(getattr(args, "title", None), "Untitled"),
        configured_url,
    )
    print(f"Applied Migration{count + 1} to {alias}; snapshot: {path.name}")
    return 0


# SQLite ordinary-index inspection belongs to the constraints owner.


def _ordinary_index_sql(raw):
    """Only nonunique, column-based, application indexes are model-neutral."""
    from .schema import _quote

    indexes = raw.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master "
        "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    names = {
        m[1]
        for m in raw.execute(
            "SELECT type,name,tbl_name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for name, table, sql in indexes:
        if table not in names or not isinstance(sql, str):
            raise ValueError("Index catalog is not independently verifiable")
        info = raw.execute(f"PRAGMA index_list({_quote(table, 'sqlite')})").fetchall()
        entry = next((row for row in info if row[1] == name), None)
        if (
            entry is None
            or entry[2]
            or entry[3] != "c"
            or entry[4]
            or not re.fullmatch(
                r'CREATE\s+INDEX\s+"?[A-Za-z_][A-Za-z_0-9]*"?\s+ON\s+"?[A-Za-z_][A-Za-z_0-9]*"?\s*\(\s*"?[A-Za-z_][A-Za-z_0-9]*"?(?:\s*,\s*"?[A-Za-z_][A-Za-z_0-9]*"?)*\s*\)',
                sql,
                re.I,
            )
        ):
            raise ValueError("Advanced or unique indexes require dedicated schema review")
        columns = raw.execute(f"PRAGMA index_xinfo({_quote(name, 'sqlite')})").fetchall()
        if any(row[1] < 0 or row[5] != 1 or row[3] for row in columns if row[5]):
            raise ValueError("Expression or collated indexes require dedicated review")
    return indexes


def matches_models_with_indexes(raw, models):
    """Check model columns/constraints while excluding independently validated ordinary indexes."""
    from .migration_history import SQLITE_BACKUP_TABLE as BACKUP_TABLE
    from .migration_history import SQLITE_LEGACY_TABLE as LEGACY_TABLE
    from .migration_history import SQLITE_TABLE as TABLE
    from .migration_review import sqlite_matches_models as matches_models

    _ordinary_index_sql(raw)
    with sqlite3.connect(":memory:") as clone:
        clone.execute("PRAGMA foreign_keys=ON")
        for kind, name, _, sql in raw.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ):
            if kind == "table" and sql and name not in (TABLE, LEGACY_TABLE, BACKUP_TABLE):
                clone.execute(sql)
        return matches_models(clone, models)


def validate_index_operation(raw, models, operation):
    """Validate an index-only change against exact model and history evidence."""
    from .migration_history import SQLITE_BACKUP_TABLE as BACKUP_TABLE
    from .migration_history import SQLITE_LEGACY_TABLE as LEGACY_TABLE
    from .migration_history import SQLITE_TABLE as TABLE

    if operation["kind"] not in ("create_index", "drop_index"):
        raise ValueError("Expected an index operation")
    table, name = operation["table"], operation["name"]
    if table not in {m.__table_name__ for m in models} or table in (
        TABLE,
        LEGACY_TABLE,
        BACKUP_TABLE,
    ):
        raise ValueError("Index target is not a registered application table")
    indexes = _ordinary_index_sql(raw)
    existing = {index_name.casefold(): (index_name, target) for index_name, target, _ in indexes}
    if operation["kind"] == "create_index":
        if operation["unique"]:
            raise ValueError("Unique indexes require the dedicated UNIQUE migration path")
        if any(
            row[1].casefold() == name.casefold()
            for row in raw.execute("SELECT type,name FROM sqlite_master")
        ):
            raise ValueError("Index name is already in use")
        fields = {
            field.casefold(): field
            for field in next(m for m in models if m.__table_name__ == table).__columns__
        }
        if any(column.casefold() not in fields for column in operation["columns"]):
            raise ValueError("Index column is not declared by the selected model")
    elif name.casefold() not in existing or existing[name.casefold()][1] != table:
        raise ValueError("Only a recorded ordinary index on the selected table may be dropped")
    matches_models_with_indexes(raw, models)
