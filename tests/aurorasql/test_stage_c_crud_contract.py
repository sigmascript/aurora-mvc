"""Stage C: preserve safe CRUD behavior while the public return API is reviewed."""

import pytest

from aurora.aurorasql import Model, column, connect


class CrudEntry(Model):
    __table__ = "stage_c_crud_entries"
    id = column(int, primary_key=True)
    title = column(str)
    active = column(bool)


@pytest.fixture
def db():
    with connect("sqlite:///:memory:") as connection:
        connection.execute_write(
            "CREATE TABLE stage_c_crud_entries "
            "(id INTEGER PRIMARY KEY, title TEXT NOT NULL, active INTEGER NOT NULL)"
        )
        yield connection


def test_create_current_return_and_direct_read(db):
    created = CrudEntry.using(db).create(title="First", active=True)
    assert isinstance(created, CrudEntry)  # Current Alpha 8 compatibility contract.
    assert type(created.id) is int
    assert CrudEntry.using(db).read({"id": created.id}, as_dicts=True) == [
        {"id": created.id, "title": "First", "active": True}
    ]


def test_update_and_delete_report_affected_rows(db):
    entry = CrudEntry.using(db).create(title="First", active=True)
    ops = CrudEntry.using(db)
    assert ops.update(values={"title": "Second"}, where={"id": entry.id}) == 1
    assert ops.update(values={"title": "Missing"}, where={"id": -1}) == 0
    assert ops.delete(where={"id": -1}) == 0
    assert ops.delete(where={"id": entry.id}) == 1
    assert ops.read({"id": entry.id}, as_dicts=True) == []


def test_whole_table_mutations_require_explicit_confirmation(db):
    ops = CrudEntry.using(db)
    ops.create(title="One", active=True)
    with pytest.raises(RuntimeError, match="confirm=True"):
        ops.update(values={"title": "Unsafe"})
    with pytest.raises(RuntimeError, match="confirm=True"):
        ops.delete()
    assert len(ops.read(as_dicts=True)) == 1


def test_explicit_confirmation_and_no_matching_rows(db):
    ops = CrudEntry.using(db)
    ops.create(title="One", active=True)
    ops.create(title="Two", active=False)
    assert ops.update(values={"active": True}, confirm=True) >= 1
    assert ops.delete(confirm=True) == 2
    assert ops.read(as_dicts=True) == []
