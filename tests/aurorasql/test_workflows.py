"""General AuroraSQL result and transaction lifecycle workflows."""

import sqlite3

import pytest

from aurora.aurorasql import connect
from aurora.aurorasql.core import Connection


def test_result_helpers_and_rollback_workflow():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, name TEXT)")
        with db.transaction():
            assert db.execute_write("INSERT INTO entries(name) VALUES (:name)", {"name": "first"}) == 1
        assert db.fetch_first("SELECT name FROM entries WHERE name=:name", {"name": "first"}) == {"name": "first"}
        assert db.fetch_first("SELECT name FROM entries WHERE name=:name", {"name": "missing"}) is None
        assert db.fetch_all("SELECT name FROM entries") == [{"name": "first"}]
        with pytest.raises(ValueError, match="did not return rows"):
            db.fetch_all("DELETE FROM entries WHERE name='missing'")
        with pytest.raises(ValueError, match="returned rows"):
            db.execute_write("SELECT name FROM entries")
        with pytest.raises(RuntimeError, match="cancel"):
            with db.transaction():
                db.execute_write("INSERT INTO entries(name) VALUES ('second')")
                raise RuntimeError("cancel")
        assert db.fetch_all("SELECT name FROM entries") == [{"name": "first"}]


def test_close_is_idempotent_and_rejects_reuse():
    db = connect("sqlite:///:memory:")
    db.close()
    db.close()
    with pytest.raises(RuntimeError, match="closed"):
        db.fetch_first("SELECT 1")


def test_begin_failure_does_not_leave_transaction_flag_stuck():
    raw = sqlite3.connect(":memory:", isolation_level=None)
    db = Connection(raw, "sqlite")
    raw.execute("BEGIN")
    with pytest.raises(sqlite3.OperationalError):
        with db.transaction():
            pass
    assert db._transaction_active is False
    raw.rollback()
    with db.transaction():
        assert db.fetch_first("SELECT 1 AS value") == {"value": 1}
    db.close()
