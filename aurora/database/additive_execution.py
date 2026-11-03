"""Guarded single-column additive DDL for AuroraSQL.

All three dialects use a separate journal. This is not a general migration
engine: unresolved outcomes always require manual investigation.
"""

from dataclasses import dataclass
import re
from uuid import UUID, uuid4

from .additive_approval import AddColumnReview, review_add_column, _column_sql
from .coordination import migration_lock
from .history import review_history
from .inspection import inspect_table
from .models import _identifier
from .schema import _quote


_JOURNAL = "aurorasql_additive_history_v1"
_CREATE_JOURNAL = "aurorasql_schema_history"


@dataclass(frozen=True)
class AdditiveEntry:
    operation_id: str
    migration_id: str
    table: str
    column: str
    fingerprint: str
    status: str


def _verify_additive_journal(connection):
    """Reject a missing or structurally altered audit journal before use."""
    observed = inspect_table(connection, _JOURNAL)
    required = {
        "operation_id", "migration_id", "table_name", "column_name",
        "fingerprint", "status",
    }
    columns = {item.name: item for item in observed.columns}

    if not observed.exists:
        raise RuntimeError("Additive journal missing; initialize explicitly")

    expected_types = {
        "operation_id": "varchar(36)",
        "migration_id": "varchar(80)",
        "table_name": "varchar(128)",
        "column_name": "varchar(128)",
        "fingerprint": "varchar(64)",
        "status": "varchar(16)",
    }

    if (set(columns) != required
            or not columns["operation_id"].primary_key
            or any(columns[name].primary_key for name in required - {"operation_id"})
            or any(columns[name].nullable for name in required)
            or any(columns[name].default_sql is not None for name in required)
            or any(_reflected_type_name(columns[name].sql_type, connection.dialect)
                   != _reflected_type_name(sql_type, connection.dialect)
                   for name, sql_type in expected_types.items())
            or not observed.uniqueness_verified
            or "migration_id" not in observed.unique_columns
            or set(observed.unique_columns) - {"operation_id", "migration_id"}
            or len(observed.indexes) != 2):
        raise RuntimeError("Additive journal structure mismatch; migration blocked")

    # Column reflection alone cannot reveal triggers or table-level CHECK/FK
    # clauses. Never trust an audit journal with executable side effects.
    if connection.dialect == "sqlite":
        ddl = connection.fetch_first(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
            {"name": _JOURNAL},
        )
        trigger = connection.fetch_first(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:name",
            {"name": _JOURNAL},
        )
        temporary_trigger = connection.fetch_first(
            "SELECT name FROM sqlite_temp_master WHERE type='trigger' AND tbl_name=:name",
            {"name": _JOURNAL},
        )
        foreign_keys = connection.fetch_all(f'PRAGMA foreign_key_list("{_JOURNAL}")')

        if (ddl is None or not isinstance(ddl["sql"], str)
                or trigger is not None or temporary_trigger is not None or foreign_keys):
            raise RuntimeError("Additive journal has unsupported schema features")

        if re.search(r"\b(CHECK|REFERENCES|GENERATED|COLLATE|ON\s+CONFLICT|WITHOUT\s+ROWID|STRICT)\b",
                     ddl["sql"], re.I):
            raise RuntimeError("Additive journal has unsupported schema features")


def initialize_additive_history(connection, *, confirm=False):
    """Explicitly create a separate journal; never alter legacy history."""

    if not confirm:
        raise PermissionError("Explicit confirmation required")

    with migration_lock(connection):
        if inspect_table(connection, _JOURNAL).exists:
            raise RuntimeError("Additive journal already exists")

        journal = _quote(_JOURNAL, connection.dialect)
        connection.execute_write(
            f"CREATE TABLE {journal} ("
            "operation_id VARCHAR(36) PRIMARY KEY, "
            "migration_id VARCHAR(80) NOT NULL UNIQUE, "
            "table_name VARCHAR(128) NOT NULL, "
            "column_name VARCHAR(128) NOT NULL, "
            "fingerprint VARCHAR(64) NOT NULL, "
            "status VARCHAR(16) NOT NULL)"
        )

        # Verify that the durable audit structure is actually observable.
        try:
            _verify_additive_journal(connection)
        except RuntimeError as exc:
            raise RuntimeError(
                "Additive journal creation could not be verified: structure mismatch"
            ) from exc


def list_additive_history(connection):
    """Read history without creating or repairing any tables."""

    _verify_additive_journal(connection)

    journal = _quote(_JOURNAL, connection.dialect)
    rows = connection.fetch_all(
        "SELECT operation_id, migration_id, table_name, column_name, "
        f"fingerprint, status FROM {journal} ORDER BY operation_id"
    )
    entries = tuple(AdditiveEntry(row["operation_id"], row["migration_id"],
                                  row["table_name"], row["column_name"],
                                  row["fingerprint"], row["status"]) for row in rows)

    # A manually modified journal row must not be treated as proof of success.
    # Preserve unresolved entries for inspection, even if their data is damaged.
    for entry in entries:
        if entry.status == "applied":
            try:
                valid_id = str(UUID(entry.operation_id)) == entry.operation_id
            except (ValueError, TypeError, AttributeError):
                valid_id = False

            fingerprint = entry.fingerprint
            valid_migration = (isinstance(entry.migration_id, str)
                               and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}",
                                                entry.migration_id) is not None)

            try:
                _identifier(entry.table)
                _identifier(entry.column)
                valid_columns = True
            except (TypeError, ValueError):
                valid_columns = False

            if (not valid_id or not valid_migration or not valid_columns
                    or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in fingerprint)):
                raise RuntimeError("Applied migration journal evidence is invalid")

    return entries


def _source_columns(before, table):
    _identifier(table)
    return {item["name"]: item for item in next(
        entry for entry in before["tables"] if entry["name"] == table
    )["columns"]}


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
        if connection.fetch_first(
            f"SELECT name FROM {catalog} WHERE type='trigger' AND tbl_name=:name",
            {"name": table},
        ) is not None:
            raise RuntimeError("Tables with triggers require separate verification")

    row = connection.fetch_first(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
        {"name": table},
    )

    if row is None or not isinstance(row["sql"], str):
        raise RuntimeError("Missing table DDL")

    if re.search(r'\b(CHECK|REFERENCES|GENERATED|COLLATE|ON\s+CONFLICT|WITHOUT\s+ROWID|STRICT)\b',
                 row["sql"], re.I):
        raise RuntimeError("Advanced schema constraints require separate verification")

    for name, field in declared.items():
        observed = actual[name]

        if field["unique"] or field["references"] is not None or field["server_default"] is not None:
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


def _reflected_type_name(sql_type, dialect):
    """Normalize only documented PostgreSQL spellings of supported SQL types."""
    name = sql_type.lower()

    if dialect == "postgresql":
        if name.startswith("varchar("):
            return name.replace("varchar(", "character varying(", 1)

        if name.startswith("decimal("):
            return name.replace("decimal(", "numeric(", 1)

    return name


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
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table", {"table": table})

        if len(constraints) != 1 or constraints[0]["kind"] != "PRIMARY KEY":
            raise RuntimeError("Unsupported MySQL table constraints")

        triggers = connection.fetch_first(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
            {"table": table})

        if triggers is not None:
            raise RuntimeError("Triggers require separate verification")

        metadata = connection.fetch_all(
            "SELECT COLUMN_NAME AS name, EXTRA AS extra, GENERATION_EXPRESSION AS generation "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": table})

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
            {"table": table})

        if (metadata is None or metadata["persistence"] != "p" or metadata["partitioned"]
                or metadata["inherited"] or metadata["rls"]
                or metadata["constraints"] != 1 or metadata["triggers"] != 0
                or metadata["parents"] != 0):
            raise RuntimeError("Unsupported PostgreSQL table features")

        identity = connection.fetch_all(
            "SELECT a.attname AS name, a.attidentity AS identity, a.attgenerated AS generated "
            "FROM pg_catalog.pg_attribute a WHERE a.attrelid=to_regclass(:table) "
            "AND a.attnum>0 AND NOT a.attisdropped", {"table": table})

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
        "mysql": {"BOOLEAN": "tinyint(1)", "INTEGER": "int", "BIGINT": "bigint",
                  "DOUBLE": "double", "DATE": "date", "DATETIME": "datetime",
                  "TEXT": "text"},
        "postgresql": {"BOOLEAN": "boolean", "INTEGER": "integer", "BIGINT": "bigint",
                       "DOUBLE PRECISION": "double precision", "DATE": "date",
                       "TIMESTAMP": "timestamp without time zone", "TEXT": "text"},
    }[dialect]

    for name, field in declared.items():
        observed = actual[name]

        if field["unique"] or field["references"] is not None or field["server_default"] is not None:
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

    if (not live.exists or not live.uniqueness_verified
            or live.constraints_verified != original.constraints_verified):
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

    if (observed.primary_key or not observed.nullable
            or observed.default_sql is not None or reflected_type != expected_type):
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
            {"oid": op_id, "mid": approval.migration_id, "table": approval.table,
             "column": approval.column, "fingerprint": approval.plan_fingerprint,
             "status": "started"},
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
            f"UPDATE {journal} SET status=:status "
            "WHERE operation_id=:oid AND status='started'",
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
