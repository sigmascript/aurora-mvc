"""Multi-column ordering compatibility and input-safety checks."""

import pytest

from aurora.aurorasql import Model, column, connect


class Entry(Model):
    __table__ = "ordering_entries"
    id = column(int, primary_key=True)
    category = column(str)
    priority = column(int)


def test_legacy_dictionary_and_compact_list_have_identical_order():
    with connect("sqlite:///:memory:") as db:
        db.execute_write(
            "CREATE TABLE ordering_entries (id INTEGER PRIMARY KEY, category TEXT, priority INTEGER)"
        )
        entries = Entry.using(db)
        with db.transaction():
            for id_, category, priority in (
                (1, "a", 2), (2, "b", 1), (3, "a", 1), (4, "b", 2)
            ):
                entries.create(id=id_, category=category, priority=priority)

        expected = [1, 3, 4, 2]
        for order in ({"category": "ASC", "priority": "DESC"}, ["category", "-priority"]):
            assert [item.id for item in entries.legacy_query(order_by=order).all()] == expected
            assert entries.legacy_query(order_by=order, limit=2, offset=1).first().id == 3
        assert [item.id for item in entries.legacy_query(order_by="-id").all()] == [4, 3, 2, 1]


def test_ordering_rejects_unknown_columns_directions_and_duplicates():
    with connect("sqlite:///:memory:") as db:
        entries = Entry.using(db)
        for order in ({"id": "DROP"}, ["id", "-id"], ["id; DROP TABLE ordering_entries"], [], {"id": 1}):
            with pytest.raises(ValueError):
                entries.legacy_query(order_by=order)
