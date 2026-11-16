"""Forward history identity: fresh name, verified adoption, and conflict refusal."""

import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.sqlite_forward_history import (
    TABLE, adopt_history, has_forward_history, initialize, read_history,
)


class Note(Model):
    id = column(int, primary_key=True)
    title = column(str, length=40)


def test_fresh_history_name_and_existing_history_adoption(tmp_path):
    path = tmp_path / "notes.sqlite"
    with sqlite3.connect(path, isolation_level=None) as db:
        initialize(db, tmp_path, path, [Note], "Initial migration")
        db.execute("INSERT INTO note (id,title) VALUES (1,'keep')")
        assert TABLE == "__migrations__"
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == 1
        original, snapshot = read_history(db, tmp_path, path)
        db.execute('ALTER TABLE "__migrations__" RENAME TO "migrations"')
        assert has_forward_history(db)
        assert read_history(db, tmp_path, path) == (original, snapshot)
        assert adopt_history(db, tmp_path, path) == 1
        assert read_history(db, tmp_path, path) == (original, snapshot)
        assert db.execute('SELECT id,title FROM note').fetchall() == [(1, "keep")]
        assert db.execute('SELECT name FROM sqlite_master WHERE name="migrations"').fetchone() is None


def test_dual_history_tables_are_blocked_without_mutation(tmp_path):
    path = tmp_path / "notes.sqlite"
    with sqlite3.connect(path, isolation_level=None) as db:
        initialize(db, tmp_path, path, [Note], "Initial migration")
        db.execute('CREATE TABLE "migrations" (id INTEGER PRIMARY KEY)')
        with pytest.raises(RuntimeError, match="Both migrations and __migrations__"):
            read_history(db, tmp_path, path)
        with pytest.raises(RuntimeError, match="Both migrations and __migrations__"):
            adopt_history(db, tmp_path, path)
        assert db.execute('SELECT COUNT(*) FROM "__migrations__"').fetchone()[0] == 1


def test_server_history_identity_fails_closed_when_conflicting():
    from aurora.database import server_forward_history as server

    class ListedTables:
        dialect = "mysql"

        def __init__(self, names):
            self.names = names

        def fetch_all(self, sql, params=None):
            return [{"name": name, "kind": "BASE TABLE"} for name in self.names]

    assert server.history_table(ListedTables(["notes", "__migrations__"])) == "__migrations__"
    assert server.history_table(ListedTables(["notes", "migrations"])) == "migrations"
    with pytest.raises(RuntimeError, match="Both migrations and __migrations__"):
        server.history_table(ListedTables(["migrations", "__migrations__"]))
