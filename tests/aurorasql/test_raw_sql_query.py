"""Direct SQL is an explicit alternative to the model query builder."""

import pytest

from aurora.aurorasql import Model, column, connect, create_table


class RawNote(Model):
    __table__ = "raw_notes"
    id = column(int, primary_key=True)
    title = column(str)


def test_bound_raw_sql_returns_dicts_and_affected_counts():
    with connect("sqlite:///:memory:") as db:
        create_table(db, RawNote)
        note = RawNote.using(db)
        assert note.query("INSERT INTO raw_notes (title) VALUES (:title)", {"title": "hello"}) == 1
        assert note.query("SELECT id, title FROM raw_notes WHERE title = :title", {"title": "hello"}) == [
            {"id": 1, "title": "hello"}
        ]
        assert note.query("UPDATE raw_notes SET title = :title WHERE id = :id", {"title": "updated", "id": 1}) == 1
        assert note.query("DELETE FROM raw_notes WHERE id = :id", {"id": 1}) == 1


def test_raw_sql_parameter_binding_rejects_missing_and_prevents_injection():
    with connect("sqlite:///:memory:") as db:
        create_table(db, RawNote)
        note = RawNote.using(db)
        note.query("INSERT INTO raw_notes (title) VALUES (:title)", {"title": "safe"})
        payload = "safe' OR 1=1 --"
        assert note.query("SELECT id FROM raw_notes WHERE title = :title", {"title": payload}) == []
        with pytest.raises(ValueError, match="Missing SQL parameter"):
            note.query("SELECT id FROM raw_notes WHERE title = :title", {})
        assert len(note.query("SELECT id FROM raw_notes")) == 1


def test_class_query_uses_explicit_connection_scope():
    with connect("sqlite:///:memory:") as db:
        create_table(db, RawNote)
        with Model.connection_scope(db):
            assert RawNote.query("SELECT id FROM raw_notes") == []
