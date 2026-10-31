"""NoteFlow model declaration and ownership-contract compatibility checks."""

import pytest

from aurora.aurorasql import Model, column, connect
from aurora.aurorasql.schema import create_table_sql, create_tables


class User(Model):
    __table__ = "users"
    id = column(int, primary_key=True)
    username = column(str, length=100, unique=True)
    email = column(str, length=254, unique=True)
    password_hash = column(str, length=255)


class Note(Model):
    __table__ = "notes"
    id = column(int, primary_key=True)
    user_id = column(int, references="users.id")
    title = column(str, length=200)
    content = column(str)


def test_noteflow_foreign_key_and_owner_scoped_crud():
    with connect("sqlite:///:memory:") as db:
        db.execute("PRAGMA foreign_keys=ON")
        create_tables(db, User, Note)
        user = User.using(db).create(username="one", email="one@example.com", password_hash="hash")
        note = Note.using(db).create(user_id=user.id, title="Private", content="Secret")
        assert Note.using(db).legacy_query({"id": note.id, "user_id": user.id}).first().title == "Private"
        assert Note.using(db).legacy_query({"id": note.id, "user_id": user.id + 1}).first() is None
        assert Note.using(db).update(values={"title": "Changed"}, where={"id": note.id, "user_id": user.id + 1}) == 0
        assert Note.using(db).delete(where={"id": note.id, "user_id": user.id + 1}) == 0
        with pytest.raises(Exception):
            Note.using(db).create(user_id=user.id + 1, title="Invalid", content="No owner")


def test_foreign_key_sql_is_dialect_quoted_and_reference_validated():
    for dialect, quote in (("sqlite", '"'), ("mysql", '`'), ("postgresql", '"')):
        ddl = create_table_sql(Note, dialect)
        assert f"FOREIGN KEY ({quote}user_id{quote}) REFERENCES {quote}users{quote}({quote}id{quote})" in ddl
    with pytest.raises(ValueError):
        column(int, references="users.id; DROP TABLE users")
