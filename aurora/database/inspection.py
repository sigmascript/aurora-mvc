"""Read-only schema discovery for AuroraSQL's three supported dialects.

Information-schema inspection is limited to the current database/schema.
No database changes or implicit schema creation are performed here.
"""

from dataclasses import dataclass

from .models import _identifier


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
            table_name, True,
            tuple(ColumnInfo(
                row["name"], row["type"],
                not bool(row["notnull"]) and not bool(row["pk"]),
                bool(row["pk"]), row["dflt_value"],
            ) for row in rows),
            tuple(sorted(unique_columns)), tuple(sorted(index_names)),
            foreign_keys_verified, True, tuple(sorted(foreign_keys)),
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
        exists = connection.fetch_first(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name AND TABLE_TYPE = 'BASE TABLE'",
            {"name": table_name},
        ) is not None

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
            if (name != "PRIMARY" and len(members) == 1
                    and not bool(members[0]["non_unique"])
                    and members[0]["column_name"] is not None):
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
            and all(row[key] is not None for key in (
                "source_column", "target_table", "target_column", "update_rule", "delete_rule"))
            and row["update_rule"].upper() in allowed_actions
            and row["delete_rule"].upper() in allowed_actions
            for row in fk_rows
        )
        fk_targets = tuple(sorted((row["source_column"], row["target_table"], row["target_column"])
                                  for row in fk_rows)) if fk_verified else ()
        fk_actions = tuple(sorted((row["source_column"], row["update_rule"].upper(), row["delete_rule"].upper())
                                  for row in fk_rows)) if fk_verified else None
        return TableInfo(table_name, True, tuple(
            ColumnInfo(row["name"], row["sql_type"], row["nullable"] == "YES", row["column_key"] == "PRI", row["default_sql"])
            for row in rows
        ), tuple(sorted(unique_columns)), tuple(sorted(groups)), fk_verified, True,
            fk_targets, fk_actions)

    if dialect == "postgresql":
        # Resolve via the connection's current search_path, without concatenating
        # user-controlled table names into SQL identifiers.
        exists = connection.fetch_first(
            "SELECT c.oid AS oid FROM pg_catalog.pg_class c "
            "WHERE c.oid = to_regclass(:name) AND c.relkind IN ('r','p')",
            {"name": table_name},
        ) is not None

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
        unique_columns = tuple(sorted({
            row["column_name"] for row in index_rows
            if row["is_unique"] and not row["is_primary"]
            and row["is_valid"] and not row["is_partial"]
            and not row["is_expression"] and row["key_count"] == 1
            and row["column_name"] is not None
        }))
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
        actions = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                   "n": "SET NULL", "d": "SET DEFAULT"}
        fk_verified = all(
            int(row["column_count"]) == 1
            and row["same_schema"] is True
            and row["validated"] is True
            and row["deferrable"] is False
            and row["match_type"] == "s"
            and all(row[key] is not None for key in (
                "source_column", "target_table", "target_column"))
            and row["update_code"] in actions and row["delete_code"] in actions
            for row in fk_rows
        )
        fk_targets = tuple(sorted((row["source_column"], row["target_table"], row["target_column"])
                                  for row in fk_rows)) if fk_verified else ()
        fk_actions = tuple(sorted((row["source_column"], actions[row["update_code"]], actions[row["delete_code"]])
                                  for row in fk_rows)) if fk_verified else None
        return TableInfo(table_name, True, tuple(
            ColumnInfo(row["name"], row["sql_type"], bool(row["nullable"]), bool(row["primary_key"]), row["default_sql"])
            for row in rows
        ), unique_columns, tuple(sorted({row["index_name"] for row in index_rows})),
            fk_verified, True, fk_targets, fk_actions)

    raise ValueError("Unsupported database dialect")
