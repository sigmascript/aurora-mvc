"""General workflow tests for the experimental internal DB-API layer."""

import pytest

from aurora.aurorasql import connect


def test_sqlite_transaction_commit_rollback_and_named_parameters():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        with db.transaction():
            assert db.execute("INSERT INTO notes(title) VALUES (:title)", {"title": "one"}) == 1
        assert db.execute("SELECT title FROM notes") == [{"title": "one"}]
        with pytest.raises(ValueError):
            with db.transaction():
                db.execute("INSERT INTO notes(title) VALUES (:title)", {"title": "two"})
                raise ValueError("rollback")
        assert db.execute("SELECT title FROM notes") == [{"title": "one"}]
        with db.transaction():
            with pytest.raises(RuntimeError, match="Nested"):
                with db.transaction():
                    pass


def test_sqlite_parameter_validation_and_closed_connection():
    db = connect("sqlite:///:memory:")
    with pytest.raises(ValueError, match="Missing SQL parameter"):
        db.execute("SELECT :missing")
    assert db.execute("SELECT :value AS answer", {"value": 42}) == [{"answer": 42}]
    db.close()
    with pytest.raises(RuntimeError, match="closed"):
        db.execute("SELECT 1")


def test_sqlite_foreign_keys_are_enabled_on_connection():
    with connect("sqlite:///:memory:") as db:
        assert db.fetch_first("PRAGMA foreign_keys")["foreign_keys"] == 1
        db.execute_write("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        db.execute_write("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        with pytest.raises(Exception):
            db.execute_write("INSERT INTO child(parent_id) VALUES (999)")
