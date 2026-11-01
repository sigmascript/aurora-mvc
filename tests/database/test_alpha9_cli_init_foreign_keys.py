"""Recorded foreign-key initialization on disposable SQLite databases."""

import sqlite3
from unittest.mock import patch

import pytest

from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.core import Connection
from aurora.database.init_validation import validate_init_models


class Parent(Model):
    __table__ = "fk_init_parent"
    id = column(int, primary_key=True)


class Child(Model):
    __table__ = "fk_init_child"
    id = column(int, primary_key=True)
    parent_id = column(int, references="fk_init_parent.id")


def test_dependency_order_enforced_and_journaled(tmp_path):
    raw = sqlite3.connect(tmp_path / "fk.sqlite3", isolation_level=None)
    with Connection(raw, "sqlite") as connection:
        assert connection._raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        ordered = validate_init_models((Child, Parent))
        assert ordered == (Parent, Child)
        assert len(_execute_batch(connection, ordered)) == 2
        assert connection._raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        fk = connection._raw.execute('PRAGMA foreign_key_list("fk_init_child")').fetchall()
        assert len(fk) == 1 and fk[0][2] == "fk_init_parent"
        with pytest.raises(sqlite3.IntegrityError):
            connection._raw.execute('INSERT INTO "fk_init_child" (id, parent_id) VALUES (1, 999)')
        records = connection.fetch_all(
            "SELECT table_name, status FROM aurorasql_schema_history ORDER BY rowid"
        )
        assert records == [
            {"table_name": "fk_init_parent", "status": "applied"},
            {"table_name": "fk_init_child", "status": "applied"},
        ]


def test_interrupted_child_preserves_uncertain_history(tmp_path):
    raw = sqlite3.connect(tmp_path / "interrupted.sqlite3", isolation_level=None)
    with Connection(raw, "sqlite") as connection:
        from aurora.database import execution
        actual = execution.execute_create

        def interrupted(conn, model, approval, *, confirm=False):
            if model is Child:
                raise RuntimeError("interrupted child DDL")
            return actual(conn, model, approval, confirm=confirm)

        with patch.object(execution, "execute_create", side_effect=interrupted):
            with pytest.raises(RuntimeError, match="interrupted child"):
                _execute_batch(connection, (Parent, Child))
        rows = connection.fetch_all(
            "SELECT table_name, status FROM aurorasql_schema_history ORDER BY rowid"
        )
        assert rows == [
            {"table_name": "fk_init_parent", "status": "applied"},
            {"table_name": "fk_init_child", "status": "uncertain"},
        ]
        with pytest.raises(RuntimeError, match="Existing migration journal"):
            _execute_batch(connection, (Parent, Child))


def test_active_transaction_refused_before_writes(tmp_path):
    raw = sqlite3.connect(tmp_path / "active.sqlite3", isolation_level=None)
    with Connection(raw, "sqlite") as connection:
        connection._raw.execute("BEGIN")
        with pytest.raises(RuntimeError, match="active transaction"):
            _execute_batch(connection, (Parent, Child))
        connection._raw.rollback()
        assert connection._raw.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='aurorasql_schema_history'"
        ).fetchone()[0] == 0
