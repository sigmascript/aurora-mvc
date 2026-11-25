"""Read-only schema discovery for AuroraSQL's three supported dialects.

Information-schema inspection is limited to the current database/schema.
No database changes or implicit schema creation are performed here.
"""

from decimal import Decimal, InvalidOperation
from datetime import date, datetime


from dataclasses import dataclass
from hashlib import sha256

import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

from .migration_timeline import _TABLE, inspect_native_history
from .migration_journal import _require_journal, _validate_intent

from .models import _identifier
from .migration_review import snapshot_fingerprint
from .snapshots import _snapshot_tables


@dataclass(frozen=True)
class ColumnInfo:
    """Inspected database column metadata."""

    name: str
    sql_type: str
    nullable: bool
    primary_key: bool
    default_sql: str | None = None


@dataclass(frozen=True)
class TableInfo:
    """Inspected database table metadata."""

    name: str
    exists: bool
    columns: tuple[ColumnInfo, ...]
    unique_columns: tuple[str, ...] = ()
    indexes: tuple[str, ...] = ()
    constraints_verified: bool = False
    uniqueness_verified: bool = False
    foreign_keys: tuple[tuple[str, str, str], ...] = ()
    foreign_key_actions: tuple[tuple[str, str, str], ...] | None = None


def inspect_table(connection, table_name):
    """Inspect an existing table, or report its absence, without writing."""
    _identifier(table_name)
    dialect = connection.dialect

    if dialect == "sqlite":
        # table_list is not available on all supported SQLite versions.
        found = connection.fetch_first(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:name",
            {"name": table_name},
        )

        if found is None:
            return TableInfo(table_name, False, ())

        rows = connection.fetch_all(f'PRAGMA table_info("{table_name}")')
        index_rows = connection.fetch_all(f'PRAGMA index_list("{table_name}")')
        foreign_key_rows = connection.fetch_all(f'PRAGMA foreign_key_list("{table_name}")')
        unique_columns = set()
        index_names = []
        foreign_keys = []
        foreign_key_actions = []

        # Composite foreign keys need separate structural intent; never infer
        # equivalence from one member of a multi-column constraint.
        foreign_key_ids = {}

        for row in foreign_key_rows:
            foreign_key_ids.setdefault(row["id"], []).append(row)

        foreign_keys_verified = True

        for members in foreign_key_ids.values():
            if len(members) != 1 or members[0]["seq"] != 0:
                foreign_keys_verified = False
                continue

            member = members[0]
            foreign_keys.append((member["from"], member["table"], member["to"]))
            foreign_key_actions.append((member["from"], member["on_update"], member["on_delete"]))

        for index in index_rows:
            index_name = index["name"]
            _identifier(index_name)
            index_names.append(index_name)

            # A unique composite index does not imply each member is unique.
            if index["unique"] and not index["partial"]:
                members = connection.fetch_all(f'PRAGMA index_xinfo("{index_name}")')
                keys = [member for member in members if member["key"]]

                if len(keys) == 1 and keys[0]["name"] is not None:
                    unique_columns.add(keys[0]["name"])

        return TableInfo(
            table_name,
            True,
            tuple(
                ColumnInfo(
                    row["name"],
                    row["type"],
                    not bool(row["notnull"]) and not bool(row["pk"]),
                    bool(row["pk"]),
                    row["dflt_value"],
                )
                for row in rows
            ),
            tuple(sorted(unique_columns)),
            tuple(sorted(index_names)),
            foreign_keys_verified,
            True,
            tuple(sorted(foreign_keys)),
            tuple(sorted(foreign_key_actions)),
        )

    if dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT COLUMN_NAME AS name, COLUMN_TYPE AS sql_type, "
            "IS_NULLABLE AS nullable, COLUMN_KEY AS column_key, COLUMN_DEFAULT AS default_sql "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name "
            "ORDER BY ORDINAL_POSITION",
            {"name": table_name},
        )
        exists = (
            connection.fetch_first(
                "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name AND TABLE_TYPE = 'BASE TABLE'",
                {"name": table_name},
            )
            is not None
        )

        if not exists:
            return TableInfo(table_name, False, ())
        # A composite unique index does not make its individual columns unique.
        index_rows = connection.fetch_all(
            "SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name, "
            "NON_UNIQUE AS non_unique, SEQ_IN_INDEX AS position "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            {"name": table_name},
        )
        groups = {}

        for index in index_rows:
            groups.setdefault(index["index_name"], []).append(index)

        unique_columns = set()

        for name, members in groups.items():
            if (
                name != "PRIMARY"
                and len(members) == 1
                and not bool(members[0]["non_unique"])
                and members[0]["column_name"] is not None
            ):
                unique_columns.add(members[0]["column_name"])

        fk_rows = connection.fetch_all(
            "SELECT k.CONSTRAINT_NAME AS constraint_name, "
            "k.COLUMN_NAME AS source_column, k.REFERENCED_TABLE_NAME AS target_table, "
            "k.REFERENCED_COLUMN_NAME AS target_column, "
            "k.REFERENCED_TABLE_SCHEMA AS target_schema, "
            "k.TABLE_SCHEMA AS source_schema, "
            "r.UPDATE_RULE AS update_rule, r.DELETE_RULE AS delete_rule, "
            "(SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE members "
            "WHERE members.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
            "AND members.TABLE_NAME = k.TABLE_NAME "
            "AND members.CONSTRAINT_NAME = k.CONSTRAINT_NAME) AS column_count "
            "FROM information_schema.KEY_COLUMN_USAGE k "
            "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
            "ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
            "AND r.TABLE_NAME = k.TABLE_NAME "
            "AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
            "WHERE k.TABLE_SCHEMA = DATABASE() AND k.TABLE_NAME = :name "
            "AND k.REFERENCED_TABLE_NAME IS NOT NULL",
            {"name": table_name},
        )
        allowed_actions = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
        fk_verified = all(
            int(row["column_count"]) == 1
            and row["target_schema"] == row["source_schema"]
            and all(
                row[key] is not None
                for key in (
                    "source_column",
                    "target_table",
                    "target_column",
                    "update_rule",
                    "delete_rule",
                )
            )
            and row["update_rule"].upper() in allowed_actions
            and row["delete_rule"].upper() in allowed_actions
            for row in fk_rows
        )
        fk_targets = (
            tuple(
                sorted(
                    (row["source_column"], row["target_table"], row["target_column"])
                    for row in fk_rows
                )
            )
            if fk_verified
            else ()
        )
        fk_actions = (
            tuple(
                sorted(
                    (row["source_column"], row["update_rule"].upper(), row["delete_rule"].upper())
                    for row in fk_rows
                )
            )
            if fk_verified
            else None
        )
        return TableInfo(
            table_name,
            True,
            tuple(
                ColumnInfo(
                    row["name"],
                    row["sql_type"],
                    row["nullable"] == "YES",
                    row["column_key"] == "PRI",
                    row["default_sql"],
                )
                for row in rows
            ),
            tuple(sorted(unique_columns)),
            tuple(sorted(groups)),
            fk_verified,
            True,
            fk_targets,
            fk_actions,
        )

    if dialect == "postgresql":
        # Resolve via the connection's current search_path, without concatenating
        # user-controlled table names into SQL identifiers.
        exists = (
            connection.fetch_first(
                "SELECT c.oid AS oid FROM pg_catalog.pg_class c "
                "WHERE c.oid = to_regclass(:name) AND c.relkind IN ('r','p')",
                {"name": table_name},
            )
            is not None
        )

        if not exists:
            return TableInfo(table_name, False, ())

        rows = connection.fetch_all(
            "SELECT a.attname AS name, pg_catalog.format_type(a.atttypid, a.atttypmod) AS sql_type, "
            "NOT a.attnotnull AS nullable, "
            "pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_sql, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_index i "
            "WHERE i.indrelid = a.attrelid AND i.indisprimary AND a.attnum = ANY(i.indkey)) AS primary_key "
            "FROM pg_catalog.pg_attribute a "
            "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = to_regclass(:name) AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            {"name": table_name},
        )

        # Inventory all indexes separately from the strict single-column
        # uniqueness classification. Constraint-backed and composite indexes
        # must remain visible for conservative schema review.
        index_rows = connection.fetch_all(
            "SELECT ci.relname AS index_name, i.indisunique AS is_unique, "
            "i.indisprimary AS is_primary, i.indisvalid AS is_valid, "
            "i.indpred IS NOT NULL AS is_partial, "
            "i.indexprs IS NOT NULL AS is_expression, "
            "i.indnkeyatts AS key_count, a.attname AS column_name "
            "FROM pg_catalog.pg_index i "
            "JOIN pg_catalog.pg_class ci ON ci.oid = i.indexrelid "
            "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = i.indkey[0] "
            "WHERE i.indrelid = to_regclass(:name)",
            {"name": table_name},
        )
        unique_columns = tuple(
            sorted(
                {
                    row["column_name"]
                    for row in index_rows
                    if row["is_unique"]
                    and not row["is_primary"]
                    and row["is_valid"]
                    and not row["is_partial"]
                    and not row["is_expression"]
                    and row["key_count"] == 1
                    and row["column_name"] is not None
                }
            )
        )
        fk_rows = connection.fetch_all(
            "SELECT c.conname AS constraint_name, "
            "array_length(c.conkey, 1) AS column_count, "
            "src.attname AS source_column, target.relname AS target_table, "
            "source_ns.oid = target.relnamespace AS same_schema, "
            "dest.attname AS target_column, c.confupdtype AS update_code, "
            "c.confdeltype AS delete_code, c.convalidated AS validated, "
            "c.condeferrable AS deferrable, c.confmatchtype AS match_type "
            "FROM pg_catalog.pg_constraint c "
            "JOIN pg_catalog.pg_class target ON target.oid = c.confrelid "
            "JOIN pg_catalog.pg_class source ON source.oid = c.conrelid "
            "JOIN pg_catalog.pg_namespace source_ns ON source_ns.oid = source.relnamespace "
            "LEFT JOIN pg_catalog.pg_attribute src "
            "ON src.attrelid = c.conrelid AND src.attnum = c.conkey[1] "
            "LEFT JOIN pg_catalog.pg_attribute dest "
            "ON dest.attrelid = c.confrelid AND dest.attnum = c.confkey[1] "
            "WHERE c.conrelid = to_regclass(:name) AND c.contype = 'f'",
            {"name": table_name},
        )
        actions = {
            "a": "NO ACTION",
            "r": "RESTRICT",
            "c": "CASCADE",
            "n": "SET NULL",
            "d": "SET DEFAULT",
        }
        fk_verified = all(
            int(row["column_count"]) == 1
            and row["same_schema"] is True
            and row["validated"] is True
            and row["deferrable"] is False
            and row["match_type"] == "s"
            and all(
                row[key] is not None for key in ("source_column", "target_table", "target_column")
            )
            and row["update_code"] in actions
            and row["delete_code"] in actions
            for row in fk_rows
        )
        fk_targets = (
            tuple(
                sorted(
                    (row["source_column"], row["target_table"], row["target_column"])
                    for row in fk_rows
                )
            )
            if fk_verified
            else ()
        )
        fk_actions = (
            tuple(
                sorted(
                    (row["source_column"], actions[row["update_code"]], actions[row["delete_code"]])
                    for row in fk_rows
                )
            )
            if fk_verified
            else None
        )
        return TableInfo(
            table_name,
            True,
            tuple(
                ColumnInfo(
                    row["name"],
                    row["sql_type"],
                    bool(row["nullable"]),
                    bool(row["primary_key"]),
                    row["default_sql"],
                )
                for row in rows
            ),
            unique_columns,
            tuple(sorted({row["index_name"] for row in index_rows})),
            fk_verified,
            True,
            fk_targets,
            fk_actions,
        )

    raise ValueError("Unsupported database dialect")


@dataclass(frozen=True)
class LiveSchemaEvidence:
    dialect: str
    reviewed_before_sha256: str
    checked_tables: tuple[str, ...]
    findings: tuple[str, ...]
    execution_allowed: bool = False

    @property
    def structurally_consistent(self):
        return not self.findings


def inspect_reviewed_baseline(connection, before):
    """Check actual table/column presence against a reviewed model snapshot.

    This is structural evidence, NOT a complete schema equality proof:
    indexes, constraints, defaults, types and unexpected tables require a
    separate trustworthy dialect-specific canonicalization before execution.
    """
    tables = _snapshot_tables(before)

    if before["dialect"] != connection.dialect:
        raise ValueError("Snapshot dialect does not match the live connection")

    digest = snapshot_fingerprint(before)
    findings = []

    for name, columns in sorted(tables.items()):
        actual = inspect_table(connection, name)

        if not actual.exists:
            findings.append(f"Missing table: {name}")
            continue

        actual_columns = {column.name for column in actual.columns}
        expected_columns = set(columns)

        for missing in sorted(expected_columns - actual_columns):
            findings.append(f"Missing column: {name}.{missing}")

        for extra in sorted(actual_columns - expected_columns):
            findings.append(f"Unexpected column: {name}.{extra}")

    return LiveSchemaEvidence(connection.dialect, digest, tuple(sorted(tables)), tuple(findings))


@dataclass(frozen=True)
class StrictSchemaAssessment:
    dialect: str
    checked_tables: tuple[str, ...]
    findings: tuple[str, ...]
    complete_schema_verified: bool = False
    execution_allowed: bool = False


def assess_strict_schema(connection, before):
    """Inspect declared tables and refuse to certify incomplete catalog evidence.

    The current inspector does not enumerate all tables or fully expose foreign
    keys, check constraints, expression indexes, and dialect-specific defaults.
    Even an apparently matching catalog therefore cannot authorize migration.
    """
    tables = _snapshot_tables(before)

    if before["dialect"] != connection.dialect:
        raise ValueError("Snapshot dialect does not match connection")

    findings = []

    for name, expected in sorted(tables.items()):
        actual = inspect_table(connection, name)

        if not actual.exists:
            findings.append(f"Missing table: {name}")
            continue

        actual_columns = {column.name: column for column in actual.columns}

        for missing in sorted(expected.keys() - actual_columns.keys()):
            findings.append(f"Missing column: {name}.{missing}")

        for extra in sorted(actual_columns.keys() - expected.keys()):
            findings.append(f"Unexpected column: {name}.{extra}")

        for column_name in sorted(expected.keys() & actual_columns.keys()):
            field = expected[column_name]
            column = actual_columns[column_name]

            if bool(field["primary_key"]) != bool(column.primary_key):
                findings.append(f"Primary key mismatch: {name}.{column_name}")

            if bool(field["nullable"]) != bool(column.nullable) and not field["primary_key"]:
                findings.append(f"Nullability mismatch: {name}.{column_name}")

        if not actual.constraints_verified:
            findings.append(f"Constraint metadata incomplete: {name}")

        if not actual.uniqueness_verified:
            findings.append(f"Uniqueness metadata incomplete: {name}")

    findings.append(
        "Full catalog equivalence unavailable: table inventory, types, defaults, foreign keys and index definitions are not completely canonicalized"
    )
    return StrictSchemaAssessment(connection.dialect, tuple(sorted(tables)), tuple(findings))


# Read-only inventory of explicitly configured disposable server acceptance targets.


def inspect_server_acceptance_target(config_path, alias):
    """Fail closed unless both configured and actual identities are test-only."""
    from .cli_check import _load_config
    from .connections import _mysql_database_identity, _postgres_database_identity
    from .migration_history import list_history
    from .migration_timeline import inspect_server_timeline

    config, _ = _load_config(config_path)
    parsed = urlsplit(config.resolve(alias))
    expected_database = "aurorasql_test"
    expected_user = "aurorasql_test"

    if (
        parsed.scheme not in ("mysql+pymysql", "postgresql+psycopg")
        or unquote(parsed.path.lstrip("/")) != expected_database
        or unquote(parsed.username or "") != expected_user
    ):
        raise PermissionError("Live acceptance requires the dedicated test database and user")

    with config.connect(alias) as connection:
        if connection.dialect == "mysql":
            identity = _mysql_database_identity(connection, parsed)
            auth = connection.fetch_first("SELECT CURRENT_USER() AS user_name")

            if (
                not auth
                or not isinstance(auth.get("user_name"), str)
                or auth["user_name"].split("@", 1)[0] != expected_user
            ):
                raise PermissionError("MySQL authenticated account is not the test user")

            rows = connection.fetch_all(
                "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME"
            )
            owner_confirmed = False
            ownership_note = (
                "MySQL does not provide PostgreSQL-style database ownership; "
                "review test account grants separately before any DDL"
            )
        else:
            identity = _postgres_database_identity(connection, parsed)
            auth = connection.fetch_first(
                "SELECT current_user AS user_name, "
                "pg_catalog.pg_get_userbyid(d.datdba) AS owner_name "
                "FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
            )

            if (
                not auth
                or auth.get("user_name") != expected_user
                or auth.get("owner_name") != expected_user
            ):
                raise PermissionError("PostgreSQL test user must own the selected test database")

            schema = connection.fetch_first(
                "SELECT pg_catalog.pg_get_userbyid(n.nspowner) AS owner_name, "
                "has_schema_privilege(current_user, current_schema(), 'USAGE') AS can_use, "
                "has_schema_privilege(current_user, current_schema(), 'CREATE') AS can_create "
                "FROM pg_catalog.pg_namespace n WHERE n.nspname=current_schema()"
            )

            if not schema or (
                schema.get("owner_name") != expected_user
                and (schema.get("can_use") is not True or schema.get("can_create") is not True)
            ):
                raise PermissionError("PostgreSQL test user lacks verified active-schema access")

            rows = connection.fetch_all(
                "SELECT c.relname AS name, c.relkind AS kind, "
                "pg_catalog.pg_get_userbyid(c.relowner) AS owner_name "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
                "AND c.relkind IN ('r','p','v','m','f','S') ORDER BY c.relname"
            )

            if any(row.get("owner_name") != expected_user for row in rows):
                raise PermissionError(
                    "PostgreSQL test schema contains objects owned by another user"
                )
            owner_confirmed = True
            ownership_note = (
                "PostgreSQL test user owns the database and all inventoried objects; "
                "active schema is owned or has verified USAGE and CREATE grants"
            )

        tables = [{"name": row["name"], "kind": row["kind"]} for row in rows]
        names = [table["name"] for table in tables]

        if len(names) != len(set(names)) or any(not isinstance(name, str) for name in names):
            raise RuntimeError("Test database object inventory is inconsistent")

        if "aurorasql_migration_timeline" in names:
            timeline = inspect_server_timeline(connection, alias)
            history = [
                {"number": entry.number, "status": entry.status, "name": entry.name}
                for entry in timeline
            ]
        else:
            history = None

        if "aurorasql_schema_history" in names:
            legacy = [
                {"table": entry.table, "status": entry.status, "dialect": entry.dialect}
                for entry in list_history(connection)
            ]
        else:
            legacy = None

    return {
        "alias": alias,
        "backend": connection.dialect,
        "database_identity": identity,
        "authenticated_test_user": expected_user,
        "ownership_confirmed": owner_confirmed,
        "ownership_note": ownership_note,
        "existing_objects": tables,
        "native_history": history,
        "legacy_history": legacy,
        "requires_manual_review": bool(tables) or not owner_confirmed,
        "write_authorized": False,
    }


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _catalog(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def _native_history(connection, alias):
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aurorasql_migration_timeline'"
        ).fetchone()
        is None
    ):
        return None

    from .connections import Connection

    native = Connection(connection, "sqlite")
    timeline, _, _ = inspect_native_history(native, alias)
    return timeline


def _inspect_sqlite(connection, operation, alias="default", *, trusted_rename=False):
    """Inspect a target and refuse dependencies that this executor cannot prove."""
    catalog = _catalog(connection)
    tables = {name.casefold(): name for kind, name, _, _ in catalog if kind == "table"}
    requested = operation["table"].casefold()

    if requested not in tables:
        raise ValueError("Target table does not exist")

    table = tables[requested]
    details = connection.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
    columns = {row[1].casefold(): row for row in details}
    warnings = []

    if operation["kind"] == "rename_table":
        existing_names = {name.casefold() for _, name, _, _ in catalog}

        if operation["new_table"].casefold() in existing_names:
            raise ValueError("Destination schema name already exists")
    elif operation["kind"] == "create_index":
        index_names = {name.casefold() for _, name, _, _ in catalog}

        if operation["name"].casefold() in index_names:
            raise ValueError("Index name already exists")

        for column in operation["columns"]:
            if column.casefold() not in columns:
                raise ValueError("Indexed column does not exist")

            if columns[column.casefold()][6]:
                warnings.append("Hidden or generated columns need dedicated index review")
    elif operation["kind"] == "drop_index":
        # An index name alone is not proof of ownership: SQLite index names
        # are global, and autoindexes belong to table constraints.
        matches = [
            (parent, sql)
            for kind, name, parent, sql in catalog
            if kind == "index" and name.casefold() == operation["name"].casefold()
        ]
        if len(matches) != 1 or matches[0][0].casefold() != requested:
            raise ValueError("Index does not exist on the selected table")
        if not isinstance(matches[0][1], str):
            raise ValueError("Implicit or constraint-owned indexes cannot be dropped")
    elif operation["kind"] == "rename_columns":
        for old, new in operation["columns"].items():
            if old.casefold() not in columns:
                raise ValueError("Source column does not exist")

            if new.casefold() in columns:
                raise ValueError("Destination column already exists")

            if columns[old.casefold()][5] or columns[old.casefold()][6]:
                warnings.append("Primary-key or hidden-column rename needs dedicated review")

    for kind, name, _, _ in catalog:
        if kind in ("trigger", "view"):
            warnings.append("Triggers and views require separate dependency verification")
            break

    for name in tables.values():
        keys = connection.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall()

        if any((row[2].casefold() == requested or name.casefold() == requested) for row in keys):
            warnings.append("Foreign-key dependencies require separate verification")
            break

    for kind, _, parent, _ in catalog:
        if (
            operation["kind"] not in ("create_index", "drop_index")
            and kind == "index"
            and parent.casefold() == requested
        ):
            warnings.append("Indexes on the target require separate verification")
            break

    # Only ordinary tables with transparent SQL DDL are supported. Virtual,
    # generated and advanced constraints need a separate backend-specific path.
    ddl = next((sql for kind, name, _, sql in catalog if kind == "table" and name == table), None)

    if not isinstance(ddl, str) or not re.match(r"CREATE\s+TABLE\b", ddl, re.I):
        warnings.append("Nonordinary table DDL is unsupported")

    if ddl and re.search(r"\b(CHECK|GENERATED|COLLATE|WITHOUT\s+ROWID|STRICT)\b", ddl, re.I):
        warnings.append("Advanced table constraints require separate verification")

    # Explicit operations currently have a legacy v2 journal executor. Never
    # mutate a database with native linear history without extending that chain
    # in the same transaction; this requires model-set reconciliation first.
    if "aurorasql_migration_timeline" in tables:
        if operation["kind"] == "rename_table" and not trusted_rename:
            warnings.append("Native migration timeline needs integrated table-rename identity")
        else:
            try:
                native = _native_history(connection, alias)

                if not native or not native[-1]["physical_history_consistent"]:
                    warnings.append("Native migration history is not verified")
            except (RuntimeError, ValueError):
                warnings.append("Native migration history is not verified")

    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite database integrity check failed")

    for journal in ("aurorasql_schema_history", "aurorasql_additive_history_v1"):
        if journal.casefold() not in tables:
            continue

        recorded = connection.execute(
            f"SELECT COUNT(*) FROM {_quote(journal)} WHERE table_name=?", (table,)
        ).fetchone()[0]

        if recorded and operation["kind"] == "rename_table" and not trusted_rename:
            warnings.append("Historical table journal requires rename-aware reconciliation")

        unresolved = connection.execute(
            f"SELECT COUNT(*) FROM {_quote(journal)} WHERE status<>?", ("applied",)
        ).fetchone()[0]

        if unresolved:
            warnings.append("Unresolved legacy or additive migration history")

    try:
        _require_journal(connection)
        entries = connection.execute(
            f'SELECT migration_id,review_sha256,before_sha256,after_sha256,state FROM "{_TABLE}"'
        ).fetchall()

        for entry in entries:
            _validate_intent(*entry[:4])

            if entry[4] != "applied":
                warnings.append("Unresolved migration intent blocks execution")
    except RuntimeError:
        warnings.append("Missing or incompatible migration intent journal")

    return catalog, sorted(set(warnings))


def _catalog_digest(catalog):
    serialized = json.dumps(
        catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reflected_type_name(sql_type, dialect):
    """Normalize only documented PostgreSQL spellings of supported SQL types."""
    name = sql_type.lower()

    if dialect == "postgresql":
        if name.startswith("varchar("):
            return name.replace("varchar(", "character varying(", 1)

        if name.startswith("decimal("):
            return name.replace("decimal(", "numeric(", 1)

    return name


def _objects(connection):
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE()"
        )
        if any(item["kind"] != "BASE TABLE" for item in rows):
            raise RuntimeError("Unrecognized MySQL schema objects; migration refused")
    elif connection.dialect == "postgresql":
        rows = connection.fetch_all(
            "SELECT c.relname AS name, c.relkind AS kind FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f')"
        )
        if any(item["kind"] != "r" for item in rows):
            raise RuntimeError("Unrecognized PostgreSQL schema objects; migration refused")
    else:
        raise ValueError("Server history requires MySQL or PostgreSQL")
    return sorted(row["name"] for row in rows)


def _physical(connection):
    """Independently observe current tables, columns, keys and indexes."""
    result = []
    for name in _objects(connection):
        if name in ("__migrations__", "migrations", "__backups__"):
            if name == "__backups__":
                from .backup_catalog import _catalog_exists

                if not _catalog_exists(connection):
                    raise RuntimeError("Unverified internal backup catalog")
            continue
        table = inspect_table(connection, name)
        if not table.exists or not table.uniqueness_verified:
            raise RuntimeError("Cannot verify server table metadata: " + name)
        result.append(
            {
                "table": name,
                "columns": [
                    [col.name, col.sql_type, col.nullable, col.primary_key, col.default_sql]
                    for col in table.columns
                ],
                "indexes": sorted(table.indexes),
                "unique_columns": sorted(table.unique_columns),
                "foreign_keys": [list(fk) for fk in table.foreign_keys],
            }
        )
    return result


def catalog_fingerprint(dialect, observations):
    """Reproduce the server catalog digest without changing its ordering."""
    payload = json.dumps(
        {"dialect": dialect, "observed": observations},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


# Native declared-schema verification (read-only).

from .schema import validate_init_models, _default_literal, _resolved_foreign_keys
from .schema_planning import _declared_type, _normalized_declaration

_JOURNALS = frozenset({"aurorasql_schema_history", "aurorasql_migration_timeline"})


# PostgreSQL pg_get_expr may spell a constant with a known type cast. Reject
# expressions, arithmetic, nested casts, and casts to unrelated types.
_PG_QUOTED = re.compile(r"'((?:[^']|'')*)'(?:\:\:([a-z ]+))?\Z", re.IGNORECASE)
_PG_NUMBER = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\:\:([a-z ]+))?\Z", re.IGNORECASE)
_PG_BOOLEAN = re.compile(r"(true|false)(?:\:\:([a-z ]+))?\Z", re.IGNORECASE)


def postgres_literal_value(field, observed):
    """Return a validated PostgreSQL constant's literal text or None.

    The result is not a SQL fragment and is never used to generate DDL.
    This parser only establishes catalog evidence for a known model type.
    """
    if not isinstance(observed, str):
        return None
    kind = field.python_type
    if kind in (str, date, datetime):
        match = _PG_QUOTED.fullmatch(observed)
        casts = {
            str: ("text", "character varying"),
            date: ("date",),
            datetime: ("timestamp", "timestamp without time zone"),
        }
        if match is None or (match[2] is not None and match[2].lower() not in casts[kind]):
            return None
        return match[1].replace("''", "'")
    if kind is bool:
        match = _PG_BOOLEAN.fullmatch(observed)
        if match is None or (match[2] is not None and match[2].lower() != "boolean"):
            return None
        return match[1].lower()
    if kind in (int, float) or kind is Decimal:
        match = _PG_NUMBER.fullmatch(observed)
        casts = {
            int: ("bigint", "integer", "smallint"),
            float: ("double precision", "real"),
            Decimal: ("numeric", "decimal"),
        }
        if match is None or (match[2] is not None and match[2].lower() not in casts[kind]):
            return None
        if kind is int and ("." in match[1] or Decimal(match[1]) != int(Decimal(match[1]))):
            return None
        return match[1]
    return None


def server_default_matches(field, observed, dialect):
    """Recognize bounded literal defaults, never arbitrary SQL expressions.

    PostgreSQL returns SQL expressions from pg_get_expr; accept only exact
    literal spellings with known casts. MySQL returns normalized values and
    requires separate EXTRA/DEFAULT_GENERATED catalog checks by its caller.
    """
    expected = _default_literal(field, dialect)

    if expected is None:
        return observed is None

    if not isinstance(observed, str) or dialect not in ("mysql", "postgresql"):
        return False

    value = field.default
    if dialect == "postgresql":
        observed = postgres_literal_value(field, observed)
        if observed is None:
            return False

    if type(value) is bool:
        candidates = {"1", "true"} if value else {"0", "false"}
        return observed.lower() in candidates

    if type(value) is int or isinstance(value, Decimal):
        try:
            return Decimal(observed) == Decimal(str(value))
        except InvalidOperation:
            return False

    if type(value) is float:
        try:
            return Decimal(observed) == Decimal(str(value))
        except InvalidOperation:
            return False

    if isinstance(value, (date, datetime)):
        literal = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    elif isinstance(value, str):
        literal = value
    else:
        return False

    if dialect == "mysql":
        return observed == literal

    return observed == literal


def _verify_declared_table(connection, model, models=None):
    """Require ordinary native table metadata equivalent to a model declaration."""
    dialect = connection.dialect
    name = model.__table_name__
    table = inspect_table(connection, name)
    actual = {column.name: column for column in table.columns}
    declared = model.__columns__
    unique = {key for key, field in declared.items() if field.unique and not field.primary_key}
    keys = [key for key, field in declared.items() if field.primary_key]
    expected_fks = _resolved_foreign_keys(model, models) if models is not None else {}
    expected_targets = sorted(
        (source, target, col) for source, (target, col, _, _) in expected_fks.items()
    )
    expected_actions = sorted(
        (source, update, delete) for source, (_, _, update, delete) in expected_fks.items()
    )
    if models is not None:
        if (
            not table.constraints_verified
            or table.foreign_key_actions is None
            or list(table.foreign_keys) != expected_targets
        ):
            raise RuntimeError("Initial table foreign-key target differs from its model")
        actual_actions = list(table.foreign_key_actions)
        if dialect == "mysql":
            # MySQL treats NO ACTION as RESTRICT for non-deferrable constraints.
            def normalize(action):
                return "RESTRICT" if action == "NO ACTION" else action

            actual_actions = [
                (source, normalize(up), normalize(down)) for source, up, down in actual_actions
            ]
            expected_actions = [
                (source, normalize(up), normalize(down)) for source, up, down in expected_actions
            ]
        if actual_actions != expected_actions:
            raise RuntimeError("Initial table foreign-key actions differ from its model")

    expected_index_count = 1 + len(unique)
    if dialect == "mysql" and expected_fks:
        # MySQL may create a supporting non-unique index for each FK. Verify
        # its exact single source column, never ignore arbitrary extra indexes.
        rows = connection.fetch_all(
            "SELECT INDEX_NAME AS name, COLUMN_NAME AS col, NON_UNIQUE AS non_unique, "
            "SEQ_IN_INDEX AS position FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            {"table": name},
        )
        groups = {}
        for row in rows:
            groups.setdefault(row["name"], []).append(row)
        extras = [
            members
            for index, members in groups.items()
            if index != "PRIMARY" and members[0]["col"] not in unique
        ]
        if any(
            len(members) != 1
            or members[0]["position"] != 1
            or not members[0]["non_unique"]
            or members[0]["col"] not in expected_fks
            for members in extras
        ):
            raise RuntimeError("MySQL initial table has unexpected supporting indexes")
        expected_index_count += len(extras)
        if set(table.indexes) != set(groups):
            raise RuntimeError("MySQL initial table index evidence differs")

    if (
        not table.exists
        or not table.uniqueness_verified
        or set(actual) != set(declared)
        or len(keys) != 1
        or declared[keys[0]].python_type is not int
        or set(table.unique_columns) != unique
        or len(table.indexes) != expected_index_count
        or any(field.server_default is not None for field in declared.values())
        or (models is None and any(field.references is not None for field in declared.values()))
    ):
        raise RuntimeError("Initial table has unsupported structure or constraint drift")

    if dialect == "mysql":
        constraints = connection.fetch_all(
            "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": name},
        )
        kinds = sorted(item["kind"] for item in constraints)

        if kinds != sorted(
            ["PRIMARY KEY"] + ["UNIQUE"] * len(unique) + ["FOREIGN KEY"] * len(expected_fks)
        ):
            raise RuntimeError("MySQL table has unsupported constraints")

        if (
            connection.fetch_first(
                "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
                {"table": name},
            )
            is not None
        ):
            raise RuntimeError("MySQL table has unverified triggers")

        metadata = connection.fetch_all(
            "SELECT COLUMN_NAME AS name, EXTRA AS extra, GENERATION_EXPRESSION AS generation "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": name},
        )

        if {item["name"] for item in metadata} != set(actual) or len(metadata) != len(actual):
            raise RuntimeError("MySQL column catalog is incomplete")

        for item in metadata:
            extra = (item["extra"] or "").lower()

            if item["name"] == keys[0]:
                if extra != "auto_increment":
                    raise RuntimeError("MySQL primary key identity differs")
            elif extra or item["generation"]:
                raise RuntimeError("MySQL generated or unusual column requires review")
    else:
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT count(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal) AS triggers, "
            "(SELECT count(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {"table": name},
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
            raise RuntimeError("PostgreSQL table has unsupported catalog features")

        constraints = connection.fetch_all(
            "SELECT contype AS kind FROM pg_catalog.pg_constraint "
            "WHERE conrelid=to_regclass(:table)",
            {"table": name},
        )

        if sorted(item["kind"] for item in constraints) != sorted(
            ["p"] + ["u"] * len(unique) + ["f"] * len(expected_fks)
        ):
            raise RuntimeError("PostgreSQL table has unsupported constraints")

        identities = connection.fetch_all(
            "SELECT attname AS name, attidentity AS identity, attgenerated AS generated "
            "FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass(:table) "
            "AND attnum>0 AND NOT attisdropped",
            {"table": name},
        )

        if {item["name"] for item in identities} != set(actual) or len(identities) != len(actual):
            raise RuntimeError("PostgreSQL column catalog is incomplete")

        for item in identities:
            if item["name"] == keys[0]:
                if item["identity"] != "d" or item["generated"]:
                    raise RuntimeError("PostgreSQL primary key identity differs")
            elif item["identity"] or item["generated"]:
                raise RuntimeError("PostgreSQL generated column requires review")

    for key, field in declared.items():
        observed = actual[key]
        expected_type = _normalized_declaration(_declared_type(field, dialect), dialect)
        observed_type = _normalized_declaration(observed.sql_type, dialect)

        if (
            observed.primary_key != field.primary_key
            or observed.nullable != (field.nullable and not field.primary_key)
            or expected_type != observed_type
            or not server_default_matches(field, observed.default_sql, dialect)
        ):
            raise RuntimeError("Initial column differs from its validated model")

    return table


def verify_server_declared_schema(
    connection,
    models,
    *,
    evidence_models=None,
    historical_exclusion=None,
    historical_type_override=None,
    historical_nullability_override=None,
    historical_default_exclusion=None,
    include_observations=False,
    history_tables=None,
    allow_extended=False,
):
    """Verify all declared server tables, optionally fingerprinting a subset.

    Require no unexpected tables or views, no extra indexes/constraints, and
    the narrow per-table catalog assurances already used by additive safety.
    Subset evidence lets a successor independently check that an earlier
    Initial schema remains unchanged while also checking newly added tables.
    A subset never weakens full physical catalog verification.
    """
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Initial server verification requires MySQL or PostgreSQL")

    ordered = validate_init_models(models, dialect=dialect, allow_extended=allow_extended)
    evidence = (
        ordered
        if evidence_models is None
        else validate_init_models(evidence_models, dialect=dialect, allow_extended=allow_extended)
    )

    if not evidence or any(model not in ordered for model in evidence):
        raise ValueError("Schema evidence models must be a nonempty subset of verified models")

    if historical_exclusion is not None and (
        not isinstance(historical_exclusion, tuple)
        or len(historical_exclusion) != 2
        or historical_exclusion[0] not in {model.__table_name__ for model in evidence}
    ):
        raise ValueError("Historical column exclusion must belong to the verified schema")

    overrides = sum(
        option is not None
        for option in (
            historical_exclusion,
            historical_type_override,
            historical_nullability_override,
            historical_default_exclusion,
        )
    )

    if overrides > 1:
        raise ValueError("Only one historical schema transformation can be verified")

    if include_observations and (overrides or evidence_models is not None):
        raise ValueError("Complete current catalog evidence is required for observations")

    if historical_type_override is not None:
        if (
            not isinstance(historical_type_override, tuple)
            or len(historical_type_override) != 3
            or historical_type_override[0] not in {model.__table_name__ for model in evidence}
            or not all(isinstance(value, str) for value in historical_type_override)
        ):
            raise ValueError("Historical type evidence must identify a verified column")

    if historical_nullability_override is not None:
        if (
            not isinstance(historical_nullability_override, tuple)
            or len(historical_nullability_override) != 3
            or historical_nullability_override[0]
            not in {model.__table_name__ for model in evidence}
            or not isinstance(historical_nullability_override[1], str)
            or type(historical_nullability_override[2]) is not bool
        ):
            raise ValueError("Historical nullability evidence must identify a verified column")

    if historical_default_exclusion is not None:
        if (
            not isinstance(historical_default_exclusion, tuple)
            or len(historical_default_exclusion) != 2
            or historical_default_exclusion[0] not in {model.__table_name__ for model in evidence}
            or not isinstance(historical_default_exclusion[1], str)
        ):
            raise ValueError("Historical default evidence must identify a verified column")

    expected = {model.__table_name__ for model in ordered} | (
        _JOURNALS if history_tables is None else set(history_tables)
    )

    if dialect == "mysql":
        objects = connection.fetch_all(
            "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE()"
        )
        names = {row["name"] for row in objects}
        valid_kinds = all(row["kind"] == "BASE TABLE" for row in objects)
    else:
        objects = connection.fetch_all(
            "SELECT c.relname AS name, c.relkind AS kind "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
            "AND c.relkind IN ('r','p','v','m','f')"
        )
        names = {row["name"] for row in objects}
        valid_kinds = all(row["kind"] == "r" for row in objects)

    # Backup catalogs are internal evidence, not application model tables.
    # Recognize them only after validating their exact known definition; never
    # exempt an arbitrary table merely because it uses the reserved name.
    if "__backups__" in names:
        from .backup_catalog import _catalog_format

        if _catalog_format(connection) is None:
            raise RuntimeError("Server backup catalog disappeared during verification")
        expected.add("__backups__")

    if not valid_kinds or names != expected or len(objects) != len(expected):
        raise RuntimeError("Server Initial contains unexpected or missing schema objects")

    observations = []
    historical_observations = []
    evidence_tables = {model.__table_name__ for model in evidence}

    for model in ordered:
        table = _verify_declared_table(connection, model, ordered if allow_extended else None)

        if table.name in evidence_tables:
            observed = {
                "table": table.name,
                "columns": [
                    (
                        column.name,
                        column.sql_type,
                        column.nullable,
                        column.primary_key,
                        column.default_sql,
                    )
                    for column in table.columns
                ],
                "indexes": list(table.indexes),
                "unique_columns": list(table.unique_columns),
            }
            if allow_extended:
                observed["foreign_keys"] = list(table.foreign_keys)
                observed["foreign_key_actions"] = list(table.foreign_key_actions)
            observations.append(observed)

            if overrides:
                historical = {**observed}

                if historical_exclusion is not None:
                    target, removed = historical_exclusion

                    if table.name == target:
                        if (
                            removed not in {column[0] for column in observed["columns"]}
                            or removed in observed["unique_columns"]
                        ):
                            raise RuntimeError("Removed-column historical evidence is invalid")

                        historical["columns"] = [
                            column for column in observed["columns"] if column[0] != removed
                        ]
                elif historical_type_override is not None:
                    target, changed, previous_type = historical_type_override

                    if table.name == target:
                        matching = [
                            column for column in observed["columns"] if column[0] == changed
                        ]

                        if (
                            len(matching) != 1
                            or changed in observed["unique_columns"]
                            or matching[0][3]
                        ):
                            raise RuntimeError("Historical type evidence is not a plain column")

                        # PostgreSQL format_type() uses these long spellings.
                        # Reconstruct the original catalog representation for
                        # its stored digest; the reviewed operation itself
                        # deliberately uses normalized type names.
                        if dialect == "postgresql":
                            current_type = matching[0][1].lower()

                            if current_type.startswith(
                                "character varying("
                            ) and previous_type.startswith("varchar("):
                                previous_type = (
                                    "character varying(" + previous_type[len("varchar(") :]
                                )
                            elif current_type.startswith("numeric(") and previous_type.startswith(
                                "decimal("
                            ):
                                previous_type = "numeric(" + previous_type[len("decimal(") :]

                        historical["columns"] = [
                            (column[0], previous_type, *column[2:])
                            if column[0] == changed
                            else column
                            for column in observed["columns"]
                        ]
                elif historical_nullability_override is not None:
                    target, changed, previous_nullable = historical_nullability_override

                    if table.name == target:
                        matching = [
                            column for column in observed["columns"] if column[0] == changed
                        ]

                        if (
                            len(matching) != 1
                            or changed in observed["unique_columns"]
                            or matching[0][3]
                            or matching[0][2] is previous_nullable
                        ):
                            raise RuntimeError(
                                "Historical nullability evidence is not a plain transition"
                            )

                        historical["columns"] = [
                            (column[0], column[1], previous_nullable, *column[3:])
                            if column[0] == changed
                            else column
                            for column in observed["columns"]
                        ]
                else:
                    target, changed = historical_default_exclusion

                    if table.name == target:
                        matching = [
                            column for column in observed["columns"] if column[0] == changed
                        ]

                        if (
                            len(matching) != 1
                            or changed in observed["unique_columns"]
                            or matching[0][3]
                            or matching[0][4] is None
                        ):
                            raise RuntimeError(
                                "Historical default evidence is not an added literal"
                            )

                        historical["columns"] = [
                            (*column[:4], None) if column[0] == changed else column
                            for column in observed["columns"]
                        ]

                historical_observations.append(historical)

    payload = json.dumps(
        {"dialect": dialect, "observed": observations},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    current = sha256(payload.encode("utf-8")).hexdigest()

    if not overrides:
        return (current, tuple(observations)) if include_observations else current

    previous_payload = json.dumps(
        {"dialect": dialect, "observed": historical_observations},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return current, sha256(previous_payload.encode("utf-8")).hexdigest()


def verify_server_initial_schema(connection, models):
    """Verify a newly initialized server schema before marking Initial applied."""
    return verify_server_declared_schema(connection, models)
