"""Guarded PostgreSQL single-column FK successors for verified forward histories.

Narrow first slice: existing columns, same selected database, ordinary primary-key
parent, NO ACTION rules, one change only. MySQL uses an Aurora-owned supporting index and explicitly guarded nontransactional DDL.
"""

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import sys

from aurora.core.project_policy import project_is_production

from .migration_coordination import _PG_LOCK_KEY, migration_lock
from .schema import validate_init_models
from .schema_inspection import inspect_table
from .schema import _resolved_foreign_keys
from .migration_history import history_table, read_history
from .initialization import _write_snapshot
from .schema_inspection import verify_server_declared_schema
from .constraints import _quote
from .snapshots import schema_snapshot


def _foreign_key_dependency(connection, table):
    """Refuse any FK involving the target table, including external dependents."""
    if connection.dialect == "mysql":
        result = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE CONSTRAINT_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL "
            "AND (TABLE_NAME=:table OR REFERENCED_TABLE_NAME=:table) LIMIT 1",
            {"table": table},
        )
    else:
        result = connection.fetch_first(
            "SELECT 1 AS present FROM pg_catalog.pg_constraint c "
            "WHERE c.contype='f' AND (c.conrelid=to_regclass(:table) "
            "OR c.confrelid=to_regclass(:table)) LIMIT 1",
            {"table": table},
        )
    return result is not None


def _normalized_tables(tables):
    normalized = json.loads(json.dumps(tables))
    for table in normalized:
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list):
            continue
        table.pop("create_sql_preview", None)
        for col in table["columns"]:
            if not isinstance(col, dict):
                continue
            col.setdefault("on_update", "NO ACTION" if col.get("references") else None)
            col.setdefault("on_delete", "NO ACTION" if col.get("references") else None)
    return normalized


def single_fk_delta(previous, desired):
    """Detect exactly one FK target addition/removal, with no other model edits."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return False
    old, new = _normalized_tables(previous), _normalized_tables(desired)
    if len(old) != len(new) or any(
        not isinstance(table, dict)
        or not isinstance(table.get("name"), str)
        or not isinstance(table.get("columns"), list)
        for table in old + new
    ):
        return False
    changed = 0
    for before, after in zip(old, new):
        if (
            before["name"] != after["name"]
            or len(before["columns"]) != len(after["columns"])
            or any(not isinstance(col, dict) for col in before["columns"] + after["columns"])
        ):
            return False
        for left, right in zip(before["columns"], after["columns"]):
            if left.get("references") != right.get("references"):
                if bool(left.get("references")) == bool(right.get("references")):
                    return False
                changed += 1
                left["references"] = right["references"]
                left["on_update"] = right["on_update"]
                left["on_delete"] = right["on_delete"]
    return changed == 1 and old == new


def _fk_name(table, column):
    result = f"aurora_fk_{table}_{column}"
    if len(result.encode()) > 63:
        raise ValueError("Foreign-key name exceeds portable PostgreSQL length")
    return result


def _fk_catalog(connection, table, name):
    """Independent catalog proof: exact, validated, nondeferrable single FK."""
    rows = connection.fetch_all(
        "SELECT c.contype AS kind, c.convalidated AS valid, "
        "c.condeferrable AS deferrable, c.confmatchtype AS match_type, "
        "src.attname AS src, target.relname AS target_table, "
        "dest.attname AS target_column, c.confupdtype AS update_action, "
        "c.confdeltype AS delete_action, "
        "array_length(c.conkey,1) AS source_count, "
        "array_length(c.confkey,1) AS target_count, "
        "target.relnamespace=ns.oid AS same_schema "
        "FROM pg_catalog.pg_constraint c "
        "JOIN pg_catalog.pg_class t ON t.oid=c.conrelid "
        "JOIN pg_catalog.pg_namespace ns ON ns.oid=t.relnamespace "
        "JOIN pg_catalog.pg_class target ON target.oid=c.confrelid "
        "LEFT JOIN pg_catalog.pg_attribute src ON src.attrelid=c.conrelid "
        "AND src.attnum=c.conkey[1] "
        "LEFT JOIN pg_catalog.pg_attribute dest ON dest.attrelid=c.confrelid "
        "AND dest.attnum=c.confkey[1] "
        "WHERE ns.nspname=current_schema() AND t.relname=:table AND c.conname=:name",
        {"table": table, "name": name},
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("Foreign-key catalog identity is ambiguous")
    row = rows[0]
    if (
        row["kind"] != "f"
        or row["valid"] is not True
        or row["deferrable"] is not False
        or row["match_type"] != "s"
        or row["update_action"] != "a"
        or row["delete_action"] != "a"
        or row["source_count"] != 1
        or row["target_count"] != 1
        or row["same_schema"] is not True
        or any(not row[field] for field in ("src", "target_table", "target_column"))
    ):
        raise ValueError("Named foreign key does not match the supported contract")
    return (row["src"], row["target_table"], row["target_column"])


def review_fk(connection, models, root, alias, identity):
    if connection.dialect == "mysql":
        return review_mysql_fk(connection, models, root, alias, identity)
    if connection.dialect != "postgresql":
        raise ValueError("Unsupported foreign-key backend")
    ordered = validate_init_models(models, dialect="postgresql", allow_extended=True)
    rows, predecessor = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect="postgresql", allow_v2=True)["tables"]
    if not single_fk_delta(predecessor["models"], desired):
        raise ValueError("Exactly one existing-column foreign-key change is required")
    before, after = _normalized_tables(predecessor["models"]), _normalized_tables(desired)
    changes = [
        (new_table["name"], old_col["name"], new_col["references"])
        for old_table, new_table in zip(before, after)
        for old_col, new_col in zip(old_table["columns"], new_table["columns"])
        if old_col["references"] != new_col["references"]
    ]
    if len(changes) != 1:
        raise ValueError("Foreign-key reviewer must find one changed column")
    table, col, target = changes[0]
    model_by_table = {model.__table_name__: model for model in ordered}
    source = model_by_table[table]
    field = source.__columns__[col]
    if field.primary_key or field.unique:
        raise ValueError("Foreign-key source must be an ordinary existing column")
    observed = inspect_table(connection, table)
    if (
        not observed.exists
        or not observed.constraints_verified
        or observed.foreign_key_actions is None
    ):
        raise ValueError("Source table constraints cannot be independently verified")
    if set(observed.indexes) != {f"{table}_pkey"} or observed.unique_columns:
        raise ValueError("Foreign-key source has unsupported indexes or constraints")
    if len(observed.foreign_keys) > 1:
        raise ValueError("Other foreign-key constraints require separate review")
    name = _fk_name(table, col)
    existing = _fk_catalog(connection, table, name)
    if target:
        # Use resolved model targets, not unchecked user strings or SQL literals.
        resolved = _resolved_foreign_keys(source, ordered)
        if col not in resolved:
            raise ValueError("Foreign-key target cannot be resolved in selected models")
        target_table, target_col, on_update, on_delete = resolved[col]
        if on_update != "NO ACTION" or on_delete != "NO ACTION":
            raise ValueError("First PostgreSQL FK successor supports NO ACTION only")
        parent = model_by_table.get(target_table)
        if parent is None or not parent.__columns__[target_col].primary_key:
            raise ValueError("Foreign-key target must be a selected model primary key")
        if existing is not None or observed.foreign_keys:
            raise ValueError("Existing foreign-key constraint conflicts with proposal")
        # Enforce current referential integrity before even proposing DDL.
        orphans = connection.fetch_first(
            f"SELECT 1 AS present FROM {_quote(table, 'postgresql')} AS child "
            f"LEFT JOIN {_quote(target_table, 'postgresql')} AS parent "
            f"ON child.{_quote(col, 'postgresql')}=parent.{_quote(target_col, 'postgresql')} "
            f"WHERE child.{_quote(col, 'postgresql')} IS NOT NULL "
            f"AND parent.{_quote(target_col, 'postgresql')} IS NULL LIMIT 1"
        )
        if orphans:
            raise ValueError("Orphan records prevent adding the foreign key")
        sql = (
            f"ALTER TABLE {_quote(table, 'postgresql')} ADD CONSTRAINT "
            f"{_quote(name, 'postgresql')} FOREIGN KEY ({_quote(col, 'postgresql')}) "
            f"REFERENCES {_quote(target_table, 'postgresql')} "
            f"({_quote(target_col, 'postgresql')}) ON UPDATE NO ACTION ON DELETE NO ACTION"
        )
        kind = "add_foreign_key"
    else:
        previous_reference = next(
            old_col["references"]
            for old_table in before
            if old_table["name"] == table
            for old_col in old_table["columns"]
            if old_col["name"] == col
        )
        target_table, target_col = previous_reference.split(".")
        if existing != (col, target_table, target_col):
            raise ValueError("Only exact Aurora-owned foreign keys can be removed")
        if observed.foreign_keys != ((col, target_table, target_col),):
            raise ValueError("Foreign-key catalog and predecessor disagree")
        sql = (
            f"ALTER TABLE {_quote(table, 'postgresql')} DROP CONSTRAINT "
            f"{_quote(name, 'postgresql')}"
        )
        kind = "drop_foreign_key"
    operation = {
        "kind": kind,
        "table": table,
        "column": col,
        "name": name,
        "target_table": target_table,
        "target_column": target_col,
        "sql": sql,
    }
    payload = json.dumps(
        {
            "alias": alias,
            "identity": identity,
            "count": len(rows),
            "predecessor": predecessor["physical_schema"],
            "operation": operation,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return operation, sha256(payload.encode()).hexdigest(), len(rows)


def execute_fk(
    connection, models, root, alias, identity, parsed, operation, digest, title, configured_url
):
    from .connections import _postgres_database_identity

    with connection.transaction():
        lock = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )
        if not lock or lock["acquired"] is not True:
            raise RuntimeError("PostgreSQL migration lock unavailable")
        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database identity changed")
        current, current_digest, count = review_fk(connection, models, root, alias, identity)
        if current != operation or not hmac.compare_digest(current_digest, digest):
            raise RuntimeError("Foreign-key proposal changed after review")
        connection.execute_write(current["sql"])
        present = _fk_catalog(connection, current["table"], current["name"])
        expected = (current["column"], current["target_table"], current["target_column"])
        if (current["kind"] == "add_foreign_key" and present != expected) or (
            current["kind"] == "drop_foreign_key" and present is not None
        ):
            raise RuntimeError("Foreign-key DDL postcondition failed")
        verify_server_declared_schema(
            connection, models, history_tables={history_table(connection)}, allow_extended=True
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
        final, _ = read_history(connection, root, alias)
        if len(final) != count + 1 or any(final[-1][k] != v for k, v in record.items()):
            raise RuntimeError("Foreign-key history completion failed")
    return path, count + 1


def run_fk(args, connection, models, root, alias, identity, parsed, configured_url):
    if connection.dialect == "mysql":
        return run_mysql_fk(args, connection, models, root, alias, identity, parsed, configured_url)
    from .migration_review import migration_title

    if project_is_production(root):
        raise PermissionError("Server foreign-key changes are disabled in production")
    if any(getattr(args, option, None) for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("Foreign-key changes require interactive review")
    operation, digest, _ = review_fk(connection, models, root, alias, identity)
    if operation["kind"] == "drop_foreign_key":
        if getattr(args, "confirm", None):
            raise PermissionError("Foreign-key removal requires interactive target acknowledgment")
    print(f"AuroraSQL migration — {alias} (postgresql)")
    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")
    print("  Reviewed SHA-256: " + digest)
    if operation["kind"] == "drop_foreign_key":
        print(
            "  RECOMMENDATION: Create a backup separately before removing this FK; backup is optional."
        )
        print("  Aurora will not automatically create or restore backups.")
    if getattr(args, "confirm", None) not in ("yes", "y"):
        if (
            not sys.stdin.isatty()
            or input("Apply this foreign-key change? [y/N]: ").strip().lower() != "y"
        ):
            print("Migration cancelled; no database changes made.")
            return 0
    if operation["kind"] == "drop_foreign_key":
        phrase = f"DROP FK {alias}.{operation['table']}.{operation['column']}"
        if input("Type " + phrase + " to acknowledge: ").strip() != phrase:
            print("Foreign-key removal cancelled; no database changes made.")
            return 0
    path, number = execute_fk(
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
    print(f"Applied Migration{number} to {alias}; snapshot: {path.name}")
    return 0


def _mysql_constraint(connection, table, name):
    """Prove the constraint's identity and exactly one source/target column."""
    rows = connection.fetch_all(
        "SELECT k.COLUMN_NAME AS source_column, k.REFERENCED_TABLE_NAME AS target_table, "
        "k.REFERENCED_COLUMN_NAME AS target_column, k.REFERENCED_TABLE_SCHEMA AS target_schema, "
        "k.ORDINAL_POSITION AS position, r.UPDATE_RULE AS update_rule, "
        "r.DELETE_RULE AS delete_rule, tc.CONSTRAINT_TYPE AS kind "
        "FROM information_schema.KEY_COLUMN_USAGE k "
        "JOIN information_schema.TABLE_CONSTRAINTS tc ON "
        "tc.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND tc.TABLE_NAME=k.TABLE_NAME "
        "AND tc.CONSTRAINT_NAME=k.CONSTRAINT_NAME "
        "JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON "
        "r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME "
        "AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME "
        "WHERE k.TABLE_SCHEMA=DATABASE() AND k.TABLE_NAME=:table "
        "AND k.CONSTRAINT_NAME=:name ORDER BY k.ORDINAL_POSITION",
        {"table": table, "name": name},
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("Foreign-key catalog identity is ambiguous")
    row = rows[0]
    if (
        row["kind"] != "FOREIGN KEY"
        or row["position"] != 1
        or row["target_schema"] != connection.fetch_first("SELECT DATABASE() AS db")["db"]
        or row["update_rule"] not in ("RESTRICT", "NO ACTION")
        or row["delete_rule"] not in ("RESTRICT", "NO ACTION")
        or any(not row[field] for field in ("source_column", "target_table", "target_column"))
    ):
        raise ValueError("Named foreign key does not match the supported contract")
    return (row["source_column"], row["target_table"], row["target_column"])


def _supporting_index(connection, table, name):
    """Return true only for a full-length ascending single-column B-tree index."""
    rows = connection.fetch_all(
        "SELECT COLUMN_NAME AS col, NON_UNIQUE AS non_unique, INDEX_TYPE AS method, "
        "SUB_PART AS prefix_length, COLLATION AS direction, SEQ_IN_INDEX AS position "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME=:table AND INDEX_NAME=:name ORDER BY SEQ_IN_INDEX",
        {"table": table, "name": name},
    )
    if not rows:
        return None
    if (
        len(rows) != 1
        or rows[0]["position"] != 1
        or rows[0]["non_unique"] != 1
        or (rows[0]["method"] or "").upper() != "BTREE"
        or rows[0]["prefix_length"] is not None
        or rows[0]["direction"] != "A"
        or not rows[0]["col"]
    ):
        raise ValueError("FK supporting index does not match the supported contract")
    return rows[0]["col"]


def _simple_table(connection, table, *, has_fk):
    kinds = connection.fetch_all(
        "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
        {"table": table},
    )
    expected = ["PRIMARY KEY"] + (["FOREIGN KEY"] if has_fk else [])
    if sorted(row["kind"] for row in kinds) != sorted(expected):
        raise ValueError("Other constraints require separate foreign-key review")
    trigger = connection.fetch_first(
        "SELECT 1 AS present FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
        {"table": table},
    )
    if trigger is not None:
        raise ValueError("Target table triggers require separate review")


def review_mysql_fk(connection, models, root, alias, identity):
    if connection.dialect != "mysql":
        raise ValueError("MySQL FK reviewer requires MySQL")
    ordered = validate_init_models(models, dialect="mysql", allow_extended=True)
    records, predecessor = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect="mysql", allow_v2=True)["tables"]
    if not single_fk_delta(predecessor["models"], desired):
        raise ValueError("Exactly one existing-column foreign-key change is required")
    before, after = _normalized_tables(predecessor["models"]), _normalized_tables(desired)
    changes = [
        (new_table["name"], new_col["name"], old_col["references"], new_col["references"])
        for old_table, new_table in zip(before, after)
        for old_col, new_col in zip(old_table["columns"], new_table["columns"])
        if old_col["references"] != new_col["references"]
    ]
    if len(changes) != 1:
        raise ValueError("Exactly one FK delta is required")
    table, col, old_target, new_target = changes[0]
    by_table = {model.__table_name__: model for model in ordered}
    source = by_table[table]
    field = source.__columns__[col]
    if field.primary_key or field.unique or field.not_null:
        raise ValueError("Foreign-key source must be an ordinary nullable existing column")
    observed = inspect_table(connection, table)
    if (
        not observed.exists
        or not observed.constraints_verified
        or observed.foreign_key_actions is None
        or observed.unique_columns
    ):
        raise ValueError("Source table metadata cannot be independently verified")
    if len(observed.foreign_keys) > 1:
        raise ValueError("Other foreign-key constraints require separate review")
    name = _fk_name(table, col)
    current = _mysql_constraint(connection, table, name)
    index = _supporting_index(connection, table, name)
    _simple_table(connection, table, has_fk=bool(old_target))
    if new_target:
        if set(observed.indexes) != {"PRIMARY"} or observed.foreign_keys or current or index:
            raise ValueError("Other indexes or foreign keys require separate review")
        resolved = _resolved_foreign_keys(source, ordered)
        if col not in resolved:
            raise ValueError("FK target is not a selected model")
        parent_table, parent_col, on_update, on_delete = resolved[col]
        if on_update != "NO ACTION" or on_delete != "NO ACTION":
            raise ValueError("Only NO ACTION foreign keys are supported")
        parent = by_table.get(parent_table)
        if parent is None or not parent.__columns__[parent_col].primary_key:
            raise ValueError("FK must reference the selected parent primary key")
        parent_info = inspect_table(connection, parent_table)
        source_col = next((item for item in observed.columns if item.name == col), None)
        parent_key = next((item for item in parent_info.columns if item.name == parent_col), None)
        if (
            not parent_info.exists
            or not parent_info.constraints_verified
            or source_col is None
            or parent_key is None
            or not source_col.nullable
            or not parent_key.primary_key
            or source_col.sql_type.lower() != parent_key.sql_type.lower()
        ):
            raise ValueError("FK columns or parent key are incompatible")
        orphan = connection.fetch_first(
            f"SELECT 1 AS present FROM {_quote(table, 'mysql')} child "
            f"LEFT JOIN {_quote(parent_table, 'mysql')} parent "
            f"ON child.{_quote(col, 'mysql')}=parent.{_quote(parent_col, 'mysql')} "
            f"WHERE child.{_quote(col, 'mysql')} IS NOT NULL "
            f"AND parent.{_quote(parent_col, 'mysql')} IS NULL LIMIT 1"
        )
        if orphan:
            raise ValueError("Orphan records prevent adding the foreign key")
        sql = (
            f"ALTER TABLE {_quote(table, 'mysql')} "
            f"ADD INDEX {_quote(name, 'mysql')} ({_quote(col, 'mysql')}), "
            f"ADD CONSTRAINT {_quote(name, 'mysql')} FOREIGN KEY ({_quote(col, 'mysql')}) "
            f"REFERENCES {_quote(parent_table, 'mysql')} ({_quote(parent_col, 'mysql')}) "
            "ON UPDATE NO ACTION ON DELETE NO ACTION"
        )
        kind = "add_foreign_key"
    else:
        parent_table, parent_col = old_target.split(".")
        if (
            current != (col, parent_table, parent_col)
            or index != col
            or set(observed.indexes) != {"PRIMARY", name}
            or observed.foreign_keys != ((col, parent_table, parent_col),)
        ):
            raise ValueError("Only exact Aurora-owned FK and supporting index can be removed")
        # Avoid removing an index needed by an unrelated external FK.
        dependents = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE CONSTRAINT_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME=:table "
            "AND TABLE_NAME<>:table LIMIT 1",
            {"table": table},
        )
        if dependents:
            raise ValueError("External FK dependencies require separate review")
        sql = (
            f"ALTER TABLE {_quote(table, 'mysql')} "
            f"DROP FOREIGN KEY {_quote(name, 'mysql')}, "
            f"DROP INDEX {_quote(name, 'mysql')}"
        )
        kind = "drop_foreign_key"
    operation = {
        "kind": kind,
        "table": table,
        "column": col,
        "name": name,
        "target_table": parent_table,
        "target_column": parent_col,
        "sql": sql,
    }
    encoded = json.dumps(
        {
            "alias": alias,
            "identity": identity,
            "count": len(records),
            "predecessor": predecessor["physical_schema"],
            "operation": operation,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return operation, sha256(encoded.encode()).hexdigest(), len(records)


def execute_mysql_fk(
    connection, models, root, alias, identity, parsed, operation, digest, title, configured_url
):
    from .connections import _mysql_database_identity

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database identity changed")
        fresh, fresh_digest, count = review_mysql_fk(connection, models, root, alias, identity)
        if fresh != operation or not hmac.compare_digest(digest, fresh_digest):
            raise RuntimeError("FK proposal changed after review")
        try:
            connection.execute_write(fresh["sql"])
            expected = (fresh["column"], fresh["target_table"], fresh["target_column"])
            present = _mysql_constraint(connection, fresh["table"], fresh["name"])
            support = _supporting_index(connection, fresh["table"], fresh["name"])
            if fresh["kind"] == "add_foreign_key":
                if present != expected or support != fresh["column"]:
                    raise RuntimeError("FK postcondition failed")
            elif present is not None or support is not None:
                raise RuntimeError("FK removal postcondition failed")
            verify_server_declared_schema(
                connection, models, history_tables={history_table(connection)}, allow_extended=True
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
            final, _ = read_history(connection, root, alias)
            if len(final) != count + 1 or any(final[-1][k] != v for k, v in record.items()):
                raise RuntimeError("FK history completion failed")
            return path, count + 1
        except Exception as exc:
            raise RuntimeError(
                "MySQL FK outcome uncertain after DDL began; "
                "inspect physical schema and history before retry"
            ) from exc


def run_mysql_fk(args, connection, models, root, alias, identity, parsed, configured_url):
    from .migration_review import migration_title

    if project_is_production(root):
        raise PermissionError("Server foreign-key changes disabled in production")
    if any(getattr(args, option, None) for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("Foreign-key changes require interactive review")
    operation, digest, _ = review_mysql_fk(connection, models, root, alias, identity)
    if operation["kind"] == "drop_foreign_key":
        if getattr(args, "confirm", None):
            raise PermissionError("FK removal requires interactive target acknowledgment")
    print(f"AuroraSQL migration — {alias} (mysql)")
    print(f"  {operation['kind']}: {operation['table']}.{operation['column']}")
    print("  Reviewed SHA-256: " + digest)
    if operation["kind"] == "drop_foreign_key":
        print(
            "  RECOMMENDATION: Create a backup separately before removing this FK; backup is optional."
        )
        print("  Aurora will not automatically create or restore backups.")
    if getattr(args, "confirm", None) not in ("yes", "y"):
        if (
            not sys.stdin.isatty()
            or input("Apply this foreign-key change? [y/N]: ").strip().lower() != "y"
        ):
            print("Migration cancelled; no database changes made.")
            return 0
    if operation["kind"] == "drop_foreign_key":
        phrase = f"DROP FK {alias}.{operation['table']}.{operation['column']}"
        if input("Type " + phrase + " to acknowledge: ").strip() != phrase:
            print("FK removal cancelled; no database changes made.")
            return 0
    path, number = execute_mysql_fk(
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
    print(f"Applied Migration{number} to {alias}; snapshot: {path.name}")
    return 0
