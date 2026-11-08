"""PostgreSQL guarded initialization contract (no server required)."""

from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import contextmanager

import pytest

from aurora.database import cli_init, postgres_init, server_migration_timeline
from aurora.database.postgres_init import execute_postgres_init
from tests.database.test_schema import Entry


def test_wrong_dialect_never_opens_transaction():
    connection = SimpleNamespace(dialect="sqlite", transaction=Mock())

    with pytest.raises(ValueError, match="PostgreSQL"):
        execute_postgres_init(connection, [object()], approval="a" * 64,
                              expected_approval="a" * 64)

    connection.transaction.assert_not_called()


def test_missing_approval_never_opens_transaction():
    connection = SimpleNamespace(dialect="postgresql", transaction=Mock())

    with pytest.raises(PermissionError, match="approval"):
        execute_postgres_init(connection, [object()], approval="wrong",
                              expected_approval="a" * 64)

    connection.transaction.assert_not_called()


def test_empty_model_set_refused():
    connection = SimpleNamespace(dialect="postgresql", transaction=Mock())

    with pytest.raises(ValueError, match="model"):
        execute_postgres_init(connection, [], approval="a" * 64,
                              expected_approval="a" * 64)

    connection.transaction.assert_not_called()


class FakePostgres:
    dialect = "postgresql"

    def __init__(self):
        self.events = []
        self._transaction_active = False

    def _ensure_open(self):
        pass

    @contextmanager
    def transaction(self):
        self._transaction_active = True
        self.events.append("begin")

        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self._transaction_active = False

    def fetch_first(self, sql, params=None):
        return {"acquired": True}

    def execute_write(self, sql, params=None):
        self.events.append((sql, params))
        return 1


def _prepare_native_postgres(monkeypatch):
    monkeypatch.setattr(cli_init, "_preflight", lambda *_: {
        "status": "EMPTY", "review_warnings": []})
    monkeypatch.setattr(postgres_init, "inspect_table", lambda *_:
                        SimpleNamespace(exists=False))
    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_:
                        SimpleNamespace(exists=False))
    monkeypatch.setattr(postgres_init, "initialize_history", lambda *_ , **__: None)
    monkeypatch.setattr(postgres_init, "verify_server_initial_schema", lambda *_: "b" * 64)


def test_postgres_native_initial_is_prepared_and_applied_in_one_transaction(monkeypatch):
    connection = FakePostgres()
    _prepare_native_postgres(monkeypatch)
    monkeypatch.setattr(postgres_init, "execute_create", lambda conn, model, token, confirm:
                        conn.events.append("application DDL"))

    assert len(execute_postgres_init(connection, (Entry,), approval="a" * 64,
                                     expected_approval="a" * 64)) == 1
    events = connection.events
    prepared = next(i for i, item in enumerate(events) if isinstance(item, tuple)
                    and "aurorasql_migration_timeline" in item[0] and "INSERT" in item[0])
    completed = next(i for i, item in enumerate(events) if isinstance(item, tuple)
                     and "aurorasql_migration_timeline" in item[0] and "UPDATE" in item[0])

    assert events[0] == "begin"
    assert prepared < events.index("application DDL") < completed
    assert events[-1] == "commit"


def test_postgres_native_initial_rolls_back_on_postcondition_failure(monkeypatch):
    connection = FakePostgres()
    _prepare_native_postgres(monkeypatch)
    monkeypatch.setattr(postgres_init, "execute_create", lambda *_ , **__:
                        connection.events.append("application DDL"))
    monkeypatch.setattr(postgres_init, "verify_server_initial_schema", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("catalog mismatch")))

    with pytest.raises(RuntimeError, match="catalog mismatch"):
        execute_postgres_init(connection, (Entry,), approval="a" * 64,
                              expected_approval="a" * 64)

    assert "rollback" in connection.events
    assert "commit" not in connection.events
