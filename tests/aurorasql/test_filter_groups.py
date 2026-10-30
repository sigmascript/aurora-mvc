"""Explicit AND/OR groups preserve safe, bounded filtering semantics."""

import pytest

from aurora.aurorasql import Model, column, connect


class Entry(Model):
    __table__ = "group_entries"
    id = column(int, primary_key=True)
    age = column(int)
    role = column(str)
    status = column(str)


@pytest.fixture
def entries():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE group_entries (id INTEGER PRIMARY KEY, age INTEGER, role TEXT, status TEXT)")
        for row in ((1, 17, "admin", "active"), (2, 25, "editor", "active"),
                    (3, 40, "guest", "inactive"), (4, 60, "admin", "inactive")):
            Entry.using(db).create(id=row[0], age=row[1], role=row[2], status=row[3])
        yield Entry.using(db)


def ids(entries, where):
    return [row.id for row in entries.read(where, order_by="id").all()]


def test_implicit_and_and_explicit_or(entries):
    assert ids(entries, {"age--ge": 18, "age--lt": 50}) == [2, 3]
    assert ids(entries, {"$or": [{"id": 1}, {"id": 3}]}) == [1, 3]
    assert ids(entries, {"status": "active", "$or": [
        {"role": "admin"}, {"age--ge": 20}]}) == [1, 2]


def test_explicit_and_and_nested_groups(entries):
    where = {"$or": [
        {"$and": [{"age--ge": 20}, {"age--le": 40}, {"role--ne": "editor"}]},
        {"id--in": [1, 4]},
    ]}
    assert ids(entries, where) == [1, 3, 4]
    assert entries.read(where).count() == 3
    assert entries.read(where).exists()
    assert entries.first(where=where).id == 1


def test_grouped_write_filters_are_restricted(entries):
    assert entries.update(values={"status": "review"}, where={"$or": [
        {"id": 1}, {"id": 2}]}) == 2
    assert ids(entries, {"status": "review"}) == [1, 2]
    assert entries.delete(where={"$and": [{"age--gt": 50}, {"role": "admin"}]}) == 1
    assert ids(entries, {"id--in": [1, 2, 3, 4]}) == [1, 2, 3]


@pytest.mark.parametrize("where,error", [
    ({"$or": []}, ValueError),
    ({"$and": []}, ValueError),
    ({"$or": {}}, ValueError),
    ({"$or": [{}, {"id": 1}]}, ValueError),
    ({"$or": [{"id": 1}, 2]}, TypeError),
    ({"$and": "id=1"}, ValueError),
    ({"$xor": [{"id": 1}]}, TypeError),
    ({"$or": [{"bad_column": 1}]}, TypeError),
    ({"$or": [{"id--in": []}]}, ValueError),
    ({"$or": [{"id--gt": "1 OR 1=1"}]}, TypeError),
])
def test_invalid_groups_rejected_for_reads_and_writes(entries, where, error):
    with pytest.raises(error):
        entries.read(where)
    with pytest.raises(error):
        entries.update(values={"status": "unsafe"}, where=where)
    with pytest.raises(error):
        entries.delete(where=where)
    assert ids(entries, {"status": "unsafe"}) == []


def test_depth_limit_and_parameter_safety(entries):
    where = {"id": 1}
    for _ in range(9):
        where = {"$or": [where]}
    with pytest.raises(ValueError, match="depth"):
        entries.read(where)
    assert ids(entries, {"$or": [{"role": "admin' OR 1=1 --"}, {"id": 2}]}) == [2]
    assert entries.read({"$or": [{"id": 1}, {"id": 2}]}).count() == 2


def test_empty_top_level_preserves_explicit_whole_table_confirmation(entries):
    with pytest.raises(RuntimeError, match="confirm"):
        entries.update(values={"status": "unsafe"}, where={})
    with pytest.raises(RuntimeError, match="confirm"):
        entries.delete(where={})
