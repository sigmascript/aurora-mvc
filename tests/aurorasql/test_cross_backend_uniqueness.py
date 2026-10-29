"""Read-only uniqueness discovery, including composite-index safeguards."""

from aurora.aurorasql.inspection import inspect_table


class InspectionConnection:
    def __init__(self, dialect, responses):
        self.dialect = dialect
        self.responses = iter(responses)
        self.queries = []

    def fetch_all(self, sql, params=None):
        self.queries.append((sql, params))
        return next(self.responses)

    def fetch_first(self, sql, params=None):
        self.queries.append((sql, params))
        return {"found": True}


def test_mysql_single_column_unique_excludes_composite_and_primary():
    columns = [
        {"name": "id", "sql_type": "bigint", "nullable": "NO", "column_key": "PRI", "default_sql": None},
        {"name": "email", "sql_type": "varchar(100)", "nullable": "NO", "column_key": "UNI", "default_sql": None},
    ]
    indexes = [
        {"index_name": "PRIMARY", "column_name": "id", "non_unique": 0, "position": 1},
        {"index_name": "email_key", "column_name": "email", "non_unique": 0, "position": 1},
        {"index_name": "composite", "column_name": "email", "non_unique": 0, "position": 1},
        {"index_name": "composite", "column_name": "id", "non_unique": 0, "position": 2},
    ]
    db = InspectionConnection("mysql", [columns, indexes])
    table = inspect_table(db, "accounts")
    assert table.unique_columns == ("email",)
    assert table.uniqueness_verified
    assert not table.constraints_verified
    assert all(params == {"name": "accounts"} for _, params in db.queries)


def test_postgresql_unique_index_discovery_uses_nonpartial_single_key_filter():
    columns = [{"name": "email", "sql_type": "character varying(100)",
                "nullable": False, "primary_key": False, "default_sql": None}]
    indexes = [{"index_name": "accounts_email_key", "column_name": "email",
                "is_unique": True, "is_primary": False, "is_valid": True,
                "is_partial": False, "is_expression": False, "key_count": 1}]
    db = InspectionConnection("postgresql", [columns, indexes])
    table = inspect_table(db, "accounts")
    assert table.unique_columns == ("email",)
    assert table.uniqueness_verified
    assert "pg_catalog.pg_index" in db.queries[-1][0]
    assert "i.indnkeyatts AS key_count" in db.queries[-1][0]


def test_missing_mysql_table_does_not_query_indexes():
    class Missing(InspectionConnection):
        def fetch_first(self, sql, params=None):
            return None

    db = Missing("mysql", [[]])
    table = inspect_table(db, "missing")
    assert not table.exists
    assert not table.uniqueness_verified


def test_postgresql_inventory_includes_composite_and_nonunique_indexes():
    columns = [{"name": "left", "sql_type": "text", "nullable": False,
                "primary_key": False, "default_sql": None}]
    indexes = [
        {"index_name": "pair_unique", "column_name": "left", "is_unique": True,
         "is_primary": False, "is_valid": True, "is_partial": False,
         "is_expression": False, "key_count": 2},
        {"index_name": "pair_lookup", "column_name": "left", "is_unique": False,
         "is_primary": False, "is_valid": True, "is_partial": False,
         "is_expression": False, "key_count": 1},
    ]
    table = inspect_table(InspectionConnection("postgresql", [columns, indexes]), "pair")
    assert table.indexes == ("pair_lookup", "pair_unique")
    assert table.unique_columns == ()
    assert not table.constraints_verified


def test_postgresql_partial_or_invalid_unique_index_is_not_column_unique():
    columns = [{"name": "email", "sql_type": "text", "nullable": True,
                "primary_key": False, "default_sql": None}]
    indexes = [
        {"index_name": "partial_email", "column_name": "email", "is_unique": True,
         "is_primary": False, "is_valid": True, "is_partial": True,
         "is_expression": False, "key_count": 1},
        {"index_name": "invalid_email", "column_name": "email", "is_unique": True,
         "is_primary": False, "is_valid": False, "is_partial": False,
         "is_expression": False, "key_count": 1},
    ]
    table = inspect_table(InspectionConnection("postgresql", [columns, indexes]), "accounts")
    assert len(table.indexes) == 2
    assert table.unique_columns == ()
