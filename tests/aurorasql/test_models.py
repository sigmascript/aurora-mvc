"""Behavior-focused SQLite model workflow and safety checks."""

import pytest

from aurora.aurorasql import Model, column, connect


class Note(Model):
    __table__ = "notes"
    id = column(int, primary_key=True)
    title = column(str)
    category = column(str, nullable=True)


def test_crud_transaction_and_scoped_writes():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT)")
        notes = Note.using(db)
        with db.transaction():
            notes.create(id=1, title="first", category=None)
            notes.create(id=2, title="second", category="work")
            assert [note.title for note in notes.all()] == ["first", "second"]
            assert notes.first(where={"category": None}).id == 1
            assert notes.first(where={"id": 999}) is None
            assert notes.update(values={"title": "updated"}, where={"id": 2}) == 1
            assert notes.delete(where={"id": 1}) == 1
        assert notes.first(where={"id": 2}).title == "updated"
        assert len(notes.all()) == 1
        with pytest.raises(RuntimeError, match="confirm=True"):
            notes.delete()
        with pytest.raises(RuntimeError, match="confirm=True"):
            notes.update(values={"title": "oops"})
        with pytest.raises(TypeError, match="Unknown column"):
            notes.all(where={"not_a_column": 1})


def test_rollback_and_sql_injection_value():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT)")
        notes = Note.using(db)
        with pytest.raises(ValueError, match="abort"):
            with db.transaction():
                notes.create(id=1, title="x' OR 1=1 --")
                raise ValueError("abort")
        assert notes.all() == []
        notes.create(id=2, title="x' OR 1=1 --")
        assert notes.first(where={"title": "x' OR 1=1 --"}).id == 2
        assert notes.first(where={"title": "x"}) is None
