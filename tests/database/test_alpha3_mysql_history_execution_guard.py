"""Offline tests for fail-closed MySQL pre-execution history inspection."""
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine

from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_history_execution_guard import require_clean_mysql_history_before_ddl
from aurora.database.mysql_migration_contract import MySQLOperationContract


def _request():
    operation = MySQLOperationContract("default", "aurora_alpha3_migration_test", "notes", "demo.models:Note")
    return MySQLApprovalRequest(operation, "a" * 64, "b" * 64)


def _connection(rows=(), schema="aurora_alpha3_migration_test"):
    connection = Mock()
    connection.dialect.name = "mysql"
    connection.execute.side_effect = [Mock(scalar_one=Mock(return_value=schema)), Mock(all=Mock(return_value=rows))]
    return connection


def _run(connection, request=None, **kwargs):
    request = request or _request()
    if isinstance(connection, Mock):
        with patch("aurora.database.mysql_history_execution_guard.Connection", Mock):
            return require_clean_mysql_history_before_ddl(
                connection, request=request, sql_sha256=kwargs.get("digest", request.model_schema_sha256),
                advisory_lock_held=kwargs.get("locked", True),
            )
    return require_clean_mysql_history_before_ddl(
        connection, request=request, sql_sha256=kwargs.get("digest", request.model_schema_sha256),
        advisory_lock_held=kwargs.get("locked", True),
    )


def test_rejects_non_mysql_connection():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            with pytest.raises(ValueError, match="MySQL connection"):
                _run(connection)
    finally:
        engine.dispose()


def test_rejects_missing_lock_before_query():
    connection = _connection()
    with pytest.raises(PermissionError, match="lock"):
        _run(connection, locked=False)
    connection.execute.assert_not_called()


def test_rejects_unapproved_digest_before_query():
    connection = _connection()
    with pytest.raises(PermissionError, match="SQL"):
        _run(connection, digest="c" * 64)
    connection.execute.assert_not_called()


def test_rejects_wrong_schema():
    connection = _connection(schema="production")
    with pytest.raises(RuntimeError, match="schema"):
        _run(connection)
    assert connection.execute.call_count == 1


def test_rejects_missing_history_table():
    connection = _connection()
    with patch("aurora.database.mysql_history_execution_guard.history_table_exists", return_value=False):
        with pytest.raises(FileNotFoundError, match="explicitly"):
            _run(connection)
    assert connection.execute.call_count == 1


def test_accepts_empty_initialized_history():
    connection = _connection()
    with patch("aurora.database.mysql_history_execution_guard.history_table_exists", return_value=True):
        result = _run(connection)
    assert result.existing_records == 0
    assert result.review_sha256 == _request().review_sha256()
    assert connection.execute.call_count == 2


def test_rejects_replayed_review():
    request = _request()
    row = (request.review_sha256(), "c" * 64, "a" * 64, request.operation.schema_name, "notes")
    with patch("aurora.database.mysql_history_execution_guard.history_table_exists", return_value=True):
        with pytest.raises(FileExistsError, match="already recorded"):
            _run(_connection([row]), request)


def test_rejects_same_operation_different_review():
    request = _request()
    row = ("c" * 64, request.operation.identity_sha256(), "a" * 64, request.operation.schema_name, "notes")
    with patch("aurora.database.mysql_history_execution_guard.history_table_exists", return_value=True):
        with pytest.raises(RuntimeError, match="Operation identity"):
            _run(_connection([row]), request)


def test_rejects_foreign_schema_record():
    row = ("c" * 64, "d" * 64, "a" * 64, "production", "notes")
    with patch("aurora.database.mysql_history_execution_guard.history_table_exists", return_value=True):
        with pytest.raises(RuntimeError, match="different schema"):
            _run(_connection([row]))


def test_allows_unrelated_history():
    row = ("c" * 64, "d" * 64, "e" * 64, "aurora_alpha3_migration_test", "other")
    with patch("aurora.database.mysql_history_execution_guard.history_table_exists", return_value=True):
        result = _run(_connection([row]))
    assert result.existing_records == 1
