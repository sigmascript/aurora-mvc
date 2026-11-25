"""Read-only SQLite rehearsal belongs to review, never persistent execution."""

import sqlite3
from hashlib import sha256

import pytest

from aurora.database.migration_review import _rehearse_sqlite


def test_rehearsal_uses_in_memory_copy_and_leaves_source_unchanged(tmp_path):
    db_file = tmp_path / "source.sqlite3"
    with sqlite3.connect(db_file) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO notes (title) VALUES ('untouched')")

    before = sha256(db_file.read_bytes()).hexdigest()
    catalog = _rehearse_sqlite(db_file, ["ALTER TABLE notes RENAME TO renamed_notes"])

    assert catalog is not None
    assert sha256(db_file.read_bytes()).hexdigest() == before
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT title FROM notes").fetchall() == [("untouched",)]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='renamed_notes'"
            ).fetchall()
            == []
        )


def test_failed_rehearsal_does_not_modify_source(tmp_path):
    db_file = tmp_path / "source.sqlite3"
    with sqlite3.connect(db_file) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")

    before = sha256(db_file.read_bytes()).hexdigest()
    with pytest.raises(sqlite3.OperationalError):
        _rehearse_sqlite(db_file, ["ALTER TABLE missing RENAME TO renamed"])
    assert sha256(db_file.read_bytes()).hexdigest() == before
