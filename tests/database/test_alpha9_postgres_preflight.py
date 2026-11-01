"""PostgreSQL read-only preflight: no live server required for contract tests."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aurora.database import cli_init


def _connection(dialect="postgresql"):
    raw = Mock()
    connection = SimpleNamespace(dialect=dialect, _raw=raw,
                                 _transaction_active=False, _ensure_open=Mock())
    return connection


def test_read_only_snapshot_is_opened_and_closed(monkeypatch):
    connection = _connection()
    seen = []

    def preflight(conn, models, alias):
        seen.append((conn, models, alias))
        return {"status": "EMPTY", "read_only": True}

    monkeypatch.setattr(cli_init, "_preflight", preflight)
    report = cli_init._postgres_read_only_preflight(connection, [object()], "analytics")
    assert report["transaction_mode"] == "REPEATABLE READ, READ ONLY"
    assert report["write_enabled"] is False
    assert seen[0][0] is connection and seen[0][2] == "analytics"
    connection._raw.execute.assert_called_once_with(
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    connection._raw.rollback.assert_called_once_with()


def test_read_only_snapshot_rolls_back_on_inspection_failure(monkeypatch):
    connection = _connection()

    def fail(*args):
        raise RuntimeError("catalog query failed")

    monkeypatch.setattr(cli_init, "_preflight", fail)
    with pytest.raises(RuntimeError, match="catalog query failed"):
        cli_init._postgres_read_only_preflight(connection, [], "default")
    connection._raw.rollback.assert_called_once_with()


def test_rejects_wrong_dialect_without_begin():
    connection = _connection("mysql")
    with pytest.raises(ValueError, match="PostgreSQL"):
        cli_init._postgres_read_only_preflight(connection, [], "default")
    connection._raw.execute.assert_not_called()


def test_rejects_active_transaction_without_begin():
    connection = _connection()
    connection._transaction_active = True
    with pytest.raises(RuntimeError, match="active"):
        cli_init._postgres_read_only_preflight(connection, [], "default")
    connection._raw.execute.assert_not_called()
