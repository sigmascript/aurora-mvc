"""Server migration execution refuses stale or uncertain review outcomes."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_migration as subject


class FakeConnection:
    _transaction_active = False
    _migration_lock_active = False

    def __init__(self, dialect, *, deny_lock=False, fail_ddl=False):
        self.dialect = dialect
        self.events = []
        self.deny_lock = deny_lock
        self.fail_ddl = fail_ddl

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def fetch_first(self, statement, params):
        self.events.append("lock")
        return {"acquired": not self.deny_lock}

    def execute_write(self, statement, *args):
        self.events.append("ddl")
        if self.fail_ddl:
            raise RuntimeError("simulated write failure")
        raise AssertionError("Test must stop before successful DDL")


@pytest.fixture
def stub_dependencies(monkeypatch):
    from aurora.database import cli_init

    monkeypatch.setattr(subject, "history_table", lambda _: "__migrations__")

    state = {"current_identity": "verified", "fresh_review": None, "read_count": 0}
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: state["current_identity"])
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: state["current_identity"])

    def history(*_):
        state["read_count"] += 1
        return ([{"id": 1}], {})

    monkeypatch.setattr(subject, "read_history", history)
    monkeypatch.setattr(subject, "review_server_model_changes", lambda *_: state["fresh_review"])
    monkeypatch.setattr(subject, "review_server_column_widening", lambda *_: state["fresh_review"])

    @contextmanager
    def simulated_mysql_lock(connection):
        connection.events.append("lock")
        yield
        connection.events.append("unlock")

    monkeypatch.setattr(subject, "migration_lock", simulated_mysql_lock)
    return state


def execute(connection, tmp_path, review):
    return subject.execute_server_forward_migration(
        connection, (), tmp_path, "test_alias", identity="verified", parsed=object(),
        review=review, approval="approved", title="Safe review", backup_path=None,
        config_path=str(tmp_path),
    )


@pytest.fixture
def review():
    return SimpleNamespace(
        operations=[{"kind": "add_column", "table": "notes", "column": "caption"}],
        statements=["ALTER TABLE notes ADD COLUMN caption TEXT"],
        review_sha256="approved",
    )


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_changed_database_identity_blocks_ddl(tmp_path, stub_dependencies, review, dialect):
    stub_dependencies["fresh_review"] = review
    stub_dependencies["current_identity"] = "other-database"
    conn = FakeConnection(dialect)
    with pytest.raises(RuntimeError, match="identity changed"):
        execute(conn, tmp_path, review)
    assert "ddl" not in conn.events
    assert stub_dependencies["read_count"] == 0


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_changed_review_blocks_ddl(tmp_path, stub_dependencies, review, dialect):
    stub_dependencies["fresh_review"] = SimpleNamespace(
        operations=review.operations,
        statements=["ALTER TABLE notes DROP COLUMN caption"],
        review_sha256="different",
    )
    conn = FakeConnection(dialect)
    with pytest.raises(RuntimeError, match="proposal changed"):
        execute(conn, tmp_path, review)
    assert "ddl" not in conn.events
    assert stub_dependencies["read_count"] == 1


def test_mysql_ddl_failure_requires_manual_reconciliation(tmp_path, stub_dependencies, review):
    stub_dependencies["fresh_review"] = review
    conn = FakeConnection("mysql", fail_ddl=True)
    with pytest.raises(RuntimeError, match="outcome uncertain after DDL execution began"):
        execute(conn, tmp_path, review)
    assert conn.events == ["lock", "ddl"]


def test_postgres_ddl_failure_rolls_back_transaction(tmp_path, stub_dependencies, review):
    stub_dependencies["fresh_review"] = review
    conn = FakeConnection("postgresql", fail_ddl=True)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        execute(conn, tmp_path, review)
    assert conn.events == ["begin", "lock", "ddl", "rollback"]


def test_postgres_advisory_lock_refusal_blocks_ddl(tmp_path, stub_dependencies, review):
    stub_dependencies["fresh_review"] = review
    conn = FakeConnection("postgresql", deny_lock=True)
    with pytest.raises(RuntimeError, match="lock unavailable"):
        execute(conn, tmp_path, review)
    assert conn.events == ["begin", "lock", "rollback"]
    assert stub_dependencies["read_count"] == 0
