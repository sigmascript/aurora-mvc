"""Focused Alpha 4-style model API compatibility tests."""

import pytest

from aurora.aurorasql import Model, column, connect


class Entry(Model):
    __table__ = "simple_query_entries"
    id = column(int, primary_key=True)
    category = column(str)


class Category(Model):
    __table__ = "simple_query_categories"
    id = column(int, primary_key=True)
    name = column(str)


def test_simple_read_parameters_and_join():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE simple_query_entries (id INTEGER PRIMARY KEY, category TEXT)")
        db.execute_write("CREATE TABLE simple_query_categories (id INTEGER PRIMARY KEY, name TEXT)")
        Model.bind(db)
        try:
            with db.transaction():
                for index in range(1, 5):
                    Entry.create(id=index, category="work")
                Category.create(id=1, name="one")
            query = Entry.read({"category": "work"}, order_by="-id", limit=2, offset=1)
            assert [row.id for row in query.all()] == [3, 2]
            assert query.count() == 4
            assert Entry.read(order_by="id").first().id == 1
            assert Entry.read().join(Category, on=("id", "id")).all()[0]["right__name"] == "one"
            assert [row.id for row in Entry.using(db).read(order_by="-id", limit=1).all()] == [4]
        finally:
            Model.bind(None)


def test_simple_query_rejects_invalid_options_and_requires_binding():
    with pytest.raises(RuntimeError, match="No database"):
        Entry.read()
    with connect("sqlite:///:memory:") as db:
        Model.bind(db)
        try:
            with pytest.raises(ValueError, match="Unknown ordering column"):
                Entry.read(order_by="id; DROP TABLE entries")
            for invalid in (-1, True, 1.2):
                with pytest.raises(ValueError, match="limit"):
                    Entry.read(limit=invalid)
                with pytest.raises(ValueError, match="offset"):
                    Entry.read(offset=invalid)
            with pytest.raises(ValueError, match="offset requires limit"):
                Entry.read(offset=1).all()
        finally:
            Model.bind(None)
