"""Read-only schema discovery for AuroraSQL's three supported dialects.

Information-schema inspection is limited to the current database/schema.
No database changes or implicit schema creation are performed here.
"""

from dataclasses import dataclass

from .models import _identifier


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    sql_type: str
    nullable: bool
    primary_key: bool
    default_sql: str | None = None


@dataclass(frozen=True)
class TableInfo:
    name: str
    exists: bool
    columns: tuple[ColumnInfo, ...]
    unique_columns: tuple[str, ...] = ()
    indexes: tuple[str, ...] = ()
    constraints_verified: bool = False
    uniqueness_verified: bool = False


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
        unique_columns = set()
        index_names = []
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
            tuple(sorted(unique_columns)), tuple(sorted(index_names)), True, True,
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
        return TableInfo(table_name, True, tuple(
            ColumnInfo(row["name"], row["sql_type"], row["nullable"] == "YES", row["column_key"] == "PRI", row["default_sql"])
            for row in rows
        ), tuple(sorted(unique_columns)), tuple(sorted(groups)), False, True)
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
        return TableInfo(table_name, True, tuple(
            ColumnInfo(row["name"], row["sql_type"], bool(row["nullable"]), bool(row["primary_key"]), row["default_sql"])
            for row in rows
        ), unique_columns, tuple(sorted({row["index_name"] for row in index_rows})), False, True)
    raise ValueError("Unsupported database dialect")
