"""FK dependency inspection owns the shared table-participation safety check."""

from types import SimpleNamespace

from aurora.database.foreign_keys import _foreign_key_dependency


def test_mysql_dependency_checks_both_source_and_referenced_table():
    queries = []

    def lookup(sql, params):
        queries.append((sql, params))
        return {"present": 1}

    connection = SimpleNamespace(dialect="mysql", fetch_first=lookup)
    assert _foreign_key_dependency(connection, "items") is True
    assert len(queries) == 1
    assert "TABLE_NAME=:table OR REFERENCED_TABLE_NAME=:table" in queries[0][0]
    assert queries[0][1] == {"table": "items"}


def test_postgresql_dependency_checks_both_fk_directions_and_absence():
    queries = []

    def lookup(sql, params):
        queries.append((sql, params))
        return None

    connection = SimpleNamespace(dialect="postgresql", fetch_first=lookup)
    assert _foreign_key_dependency(connection, "items") is False
    assert len(queries) == 1
    assert "c.conrelid=to_regclass(:table)" in queries[0][0]
    assert "c.confrelid=to_regclass(:table)" in queries[0][0]
    assert queries[0][1] == {"table": "items"}
