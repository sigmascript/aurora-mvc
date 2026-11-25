"""Guarded single-column additive DDL for AuroraSQL.

All three dialects use a separate journal. This is not a general migration
engine: unresolved outcomes always require manual investigation.
"""

from datetime import datetime, timezone
import hmac
from dataclasses import dataclass


from contextlib import closing
import sqlite3
from .schema_inspection import _quote as _sqlite_quote, _catalog
from .schema_inspection import verify_server_declared_schema
import re
from uuid import uuid4

from .migration_review import AddColumnReview, review_add_column, _column_sql, _rehearse_sqlite
from .migration_review import review_server_model_changes, review_server_column_widening
from .migration_review import snapshot_fingerprint
from .migration_review import validate_review, verify_review_acknowledgment
from .migration_coordination import migration_lock
from .migration_coordination import _PG_LOCK_KEY
from .migration_history import review_history
from .migration_history import read_history, history_table
from .schema_inspection import inspect_table, _reflected_type_name
from .models import _identifier
from .schema import _quote
from .schema import _default_literal
from .schema import create_table_sql

# Explicit SQLite schema operations are guarded by the same intent journal
# used by native forward migrations, not by the native timeline table.
from pathlib import Path
from aurora.core.project_policy import project_is_production
from .migration_journal import (
    _INTENT_TABLE,
    _require_journal,
    _validate_intent,
    _JOURNAL,
    AdditiveEntry,
    _verify_additive_journal,
    initialize_additive_history,
    list_additive_history,
)
from .migration_journal import _check_connection
from .migration_review import _fingerprint
from .schema_inspection import _catalog_digest, _native_history, _inspect_sqlite
from .migration_timeline import prepare_successor_mapping, complete_successor
from .migration_timeline import _TABLE
from .migration_timeline import inspect_native_history, prepare_successor
from .initialization import _write_snapshot
from .connections import Connection
from .model_discovery import model_identity_map
from .schema_planning import plan_schema
from .snapshots import schema_snapshot


_CREATE_JOURNAL = "aurorasql_schema_history"


def _source_columns(before, table):
    _identifier(table)
    return {
        item["name"]: item
        for item in next(entry for entry in before["tables"] if entry["name"] == table)["columns"]
    }


def _verify_sqlite_source(connection, before, table):
    """Reject SQLite schema features beyond the audited simple-column subset."""
    live = inspect_table(connection, table)

    if not live.exists or not live.constraints_verified or not live.uniqueness_verified:
        raise RuntimeError("Live table cannot be fully verified")

    declared = _source_columns(before, table)
    actual = {item.name: item for item in live.columns}

    if declared.keys() != actual.keys():
        raise RuntimeError("Live schema columns differ from approved source")

    if live.unique_columns or live.indexes:
        raise RuntimeError("Indexed or uniquely constrained tables need separate verification")

    if connection.fetch_all(f'PRAGMA foreign_key_list("{table}")'):
        raise RuntimeError("Foreign keys require separate verification")

    for catalog in ("sqlite_master", "sqlite_temp_master"):
        if (
            connection.fetch_first(
                f"SELECT name FROM {catalog} WHERE type='trigger' AND tbl_name=:name",
                {"name": table},
            )
            is not None
        ):
            raise RuntimeError("Tables with triggers require separate verification")

    row = connection.fetch_first(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
        {"name": table},
    )

    if row is None or not isinstance(row["sql"], str):
        raise RuntimeError("Missing table DDL")

    if re.search(
        r"\b(CHECK|REFERENCES|GENERATED|COLLATE|ON\s+CONFLICT|WITHOUT\s+ROWID|STRICT)\b",
        row["sql"],
        re.I,
    ):
        raise RuntimeError("Advanced schema constraints require separate verification")

    for name, field in declared.items():
        observed = actual[name]

        if (
            field["unique"]
            or field["references"] is not None
            or field["server_default"] is not None
        ):
            raise RuntimeError("Advanced source column semantics are not supported")

        if field["primary_key"] != observed.primary_key:
            raise RuntimeError("Primary key drift")

        if not field["primary_key"] and field["nullable"] != observed.nullable:
            raise RuntimeError("Nullability drift")

        if observed.default_sql is not None:
            raise RuntimeError("Unexpected live server default")

        expected = _column_sql(field, "sqlite").upper() if not field["primary_key"] else "INTEGER"

        if field["primary_key"] and field["type"] != "int":
            raise RuntimeError("Non-integer primary key needs separate verification")

        if observed.sql_type.upper() != expected:
            raise RuntimeError("Live SQL type differs from source snapshot")


def _verify_remote_source(connection, before, table):
    """Only permit an ordinary table with one BIGINT primary key and plain fields.

    Fail closed on constraints, indexes, triggers, generated columns and
    unrecognized type representations. No assumption of broad schema equivalence.
    """
    dialect = connection.dialect
    declared = _source_columns(before, table)
    live = inspect_table(connection, table)

    if not live.exists or not live.uniqueness_verified:
        raise RuntimeError("Live table cannot be verified")

    actual = {item.name: item for item in live.columns}

    if actual.keys() != declared.keys():
        raise RuntimeError("Live columns differ from source snapshot")

    keys = [name for name, field in declared.items() if field["primary_key"]]

    if len(keys) != 1 or declared[keys[0]]["type"] != "int":
        raise RuntimeError("Only a single integer primary key is supported")

    if len(live.indexes) != 1 or live.unique_columns:
        raise RuntimeError("Additional indexes are not supported")

    if dialect == "mysql":
        # Fail closed for checks, FKs, triggers and generated columns.
        constraints = connection.fetch_all(
            "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": table},
        )

        if len(constraints) != 1 or constraints[0]["kind"] != "PRIMARY KEY":
            raise RuntimeError("Unsupported MySQL table constraints")

        triggers = connection.fetch_first(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
            {"table": table},
        )

        if triggers is not None:
            raise RuntimeError("Triggers require separate verification")

        metadata = connection.fetch_all(
            "SELECT COLUMN_NAME AS name, EXTRA AS extra, GENERATION_EXPRESSION AS generation "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": table},
        )

        if len(metadata) != len(actual):
            raise RuntimeError("Column metadata incomplete")

        for item in metadata:
            extra = (item["extra"] or "").lower()

            if item["name"] == keys[0]:
                if extra != "auto_increment":
                    raise RuntimeError("Primary key identity differs")
            elif extra or item["generation"]:
                raise RuntimeError("Generated or unusual column detected")
    elif dialect == "postgresql":
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT count(*) FROM pg_catalog.pg_constraint k WHERE k.conrelid=c.oid) AS constraints, "
            "(SELECT count(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) AS triggers, "
            "(SELECT count(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {"table": table},
        )

        if (
            metadata is None
            or metadata["persistence"] != "p"
            or metadata["partitioned"]
            or metadata["inherited"]
            or metadata["rls"]
            or metadata["constraints"] != 1
            or metadata["triggers"] != 0
            or metadata["parents"] != 0
        ):
            raise RuntimeError("Unsupported PostgreSQL table features")

        identity = connection.fetch_all(
            "SELECT a.attname AS name, a.attidentity AS identity, a.attgenerated AS generated "
            "FROM pg_catalog.pg_attribute a WHERE a.attrelid=to_regclass(:table) "
            "AND a.attnum>0 AND NOT a.attisdropped",
            {"table": table},
        )

        if len(identity) != len(actual):
            raise RuntimeError("Column metadata incomplete")

        for item in identity:
            if item["name"] == keys[0]:
                if item["identity"] != "d":
                    raise RuntimeError("Primary key identity differs")
            elif item["identity"] or item["generated"]:
                raise RuntimeError("Generated column detected")
    else:
        raise ValueError("Unsupported backend")

    # Deliberately accept only an exact subset of AuroraSQL's own emitted types.
    aliases = {
        "mysql": {
            "BOOLEAN": "tinyint(1)",
            "INTEGER": "int",
            "BIGINT": "bigint",
            "DOUBLE": "double",
            "DATE": "date",
            "DATETIME": "datetime",
            "TEXT": "text",
        },
        "postgresql": {
            "BOOLEAN": "boolean",
            "INTEGER": "integer",
            "BIGINT": "bigint",
            "DOUBLE PRECISION": "double precision",
            "DATE": "date",
            "TIMESTAMP": "timestamp without time zone",
            "TEXT": "text",
        },
    }[dialect]

    for name, field in declared.items():
        observed = actual[name]

        if (
            field["unique"]
            or field["references"] is not None
            or field["server_default"] is not None
        ):
            raise RuntimeError("Unsupported source column semantics")

        if field["primary_key"] != observed.primary_key:
            raise RuntimeError("Primary key drift")

        if not field["primary_key"] and field["nullable"] != observed.nullable:
            raise RuntimeError("Nullability drift")

        if observed.default_sql is not None:
            raise RuntimeError("Unexpected server default")

        expected = "BIGINT" if field["primary_key"] else _column_sql(field, dialect)
        expected = aliases.get(expected, expected.lower())

        if observed.sql_type.lower() != _reflected_type_name(expected, dialect):
            raise RuntimeError("Live SQL type differs from source snapshot")


def _verify_added_column(connection, before, after, table, column, original):
    """Require the reviewed column to be visible before recording success.

    Reflection failures leave the durable journal unresolved. They must not
    trigger an automatic DDL retry, especially on nontransactional backends.
    """
    live = inspect_table(connection, table)

    if (
        not live.exists
        or not live.uniqueness_verified
        or live.constraints_verified != original.constraints_verified
    ):
        raise RuntimeError("Post-migration table cannot be verified")

    if live.indexes != original.indexes or live.unique_columns != original.unique_columns:
        raise RuntimeError("Post-migration indexes or uniqueness differ from source")

    previous = _source_columns(before, table)
    expected_table = next(item for item in after["tables"] if item["name"] == table)
    expected_field = next(item for item in expected_table["columns"] if item["name"] == column)
    actual = {item.name: item for item in live.columns}

    if set(actual) != set(previous) | {column}:
        raise RuntimeError("Post-migration column inventory differs from review")

    # An ADD COLUMN must not silently change existing column definitions.
    old_columns = {item.name: item for item in original.columns}

    if any(actual.get(name) != field for name, field in old_columns.items()):
        raise RuntimeError("Post-migration existing columns differ from source")

    observed = actual[column]
    expected_type = _column_sql(expected_field, connection.dialect).lower()
    reflected_type = _reflected_type_name(observed.sql_type, connection.dialect)

    # These are dialect spellings of the same restricted column declarations.
    aliases = {
        "mysql": {"boolean": "tinyint(1)"},
        "postgresql": {
            "boolean": "boolean",
            "timestamp": "timestamp without time zone",
        },
    }
    expected_type = aliases.get(connection.dialect, {}).get(expected_type, expected_type)

    expected_type = _reflected_type_name(expected_type, connection.dialect)

    if (
        observed.primary_key
        or not observed.nullable
        or observed.default_sql is not None
        or reflected_type != expected_type
    ):
        raise RuntimeError("Post-migration column differs from approved declaration")

    if column in live.unique_columns:
        raise RuntimeError("Post-migration column has an unexpected unique constraint")


def execute_add_column_recorded(connection, before, after, plan, approval, *, confirm=False):
    """Apply one approved nullable column with durable, fail-closed audit evidence."""

    if not confirm:
        raise PermissionError("Explicit confirm=True required")

    if not isinstance(approval, AddColumnReview) or approval.execution_allowed is not False:
        raise ValueError("Expected exact non-executable review token")

    with migration_lock(connection):
        _verify_additive_journal(connection)

        if inspect_table(connection, _CREATE_JOURNAL).exists and review_history(connection):
            raise RuntimeError("Unresolved legacy create history")

        entries = list_additive_history(connection)

        if any(item.status != "applied" for item in entries):
            raise RuntimeError("Unresolved additive history requires manual investigation")

        if any(item.migration_id == plan.get("id") for item in entries):
            raise RuntimeError("Migration identifier already recorded")

        expected = review_add_column(connection, before, after, plan)

        if expected != approval:
            raise ValueError("Approval does not match reviewed operation")

        if any(item.table == approval.table and item.column == approval.column for item in entries):
            raise RuntimeError("Column previously migrated; refusing reapplication")

        if connection.dialect == "sqlite":
            _verify_sqlite_source(connection, before, approval.table)
        else:
            _verify_remote_source(connection, before, approval.table)

        original = inspect_table(connection, approval.table)
        op_id = str(uuid4())
        journal = _quote(_JOURNAL, connection.dialect)
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, migration_id, table_name, column_name, fingerprint, status) "
            "VALUES (:oid, :mid, :table, :column, :fingerprint, :status)",
            {
                "oid": op_id,
                "mid": approval.migration_id,
                "table": approval.table,
                "column": approval.column,
                "fingerprint": approval.plan_fingerprint,
                "status": "started",
            },
        )

        # Do not run irreversible DDL until the intent record is observable.
        # A successful DB-API write call alone does not prove the audit row exists.
        recorded = connection.fetch_first(
            f"SELECT migration_id, table_name, column_name, fingerprint, status "
            f"FROM {journal} WHERE operation_id=:oid",
            {"oid": op_id},
        )

        if recorded != {
            "migration_id": approval.migration_id,
            "table_name": approval.table,
            "column_name": approval.column,
            "fingerprint": approval.plan_fingerprint,
            "status": "started",
        }:
            raise RuntimeError("Migration intent journal could not be verified; DDL blocked")

        try:
            connection.execute_write(approval.sql_preview)
        except BaseException:
            try:
                connection.execute_write(
                    f"UPDATE {journal} SET status=:status "
                    "WHERE operation_id=:oid AND status='started'",
                    {"status": "uncertain", "oid": op_id},
                )
            except BaseException:
                pass

            raise
        # A successful DDL call alone does not establish schema equivalence.
        # A reflection failure preserves 'started' for manual investigation.
        _verify_added_column(connection, before, after, approval.table, approval.column, original)

        # Reapply the strict backend-specific table gate to the resulting schema.
        # Column/index comparisons alone cannot detect newly introduced triggers,
        # foreign keys, or other features outside the supported migration subset.
        if connection.dialect == "sqlite":
            _verify_sqlite_source(connection, after, approval.table)
        else:
            _verify_remote_source(connection, after, approval.table)

        # An audit failure is deliberately not retried: the 'started' record
        # remains as evidence for manual investigation.
        connection.execute_write(
            f"UPDATE {journal} SET status=:status WHERE operation_id=:oid AND status='started'",
            {"status": "applied", "oid": op_id},
        )

        # A write that returns without error can still affect zero rows.
        # Never report success unless the final journal state is observable.
        finalized = connection.fetch_first(
            f"SELECT migration_id, table_name, column_name, fingerprint, status "
            f"FROM {journal} WHERE operation_id=:oid",
            {"oid": op_id},
        )

        if finalized != {
            "migration_id": approval.migration_id,
            "table_name": approval.table,
            "column_name": approval.column,
            "fingerprint": approval.plan_fingerprint,
            "status": "applied",
        }:
            raise RuntimeError("Final migration journal could not be verified; outcome unresolved")

        return op_id


def execute_server_forward_migration(
    connection,
    models,
    root,
    alias,
    *,
    identity,
    parsed,
    review,
    approval,
    title,
    backup_path,
    config_path,
    login_path=None,
):
    """Verify predecessor and exact review under lock before one DDL."""
    from .cli_backup import backup_mysql_database, backup_postgres_database
    from .connections import _mysql_database_identity, _postgres_database_identity

    dialect = connection.dialect
    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Server forward migration requires MySQL or PostgreSQL")
    if project_is_production(root):
        raise PermissionError("Server migration execution is disabled in production")
    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Cannot nest server migration execution")
    if not identity or parsed is None or not isinstance(title, str) or not title.strip():
        raise ValueError("Verified database identity and migration title are required")
    if (
        len(review.operations) != 1
        or len(review.statements) != 1
        or review.operations[0]["kind"]
        not in ("create_table", "add_column", "add_columns", "widen_column", "alter_columns")
        or not hmac.compare_digest(str(approval), review.review_sha256)
    ):
        raise PermissionError("One exact approved safe server operation is required")

    # Backups are optional for five-column forward migrations. If explicitly
    # requested, the archive must be verified before any schema change.
    archive = None
    if dialect == "mysql":
        identity_fn = _mysql_database_identity
        if backup_path is not None:
            archive = backup_mysql_database(config_path, alias, backup_path, login_path)
    else:
        identity_fn = _postgres_database_identity
        if backup_path is not None:
            archive = backup_postgres_database(config_path, alias, backup_path)

    if backup_path is not None and (not isinstance(archive, Path) or not archive.is_file()):
        raise RuntimeError("Requested server backup could not be verified")

    def apply_locked():
        if identity_fn(connection, parsed) != identity:
            raise RuntimeError("Server database identity changed after review")

        rows, _ = read_history(connection, root, alias)
        history_name = history_table(connection)
        if review.operations[0]["kind"] == "widen_column":
            fresh = review_server_column_widening(connection, models, alias, identity)
        else:
            fresh = review_server_model_changes(connection, models, alias, identity)

        if (
            fresh.operations != review.operations
            or fresh.statements != review.statements
            or not hmac.compare_digest(fresh.review_sha256, review.review_sha256)
        ):
            raise RuntimeError("Server schema proposal changed after review")

        # MySQL DDL can commit even when execution or later evidence writing
        # fails. Once execution starts, preserve evidence and require manual
        # reconciliation rather than suggesting a safe automatic retry.
        try:
            connection.execute_write(fresh.statements[0])
            verify_server_declared_schema(connection, models, history_tables={history_name})
            snapshot = _write_snapshot(connection, root, alias, models, title)
            record = {
                "id": len(rows) + 1,
                "title": title,
                "snapshot": snapshot.name,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "warnings": None,
            }
            connection.execute_write(
                f"INSERT INTO {history_name} (id,title,snapshot,date,warnings) "
                "VALUES (:id,:title,:snapshot,:date,:warnings)",
                record,
            )
            verified_rows, _ = read_history(connection, root, alias)
            if len(verified_rows) != record["id"] or any(
                verified_rows[-1][key] != value for key, value in record.items()
            ):
                raise RuntimeError("Server migration history completion was not verified")
        except Exception as exc:
            if dialect == "mysql":
                raise RuntimeError(
                    "MySQL migration outcome uncertain after DDL execution began; "
                    "inspect physical schema, migration history, and snapshots "
                    "before any retry"
                ) from exc
            raise

        return snapshot

    if dialect == "postgresql":
        with connection.transaction():
            locked = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired",
                {"key": _PG_LOCK_KEY},
            )
            if not locked or locked["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            path = apply_locked()
    else:
        with migration_lock(connection):
            path = apply_locked()

    # Retain durable file evidence on errors and uncertain commits.
    return path, archive


def _sql_statements(operation):
    table = _sqlite_quote(operation["table"])

    if operation["kind"] == "rename_table":
        return [f"ALTER TABLE {table} RENAME TO {_sqlite_quote(operation['new_table'])}"]

    if operation["kind"] == "create_index":
        columns = ", ".join(_sqlite_quote(name) for name in operation["columns"])
        prefix = "CREATE UNIQUE INDEX" if operation["unique"] else "CREATE INDEX"
        return [f"{prefix} {_sqlite_quote(operation['name'])} ON {table} ({columns})"]

    if operation["kind"] == "drop_index":
        return [f"DROP INDEX {_sqlite_quote(operation['name'])}"]

    return [
        f"ALTER TABLE {table} RENAME COLUMN {_sqlite_quote(old)} TO {_sqlite_quote(new)}"
        for old, new in operation["columns"].items()
    ]


def _apply_sqlite_operations(args, document, db_file, preview_digest, *, model_mapping=None):
    """Apply one reviewed SQLite operation after a bounded rehearsal."""
    if project_is_production(Path(args.config).expanduser().resolve().parent):
        raise PermissionError("Explicit schema migration is disabled in production")

    if args.backup:
        raise ValueError(
            "Migration backup destinations are retired; run aurora db backup separately"
        )

    if (
        args.confirm != "APPLY"
        or not isinstance(args.approve, str)
        or not hmac.compare_digest(args.approve, preview_digest)
    ):
        raise PermissionError("Require --confirm APPLY and exact reviewed approval")

    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as probe:
        native = (
            probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aurorasql_migration_timeline'"
            ).fetchone()
            is not None
        )

    if getattr(args, "title", None) is not None and not native:
        raise ValueError("Migration titles require native linear history")

    title = "Untitled"

    if native:
        from .migration_review import migration_title

        title = migration_title(
            "Untitled"
            if getattr(args, "_simple_confirm", False) and getattr(args, "title", None) is None
            else getattr(args, "title", None),
            "Untitled",
        )

    from .snapshots import prepare_schema_record, write_schema_record

    project_root = Path(args.config).expanduser().resolve().parent
    prepare_schema_record(project_root, db_file, ())

    statements = _sql_statements(document["operation"])
    expected_catalog = _rehearse_sqlite(db_file, statements)

    connection = sqlite3.connect(
        db_file.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
    )

    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA legacy_alter_table=OFF")
        connection.execute("BEGIN IMMEDIATE")
        before, warnings = _inspect_sqlite(
            connection,
            document["operation"],
            args.database,
            trusted_rename=model_mapping is not None,
        )
        current_digest = _fingerprint(document, before, "sqlite", db_file)

        if warnings or not hmac.compare_digest(current_digest, preview_digest):
            raise RuntimeError("Schema changed or migration preflight is unresolved")

        _require_journal(connection)
        operation_id = "schema_" + preview_digest[:40]
        before_sha = _catalog_digest(before)
        after_sha = _catalog_digest(expected_catalog)
        _validate_intent(operation_id, preview_digest, before_sha, after_sha)
        native = None

        if (
            document["operation"]["kind"] in ("create_index", "rename_columns")
            or model_mapping is not None
        ):
            native = _native_history(connection, args.database)

        successor_number = None

        if native is not None:
            successor_number = prepare_successor_mapping(
                connection,
                args.database,
                operation_id,
                preview_digest,
                model_mapping if model_mapping is not None else native[-1]["model_tables"],
                title,
            )

        initial_count = connection.execute(
            f"SELECT COUNT(*) FROM {_quote(document['operation']['table'])}"
        ).fetchone()[0]

        # The intent and DDL commit atomically on SQLite, never on MySQL.
        connection.execute(
            f'INSERT INTO "{_INTENT_TABLE}" (migration_id,review_sha256,before_sha256,after_sha256,state) '
            "VALUES (?,?,?,?,?)",
            (operation_id, preview_digest, before_sha, after_sha, "prepared"),
        )

        for statement in statements:
            connection.execute(statement)

        actual = _catalog(connection)

        if actual != expected_catalog:
            raise RuntimeError("Executed schema differs from rehearsal")

        new_table = document["operation"].get("new_table", document["operation"]["table"])
        final_count = connection.execute(f"SELECT COUNT(*) FROM {_quote(new_table)}").fetchone()[0]

        if initial_count != final_count:
            raise RuntimeError("Migration unexpectedly changed row count")

        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Migration created a foreign-key violation")

        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Migration integrity verification failed")

        changed = connection.execute(
            f'UPDATE "{_INTENT_TABLE}" SET state=? WHERE migration_id=? AND state=?',
            ("applied", operation_id, "prepared"),
        ).rowcount

        if changed != 1:
            raise RuntimeError("Intent journal transition failed")

        if successor_number is not None:
            complete_successor(connection, successor_number, operation_id)

            verified = _native_history(connection, args.database)

            if (
                verified is None
                or verified[-1]["number"] != successor_number
                or not verified[-1]["physical_history_consistent"]
            ):
                raise RuntimeError("Native history postcondition failed")

        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    try:
        record = write_schema_record(project_root, db_file, (), title)
    except Exception:
        print(
            "Database changed, but schema snapshot was not saved. "
            "Inspect history; do not retry this migration blindly."
        )
        return 4

    print(f"Applied SQLite {document['operation']['kind']} on {args.database}.")
    print(f"Schema snapshot: {record}")
    return 0


# Guarded SQLite additive and column-edit execution (consolidated R4.9).
# Guarded additive SQLite migrations for an existing, strictly verified schema.
#
# Only plain nullable columns and new tables are supported. This deliberately
# rejects noncanonical pre-existing DDL rather than guessing at equivalence.


@dataclass(frozen=True)
class SQLiteAdditiveResult:
    migration_id: str
    created_tables: tuple[str, ...]
    added_columns: tuple[tuple[str, str], ...]


def _objects(conn, *, native=False):
    allowed = {_TABLE}

    if native:
        allowed.update({"aurorasql_schema_history", "aurorasql_migration_timeline"})

    return {
        (kind, name): sql
        for kind, name, sql in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
        if name not in allowed
    }


def _verify_result_catalog(connection, expected, table_names, *, native=False):
    """Compare resulting catalog structures without relying on ALTER SQL text."""
    actual_objects = _objects(connection, native=native)
    expected_objects = _objects(expected)

    if set(actual_objects) != set(expected_objects):
        raise RuntimeError("Unexpected SQLite catalog objects after migration")

    for key, sql in expected_objects.items():
        if key[0] != "table" and actual_objects[key] != sql:
            raise RuntimeError("Resulting SQLite catalog definition differs from models")

    for name in sorted(table_names):
        quoted = '"' + name.replace('"', '""') + '"'

        for pragma in ("table_info", "foreign_key_list", "index_list"):
            statement = f"PRAGMA {pragma}({quoted})"

            if connection.execute(statement).fetchall() != expected.execute(statement).fetchall():
                raise RuntimeError(f"Resulting SQLite {pragma} differs from models")

        indexes = expected.execute(f"PRAGMA index_list({quoted})").fetchall()

        for index in indexes:
            index_name = '"' + index[1].replace('"', '""') + '"'
            statement = f"PRAGMA index_xinfo({index_name})"

            if connection.execute(statement).fetchall() != expected.execute(statement).fetchall():
                raise RuntimeError("Resulting SQLite index definition differs from models")


def _expected_before(snapshot):
    """Build an independent SQLite catalog from reviewed baseline DDL."""
    db = sqlite3.connect(":memory:")

    try:
        db.execute("PRAGMA foreign_keys=ON")

        for table in snapshot["tables"]:
            sql = table.get("create_sql_preview")

            if not isinstance(sql, str) or not sql.startswith("CREATE TABLE "):
                raise ValueError("Baseline lacks canonical CREATE TABLE SQL")

            db.execute(sql)

        return _objects(db)
    finally:
        db.close()


def _verify_baseline(conn, snapshot, *, native=False):
    expected = _expected_before(snapshot)
    actual = _objects(conn, native=native)

    if actual != expected:
        raise RuntimeError("Live SQLite catalog differs from reviewed baseline")

    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Existing foreign key violation")


def _sqlite_additive_column_sql(model, name, *, allow_empty_required=False):
    """Render an ordinary column; undefaulted NOT NULL needs empty-table proof."""
    field = model.__columns__[name]

    if (
        field.primary_key
        or field.unique
        or field.references is not None
        or field.server_default is not None
        or (not field.nullable and field.default is None and not allow_empty_required)
    ):
        raise ValueError("Only ordinary nullable or defaulted required columns are supported")
    # Reuse AuroraSQL's own DDL renderer; avoid a second type-mapping engine.
    from datetime import date, datetime

    from .schema import _SQL_TYPES, _default_literal

    if field.sql_type:
        kind = field.sql_type
    elif field.python_type is date:
        kind = "DATE"
    elif field.python_type is datetime:
        kind = "TIMESTAMP"
    elif field.python_type is str and field.length is not None:
        kind = f"VARCHAR({field.length})"
    else:
        kind = _SQL_TYPES["sqlite"][field.python_type]

    default_sql = _default_literal(field, "sqlite")
    nullability = " NOT NULL" if not field.nullable else ""
    suffix = f" DEFAULT {default_sql}" if default_sql is not None else ""
    return f'"{name}" {kind}{nullability}{suffix}'


def execute_sqlite_additive_migration(
    connection, review, models, *, reviewed_sha256, confirm=False, alias="default", title="Untitled"
):
    """Apply one approved, additive migration atomically on an exact baseline.

    A noncanonical baseline, unknown object, pending journal entry, or any
    unsupported operation aborts before application DDL. No auto-recovery.
    """

    if confirm is not True:
        raise PermissionError("Explicit confirm=True required")

    _check_connection(connection)
    validate_review(review)
    verify_review_acknowledgment(review, reviewed_sha256=reviewed_sha256)
    plan = review["plan"]

    if plan["dialect"] != "sqlite":
        raise ValueError("Only SQLite supported")

    operations = plan["operations"]

    if not operations or any(
        op["kind"] not in ("added_table", "added_column") or op["risk"] != "additive"
        for op in operations
    ):
        raise ValueError("Only additive table and nullable column operations supported")

    models = tuple(models)
    after = schema_snapshot(*models, dialect="sqlite")

    if snapshot_fingerprint(after) != plan["after_fingerprint"]:
        raise ValueError("Models differ from reviewed after-snapshot")

    model_by_name = {m.__table_name__: m for m in models}
    before_names = {t["name"] for t in review["before"]["tables"]}
    after_names = set(model_by_name)

    if before_names - after_names:
        raise ValueError("Existing model tables missing")

    new_names = {op["table"] for op in operations if op["kind"] == "added_table"}

    if new_names != after_names - before_names:
        raise ValueError("Added table operations mismatch")

    added = [(op["table"], op["column"]) for op in operations if op["kind"] == "added_column"]

    for table, col in added:
        if table not in before_names or col not in model_by_name[table].__columns__:
            raise ValueError("Added column operation mismatch")

        _sqlite_additive_column_sql(model_by_name[table], col)

    _validate_intent(
        plan["id"], review["review_sha256"], plan["before_fingerprint"], plan["after_fingerprint"]
    )

    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign_keys must be enabled")

    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_journal(connection)
        from .connections import Connection

        native_connection = Connection(connection, "sqlite")
        native, _, _ = inspect_native_history(native_connection, alias)

        if native is not None and not native[-1]["physical_history_consistent"]:
            raise RuntimeError("Native history is not verified; migration refused")

        _verify_baseline(connection, review["before"], native=native is not None)

        entries = connection.execute(
            f'SELECT migration_id,review_sha256,before_sha256,after_sha256,state FROM "{_TABLE}"'
        ).fetchall()

        for entry in entries:
            _validate_intent(*entry[:4])

            if entry[4] != "applied" or entry[0] == plan["id"]:
                raise RuntimeError("Unresolved or duplicate migration journal entry")

        successor_number = None

        if native is not None:
            successor_number = prepare_successor(
                connection, alias, plan["id"], review["review_sha256"], models, title
            )

        connection.execute(
            f'INSERT INTO "{_TABLE}" VALUES (?,?,?,?,?)',
            (
                plan["id"],
                review["review_sha256"],
                plan["before_fingerprint"],
                plan["after_fingerprint"],
                "prepared",
            ),
        )

        for name in sorted(new_names):
            connection.execute(create_table_sql(model_by_name[name], "sqlite"))

        for table, col in added:
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN {_sqlite_additive_column_sql(model_by_name[table], col)}'
            )

        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Foreign key integrity failure")
        # Verify exact column metadata against independently rendered target DDL.
        # SQLite ALTER TABLE preserves the original SQL text, so compare PRAGMA
        # metadata instead of sqlite_master SQL for the final state.
        expected = sqlite3.connect(":memory:")

        try:
            for model in models:
                expected.execute(create_table_sql(model, "sqlite"))

            _verify_result_catalog(connection, expected, model_by_name, native=native is not None)
        finally:
            expected.close()

        changed = connection.execute(
            f'UPDATE "{_TABLE}" SET state=? WHERE migration_id=? AND state=?',
            ("applied", plan["id"], "prepared"),
        ).rowcount

        if changed != 1:
            raise RuntimeError("Journal transition failed")

        if successor_number is not None:
            complete_successor(connection, successor_number, plan["id"])

        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return SQLiteAdditiveResult(plan["id"], tuple(sorted(new_names)), tuple(added))


# Guarded SQLite model-driven column changes and uniqueness.
_VARCHAR = re.compile(r"VARCHAR\((\d+)\)\Z", re.I)
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)\Z", re.I)


def _compatible_type(old, new):
    if old.upper() == new.upper():
        return True

    old_text, new_text = _VARCHAR.fullmatch(old), _VARCHAR.fullmatch(new)

    if old_text and new_text:
        return int(new_text[1]) >= int(old_text[1])

    old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)

    if old_decimal and new_decimal:
        before_p, before_s = map(int, old_decimal.groups())
        after_p, after_s = map(int, new_decimal.groups())
        return after_s >= before_s and after_p - after_s >= before_p - before_s

    return False


def sqlite_column_edit_proposal(connection, models, alias, db_file, *, forward=False):
    """Return a single ordinary-table edit or None when no such edit exists."""
    mapping = model_identity_map(models)
    if not forward:
        timeline, _, _ = inspect_native_history(connection, alias)
        if not timeline or not timeline[-1]["physical_history_consistent"]:
            raise RuntimeError("Column editing requires verified native history")
        if mapping != timeline[-1]["model_tables"]:
            return None

    target = None
    operations = []

    for model in models:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if target is not None or not plan.exists:
            return None

        accepted = {"type_mismatch", "nullability_mismatch", "default_mismatch", "unique_mismatch"}

        if any(change.kind not in accepted for change in plan.changes):
            return None

        observed = {
            field.name: field for field in inspect_table(connection, model.__table_name__).columns
        }

        if set(observed) != set(model.__columns__):
            return None

        table = model.__table_name__
        quoted = '"' + table + '"'
        objects = _catalog(connection._raw)

        if any(
            kind in ("view", "trigger") or (kind == "index" and parent == table)
            for kind, _, parent, _ in objects
        ):
            raise RuntimeError("Column edit with schema dependencies is unsupported")

        for other in models:
            keys = connection._raw.execute(
                f'PRAGMA foreign_key_list("{other.__table_name__}")'
            ).fetchall()

            if other.__table_name__ == table and keys or any(row[2] == table for row in keys):
                raise RuntimeError("Column edit with foreign-key dependencies is unsupported")

        # A unique-constraint transition is reviewed separately from column
        # type/default/nullability edits, so its data and index effects remain clear.
        unique_changes = [change for change in plan.changes if change.kind == "unique_mismatch"]
        if unique_changes and len(plan.changes) != 1:
            return None

        for change in plan.changes:
            field = model.__columns__[change.column]
            current = observed[change.column]

            if change.kind == "unique_mismatch":
                if field.primary_key or field.references is not None:
                    raise ValueError("Key or foreign-key changes need separate reviewed intent")
                if field.unique:
                    quoted_column = '"' + change.column + '"'
                    duplicate = connection._raw.execute(
                        f"SELECT 1 FROM {quoted} WHERE {quoted_column} IS NOT NULL "
                        f"GROUP BY {quoted_column} HAVING COUNT(*) > 1 LIMIT 1"
                    ).fetchone()
                    if duplicate:
                        raise ValueError("Existing duplicate values prevent UNIQUE constraint")
            elif (
                field.primary_key
                or field.unique
                or field.references is not None
                or field.server_default is not None
            ):
                raise ValueError("Key or constraint editing needs separate reviewed intent")

            if change.kind == "type_mismatch":
                from .schema_planning import _sqlite_declared_type

                if not _compatible_type(current.sql_type, _sqlite_declared_type(field)):
                    raise ValueError("Unsupported or narrowing column type change")

            if change.kind == "nullability_mismatch" and not field.nullable:
                count = connection._raw.execute(
                    f'SELECT COUNT(*) FROM {quoted} WHERE "{change.column}" IS NULL'
                ).fetchone()[0]

                if count:
                    raise ValueError("Existing NULL values prevent required column edit")

            if change.kind == "default_mismatch":
                _default_literal(field, "sqlite")

            operations.append({"kind": change.kind, "table": table, "column": change.column})

        target = model

    if target is None:
        return None

    after = schema_snapshot(*models, dialect="sqlite")
    document = {
        "database": alias,
        "operations": operations,
        "after": after,
        "model_identity": mapping,
    }
    catalog = _catalog(connection._raw)
    digest = _fingerprint(document, catalog, "sqlite", db_file)
    temporary = "aurora_rebuild_" + digest[:24]
    target_sql = create_table_sql(target, "sqlite")
    create = target_sql.replace(f'"{target.__table_name__}"', f'"{temporary}"', 1)
    columns = ", ".join(f'"{name}"' for name in target.__columns__)
    statements = [
        create,
        f'INSERT INTO "{temporary}" ({columns}) SELECT {columns} FROM "{target.__table_name__}"',
        f'DROP TABLE "{target.__table_name__}"',
        f'ALTER TABLE "{temporary}" RENAME TO "{target.__table_name__}"',
    ]
    return document, digest, mapping, statements, target.__table_name__


def execute_sqlite_column_edit(raw, models, alias, proposal, *, approved, title):
    """Apply a reviewed edit atomically after an independent verified backup."""
    document, digest, mapping, statements, table = proposal

    if not hmac.compare_digest(approved, digest):
        raise PermissionError("Exact model-edit review approval required")

    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("BEGIN IMMEDIATE")

    try:
        connection = Connection(raw, "sqlite")
        current = sqlite_column_edit_proposal(
            connection, models, alias, raw.execute("PRAGMA database_list").fetchall()[0][2]
        )

        if current is None or current[1] != digest:
            raise RuntimeError("Column-edit review is stale")

        before = _catalog(raw)
        _require_journal(raw)
        operation_id = "edit_" + digest[:40]
        before_sha = _catalog_digest(before)
        _validate_intent(operation_id, digest, before_sha, before_sha)
        number = prepare_successor_mapping(raw, alias, operation_id, digest, mapping, title)
        raw.execute(
            f'INSERT INTO "{_TABLE}" VALUES (?,?,?,?,?)',
            (operation_id, digest, before_sha, before_sha, "prepared"),
        )

        for statement in statements:
            raw.execute(statement)

        after = _catalog(raw)
        after_sha = _catalog_digest(after)
        raw.execute(
            f'UPDATE "{_TABLE}" SET after_sha256=? WHERE migration_id=?', (after_sha, operation_id)
        )

        with sqlite3.connect(":memory:") as expected:
            for model in models:
                expected.execute(create_table_sql(model, "sqlite"))

            _verify_result_catalog(
                raw, expected, {model.__table_name__ for model in models}, native=True
            )

        if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Column edit violated foreign-key integrity")

        if (
            raw.execute(
                f'UPDATE "{_TABLE}" SET state=? WHERE migration_id=? AND state=?',
                ("applied", operation_id, "prepared"),
            ).rowcount
            != 1
        ):
            raise RuntimeError("Column-edit intent transition failed")

        complete_successor(raw, number, operation_id)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise

    return operation_id


# SQLite reviewed migration executors; never bypass recorded review or transaction checks.
def sqlite_apply_explicit_index(raw, root, db_file, models, title, operation):
    """Rehearse and apply one guarded ordinary index change with forward history."""
    from pathlib import Path

    from .constraints import matches_models_with_indexes, validate_index_operation
    from .migration_execution import _sql_statements
    from .migration_history import (
        sqlite_new_record as _new_record,
    )
    from .migration_history import (
        sqlite_read_history as read_history,
    )
    from .migration_review import _rehearse_sqlite
    from .schema import _quote, validate_init_models
    from .schema_inspection import _catalog

    validate_init_models(models, dialect="sqlite", allow_extended=True)
    expected = _rehearse_sqlite(Path(db_file), _sql_statements(operation))
    raw.execute("PRAGMA foreign_keys=ON")

    if raw.in_transaction or raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("Index migration requires an unlocked FK-enforced SQLite connection")
    path = None

    try:
        raw.execute("BEGIN IMMEDIATE")
        read_history(raw, root, db_file)
        validate_index_operation(raw, models, operation)

        before = raw.execute(
            f"SELECT count(*) FROM {_quote(operation['table'], 'sqlite')}"
        ).fetchone()[0]

        for sql in _sql_statements(operation):
            raw.execute(sql)

        if _catalog(raw) != expected:
            raise RuntimeError("Index schema differs from reviewed rehearsal")

        matches_models_with_indexes(raw, models)
        after = raw.execute(
            f"SELECT count(*) FROM {_quote(operation['table'], 'sqlite')}"
        ).fetchone()[0]

        if before != after:
            raise RuntimeError("Index migration changed application row count")

        path = _new_record(raw, root, db_file, models, title)
        raw.commit()

    except BaseException:
        raw.rollback()

        if path is not None:
            path.unlink(missing_ok=True)

        raise

    return path


def sqlite_migrate_column_edit(raw, root, db_file, models, title, proposal):
    """Rebuild one ordinary table under the same forward-history transaction.

    The proposal admits compatible widening, validated defaults, nullability
    and isolated single-column uniqueness changes without dependent objects.
    Never replay a proposal against unverified or concurrently changed schema.
    """
    from pathlib import Path

    from .connections import Connection
    from .migration_execution import sqlite_column_edit_proposal
    from .migration_history import (
        sqlite_new_record as _new_record,
    )
    from .migration_history import (
        sqlite_read_history as read_history,
    )
    from .migration_review import _rehearse_sqlite
    from .migration_review import sqlite_matches_models as matches_models
    from .schema import _quote
    from .schema_inspection import _catalog

    document, digest, _, statements, table = proposal
    expected = _rehearse_sqlite(Path(db_file), statements)
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    snapshot = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        read_history(raw, root, db_file)
        current = sqlite_column_edit_proposal(
            Connection(raw, "sqlite"),
            models,
            "default",
            db_file,
            forward=True,
        )
        if current is None or current[1] != digest:
            raise RuntimeError("Column-edit review is stale; run db check again")
        before = raw.execute(f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')}").fetchone()[0]
        for statement in statements:
            raw.execute(statement)
        if _catalog(raw) != expected:
            raise RuntimeError("Migration result differs from the reviewed schema")
        after = raw.execute(f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')}").fetchone()[0]
        if before != after:
            raise RuntimeError("Column edit unexpectedly changed the number of rows")
        matches_models(raw, models)
        snapshot = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        raise
    return snapshot


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_additions(raw, root, db_file, models, title):
    """Apply only safe new tables and nullable/defaulted columns, forward-only."""
    from .connections import Connection
    from .constraints import matches_models_with_indexes
    from .migration_execution import _sqlite_additive_column_sql
    from .migration_history import (
        _SQLITE_OLD as _OLD,
    )
    from .migration_history import (
        SQLITE_BACKUP_TABLE as BACKUP_TABLE,
    )
    from .migration_history import (
        SQLITE_LEGACY_TABLE as LEGACY_TABLE,
    )
    from .migration_history import (
        SQLITE_TABLE as TABLE,
    )
    from .migration_history import (
        sqlite_new_record as _new_record,
    )
    from .migration_history import (
        sqlite_read_history as read_history,
    )
    from .schema import _quote, create_table_sql, validate_init_models
    from .schema_planning import plan_schema

    ordered_models = validate_init_models(models, dialect="sqlite", allow_extended=True)
    if any(
        m.__table_name__ in (TABLE, LEGACY_TABLE, BACKUP_TABLE) or m.__table_name__ in _OLD
        for m in models
    ):
        raise ValueError("A model table name is reserved for Aurora migration history")
    path = None
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    conn = Connection(raw, "sqlite")
    _, old = read_history(raw, root, db_file)
    old_tables = {item["name"] for item in old["models"]}
    if old_tables - {model.__table_name__ for model in models}:
        raise ValueError("Removing tables requires a separately reviewed operation")
    operations = []
    for model in ordered_models:
        trusted = {
            item["name"]
            for item in old["physical_schema"]
            if item["type"] == "index" and item["sql"] is not None
        }
        plan = plan_schema(conn, model, models=models, trusted_indexes=trusted)
        if plan.warnings and plan.exists:
            raise ValueError("Unsupported model drift; migration refused")
        for change in plan.changes:
            if change.kind == "create_table":
                operations.append(create_table_sql(model, "sqlite", models=models))
            elif change.kind == "missing_column" and model.__table_name__ in old_tables:
                # Restrict additions to AuroraSQL's own validated SQL renderer.
                from .migration_execution import _sqlite_additive_column_sql

                name = change.column
                operations.append(
                    f"ALTER TABLE {_quote(model.__table_name__, 'sqlite')} ADD COLUMN {_sqlite_additive_column_sql(model, name)}"
                )
            else:
                raise ValueError("Unsupported model change; migration refused")
    if not operations:
        return None
    try:
        raw.execute("BEGIN IMMEDIATE")
        read_history(raw, root, db_file)
        for sql in operations:
            raw.execute(sql)
        matches_models_with_indexes(raw, models)
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_default_change(
    raw,
    root,
    db_file,
    models,
    title,
    proposal,
    *,
    kind="default_mismatch",
    reviewed_removals=(),
    acknowledged=(),
):
    """Rebuild one reviewed ordinary column change and retain existing rows."""
    import hashlib
    import re

    from .constraints import matches_models_with_indexes
    from .migration_history import sqlite_new_record as _new_record
    from .migration_review import (
        _incoming_foreign_keys,
        _review_ordinary_rebuild,
        review_column_removal,
    )
    from .schema import _quote, create_table_sql

    table, name, indexes, _ = proposal
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.in_transaction or raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("Default migration requires an unlocked FK-enforced SQLite connection")

    # SQLite cannot drop a referenced parent while FK enforcement is enabled.
    # For an approved UNIQUE-only edit, temporarily disable enforcement on
    # this private writer and verify all relationships before committing.
    incoming_unique = kind == "unique_mismatch" and bool(_incoming_foreign_keys(raw, table))
    if incoming_unique:
        raw.execute("PRAGMA foreign_keys=OFF")
        if raw.execute("PRAGMA foreign_keys").fetchone() != (0,):
            raise RuntimeError("Cannot prepare the reviewed parent-table rebuild")

    path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        if _review_ordinary_rebuild(raw, root, db_file, models, kind) != proposal:
            raise RuntimeError("Default-change review is stale; rerun db check")
        if reviewed_removals:
            actual = review_column_removal(raw, root, db_file, models, allow_mixed=True)
            if actual != reviewed_removals:
                raise RuntimeError("Column-removal review is stale; rerun db check")
            dangerous = {(table_name, col) for table_name, col, count, _ in actual if count}
            if dangerous and not dangerous.issubset(set(acknowledged)):
                raise PermissionError("Populated column removal requires typed authorization")
        target = next(m for m in models if m.__table_name__ == table)
        temporary = "aurora_rebuild_" + hashlib.sha256(repr(proposal).encode()).hexdigest()[:24]
        target_sql = create_table_sql(target, "sqlite", models=models)
        temporary_sql = target_sql.replace(_quote(table, "sqlite"), _quote(temporary, "sqlite"), 1)
        if temporary_sql == target_sql:
            raise RuntimeError("Cannot construct reviewed SQLite rebuild")
        # Copy only columns that already exist. The target SQLite schema
        # populates eligible new fields from its DEFAULT or with NULL.
        old_columns = {
            row[1] for row in raw.execute(f"PRAGMA table_info({_quote(table, 'sqlite')})")
        }
        copied = [name for name in target.__columns__ if name in old_columns]
        names = ", ".join(_quote(name, "sqlite") for name in copied)
        # Explicit conversion for changed storage types. Unchanged columns
        # retain their original values. SQLite enforces target affinity.
        current_types = {
            row[1]: row[2].upper().replace(" ", "")
            for row in raw.execute(f"PRAGMA table_info({_quote(table, 'sqlite')})")
        }
        from .schema_planning import _sqlite_declared_type

        expressions = []
        for field_name in copied:
            field = target.__columns__[field_name]
            source = _quote(field_name, "sqlite")
            old = current_types[field_name]
            desired = _sqlite_declared_type(field).upper().replace(" ", "")
            if old == "INTEGER" and re.fullmatch(r"VARCHAR\(\d+\)", desired):
                source = f"CAST({source} AS TEXT)"
            elif re.fullmatch(r"VARCHAR\(\d+\)", old) and desired == "INTEGER":
                source = f"CAST({source} AS INTEGER)"
            expressions.append(source)
        before = raw.execute(f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')}").fetchone()[0]
        raw.execute(temporary_sql)
        raw.execute(
            f"INSERT INTO {_quote(temporary, 'sqlite')} ({names}) "
            f"SELECT {', '.join(expressions)} FROM {_quote(table, 'sqlite')}"
        )
        raw.execute(f"DROP TABLE {_quote(table, 'sqlite')}")
        raw.execute(
            f"ALTER TABLE {_quote(temporary, 'sqlite')} RENAME TO {_quote(table, 'sqlite')}"
        )
        for _, sql in indexes:
            raw.execute(sql)
        after = raw.execute(f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')}").fetchone()[0]
        if before != after:
            raise RuntimeError("Default change altered the number of application rows")
        matches_models_with_indexes(raw, models)
        warnings = [
            f"Approved permanent deletion of {t}.{col}: {count} existing non-NULL "
            f"value(s); backup recommended, not automatically created"
            for t, col, count, _ in reviewed_removals
            if count
        ]
        path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        if incoming_unique:
            raw.execute("PRAGMA foreign_keys=ON")
            if raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise RuntimeError("SQLite foreign keys could not be restored")

    return path


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_length_change(raw, root, db_file, models, title, proposal):
    """Change VARCHAR length; recheck stored values under lock when narrowing."""
    from .migration_execution import sqlite_migrate_default_change as migrate_default_change

    return migrate_default_change(raw, root, db_file, models, title, proposal, kind="type_mismatch")


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_table_transition(
    raw, root, db_file, models, title, proposal, *, reviewed_removals=(), acknowledged=()
):
    """Execute one reviewed table transition and record one forward snapshot."""
    from .migration_execution import sqlite_migrate_default_change as migrate_default_change

    return migrate_default_change(
        raw,
        root,
        db_file,
        models,
        title,
        proposal,
        kind="table_transition",
        reviewed_removals=reviewed_removals,
        acknowledged=acknowledged,
    )
