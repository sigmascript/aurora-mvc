"""Focused query workflow checks; no live database required."""

import pytest

from aurora.aurorasql import Model, column, connect


class Task(Model):
    __table__ = "tasks"
    id = column(int, primary_key=True)
    title = column(str)
    group = column(str)


def test_ordered_paginated_query_is_lazy_and_reusable():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, \"group\" TEXT)")
        tasks = Task.using(db)
        with db.transaction():
            for index in range(1, 6):
                tasks.create(id=index, title=f"task-{index}", group="a" if index < 4 else "b")
        base = tasks.legacy_query({"group": "a"}).order_by("id", descending=True)
        page = base.limit(2).offset(1)
        assert [item.id for item in page.all()] == [2, 1]
        assert page.first().id == 2
        assert [item.id for item in base.all()] == [3, 2, 1]
        assert tasks.legacy_query().order_by("id").limit(0).all() == []


def test_query_rejects_unsafe_ordering_and_invalid_pagination():
    with connect("sqlite:///:memory:") as db:
        query = Task.using(db).legacy_query()
        with pytest.raises(ValueError, match="Unknown ordering column"):
            query.order_by("id; DROP TABLE tasks")
        with pytest.raises(TypeError, match="descending"):
            query.order_by("id", descending=1)
        for invalid in (-1, True, 1.5):
            with pytest.raises(ValueError, match="limit"):
                query.limit(invalid)
            with pytest.raises(ValueError, match="offset"):
                query.offset(invalid)
        with pytest.raises(ValueError, match="offset requires limit"):
            query.offset(1).all()
