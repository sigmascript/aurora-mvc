"""Direct model reads: plain results, filters, pagination, and invalid input."""

import pytest

from aurora.database import Model, column, connect


class ReadEntry(Model):
    __table__ = "active_read_entries"
    id = column(int, primary_key=True)
    title = column(str)
    active = column(bool)


@pytest.fixture
def db():
    with connect("sqlite:///:memory:") as connection:
        connection.execute_write(
            "CREATE TABLE active_read_entries (id INTEGER PRIMARY KEY, title TEXT, active INTEGER)"
        )
        with connection.transaction():
            ReadEntry.using(connection).create({"id": 1, "title": "Alpha", "active": True})
            ReadEntry.using(connection).create({"id": 2, "title": "Beta", "active": False})
        yield connection


def test_direct_reads_return_plain_normalized_rows(db):
    rows = ReadEntry.using(db).read({"id--ge": 1}, order_by={"id": "DESC"}, limit=1)
    assert rows == [{"id": 2, "title": "Beta", "active": False}]
    assert type(rows[0]) is dict
    assert type(rows[0]["active"]) is bool
    assert ReadEntry.using(db).read({"id": 999}) == []
    assert ReadEntry.using(db).read(order_by={"id": "ASC"}, limit=1, offset=1) == rows
    with Model.connection_scope(db):
        assert ReadEntry.read({"id": 1}) == [{"id": 1, "title": "Alpha", "active": True}]


def test_direct_reads_fail_closed_on_invalid_arguments(db):
    with pytest.raises(TypeError, match="as_dicts"):
        ReadEntry.using(db).read(as_dicts="yes")
    with pytest.raises(TypeError, match="Unknown column"):
        ReadEntry.using(db).read({"missing": 1})
    with pytest.raises(ValueError, match="direction"):
        ReadEntry.using(db).read(order_by={"id": "RANDOM"})
