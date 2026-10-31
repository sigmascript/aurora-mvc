"""Direct read results and explicit query compatibility contract."""

import pytest

from aurora.aurorasql import Model, column, connect


class ReadEntry(Model):
    __table__ = "read_entries"
    id = column(int, primary_key=True)
    title = column(str)
    active = column(bool)


@pytest.fixture
def db():
    with connect("sqlite:///:memory:") as connection:
        connection.execute_write(
            "CREATE TABLE read_entries (id INTEGER PRIMARY KEY, title TEXT, active INTEGER)"
        )
        with connection.transaction():
            ReadEntry.using(connection).create(id=1, title="Alpha", active=True)
            ReadEntry.using(connection).create(id=2, title="Beta", active=False)
        yield connection


def test_direct_read_returns_plain_dicts_with_normalized_types(db):
    rows = ReadEntry.using(db).read(
        {"id--ge": 1}, order_by={"id": "DESC"}, limit=1
    )
    assert rows == [{"id": 2, "title": "Beta", "active": False}]
    assert type(rows[0]) is dict
    assert type(rows[0]["active"]) is bool


def test_direct_read_empty_and_offset(db):
    assert ReadEntry.using(db).read({"id": 999}) == []
    assert ReadEntry.using(db).read(
        order_by={"id": "ASC"}, limit=1, offset=1
    ) == [{"id": 2, "title": "Beta", "active": False}]


def test_explicit_compatibility_query_remains_available(db):
    query = ReadEntry.using(db).legacy_query({"id": 1})
    assert query.first().title == "Alpha"
    assert query.all()[0].active is True
    assert query.count() == 1
    with Model.connection_scope(db):
        assert ReadEntry.legacy_query({"id": 1}).first().title == "Alpha"


def test_class_level_direct_read(db):
    with Model.connection_scope(db):
        assert ReadEntry.read({"id": 1}) == [
            {"id": 1, "title": "Alpha", "active": True}
        ]


def test_invalid_arguments_fail_closed(db):
    with pytest.raises(TypeError, match="as_dicts"):
        ReadEntry.using(db).read(as_dicts="yes")
    with pytest.raises(TypeError, match="Unknown column"):
        ReadEntry.using(db).read({"missing": 1})
    with pytest.raises(ValueError, match="direction"):
        ReadEntry.using(db).read(order_by={"id": "RANDOM"})
