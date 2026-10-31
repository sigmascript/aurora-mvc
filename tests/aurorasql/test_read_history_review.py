import pytest

from aurora.aurorasql import (Model, Query, column, connect, create_table,
                              initialize_history, unresolved_history)


class Entry(Model):
    id = column(int, primary_key=True)
    title = column(str, length=50)


def test_read_is_lazy_and_supports_alpha4_query_shape():
    with connect("sqlite:///:memory:") as db:
        create_table(db, Entry)
        operations = Entry.using(db)
        query = operations.legacy_query({"title": "one"})
        assert isinstance(query, Query)
        assert query.all() == []
        assert query.first() is None
        operations.create(title="one")
        operations.create(title="two")
        assert [row.title for row in query.all()] == ["one"]
        assert query.first().title == "one"
        assert len(operations.legacy_query().all()) == 2


def test_read_rejects_unknown_fields_and_copies_filters():
    with connect("sqlite:///:memory:") as db:
        operations = Entry.using(db)
        with pytest.raises((ValueError, KeyError, TypeError)):
            operations.legacy_query({"not_a_column": "value"})
        where = {"title": "one"}
        query = operations.legacy_query(where)
        where["title"] = "two"
        assert query.where == {"title": "one"}


def test_unresolved_history_is_read_only_and_filterable():
    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        assert unresolved_history(db) == ()
        db.execute_write('INSERT INTO aurorasql_schema_history '
                         '(operation_id, table_name, dialect, fingerprint, status) '
                         'VALUES (:id, :name, :dialect, :fingerprint, :status)',
                         {"id": "a", "name": "items", "dialect": "sqlite",
                          "fingerprint": "f", "status": "uncertain"})
        assert len(unresolved_history(db)) == 1
        assert len(unresolved_history(db, table="items")) == 1
        assert unresolved_history(db, table="other") == ()
