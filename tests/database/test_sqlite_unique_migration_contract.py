"""Guarded forward-only SQLite single-column UNIQUE changes."""

import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.connections import Connection
from aurora.database.initialization import sqlite_initialize as initialize
from aurora.database.migration_execution import sqlite_column_edit_proposal
from aurora.database.migration_execution import sqlite_migrate_column_edit as migrate_column_edit
from aurora.database.migration_history import sqlite_read_history as read_history
from aurora.database.migration_review import sqlite_matches_models as matches_models


class Email(Model):
    __table__ = "unique_transition"
    id = column(int, primary_key=True)
    email = column(str, length=254, not_null=False)


class UniqueEmail(Model):
    __table__ = "unique_transition"
    id = column(int, primary_key=True)
    email = column(str, length=254, unique=True, not_null=False)


def _migrate(raw, root, path, model, title):
    proposal = sqlite_column_edit_proposal(
        Connection(raw, "sqlite"),
        [model],
        "default",
        path,
        forward=True,
    )
    assert proposal is not None
    return migrate_column_edit(raw, root, path, [model], title, proposal)


def test_unique_added_and_removed_with_forward_history_and_preserved_rows(tmp_path):
    path = tmp_path / "unique.sqlite"
    with sqlite3.connect(path, isolation_level=None) as raw:
        initialize(raw, tmp_path, path, [Email], "initial")
        raw.executemany(
            "INSERT INTO unique_transition (id,email) VALUES (?,?)",
            [(1, "a@example.com"), (2, "b@example.com"), (3, None), (4, None)],
        )
        _migrate(raw, tmp_path, path, UniqueEmail, "unique on")
        assert matches_models(raw, [UniqueEmail])
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO unique_transition (id,email) VALUES (5,?)", ("a@example.com",))
        _migrate(raw, tmp_path, path, Email, "unique off")
        raw.execute("INSERT INTO unique_transition (id,email) VALUES (5,?)", ("a@example.com",))
        assert raw.execute("SELECT COUNT(*) FROM unique_transition").fetchone()[0] == 5
        assert matches_models(raw, [Email])
        assert len(read_history(raw, tmp_path, path)[0]) == 3
        assert (
            sqlite_column_edit_proposal(
                Connection(raw, "sqlite"),
                [Email],
                "default",
                path,
                forward=True,
            )
            is None
        )


def test_unique_add_rejects_existing_duplicates_without_history_change(tmp_path):
    path = tmp_path / "duplicate.sqlite"
    with sqlite3.connect(path, isolation_level=None) as raw:
        initialize(raw, tmp_path, path, [Email], "initial")
        raw.executemany(
            "INSERT INTO unique_transition (id,email) VALUES (?,?)",
            [(1, "duplicate"), (2, "duplicate")],
        )
        with pytest.raises(ValueError, match="duplicate"):
            sqlite_column_edit_proposal(
                Connection(raw, "sqlite"),
                [UniqueEmail],
                "default",
                path,
                forward=True,
            )
        assert len(read_history(raw, tmp_path, path)[0]) == 1
        assert raw.execute("SELECT COUNT(*) FROM unique_transition").fetchone()[0] == 2


def test_unique_change_refuses_dependent_index(tmp_path):
    path = tmp_path / "index.sqlite"
    with sqlite3.connect(path, isolation_level=None) as raw:
        initialize(raw, tmp_path, path, [Email], "initial")
        # External indexes must never be silently erased by table reconstruction.
        raw.execute("CREATE INDEX external_email_idx ON unique_transition(email)")
        with pytest.raises(RuntimeError, match="dependencies"):
            sqlite_column_edit_proposal(
                Connection(raw, "sqlite"),
                [UniqueEmail],
                "default",
                path,
                forward=True,
            )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name=?", ("external_email_idx",)
            ).fetchone()[0]
            == 1
        )
