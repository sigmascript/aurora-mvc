"""Behavior checks for generated keys, defaults and input validation."""

import pytest

from aurora.aurorasql import Model, column, connect


class Task(Model):
    id = column(int, primary_key=True)
    title = column(str, length=20)
    status = column(str, default="new")
    detail = column(str, nullable=True)


def test_generated_ids_defaults_and_scoped_updates():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                   "status TEXT NOT NULL, detail TEXT)")
        tasks = Task.using(db)
        with db.transaction():
            first = tasks.create(title="One")
            second = tasks.create(title="Two", detail=None)
            assert (first.id, second.id) == (1, 2)
            assert first.status == "new"
            assert tasks.first(where={"id": 1}).title == "One"
            assert tasks.update(values={"status": "done"}, where={"id": 2}) == 1
        assert tasks.first(where={"id": 2}).status == "done"


def test_invalid_values_rejected_before_writes():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                   "status TEXT NOT NULL, detail TEXT)")
        tasks = Task.using(db)
        with pytest.raises(ValueError, match="length"):
            tasks.create(title="x" * 21)
        with pytest.raises(TypeError, match="Expected str"):
            tasks.create(title=10)
        with pytest.raises(ValueError, match="Non-nullable"):
            tasks.create(title=None)
        with pytest.raises(TypeError, match="Boolean"):
            tasks.create(id=True, title="no")
        assert tasks.all() == []


def test_empty_insert_and_generated_id():
    class Token(Model):
        id = column(int, primary_key=True)

    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE token (id INTEGER PRIMARY KEY)")
        token = Token.using(db).create()
        assert token.id == 1
