"""Offline tests for explicit MySQL history operations."""
from unittest.mock import Mock, patch
import pytest
from sqlalchemy import create_engine

from aurora.database.mysql_migration_history import (
    MySQLHistoryRecord, append_verified_mysql_history, history_table_exists,
    initialize_mysql_history, read_mysql_history,
)
from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_contract import MySQLOperationContract


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


def test_sqlite_cannot_use_mysql_history(connection):
    with pytest.raises(ValueError, match="MySQL connection"):
        history_table_exists(connection)
    with pytest.raises(ValueError, match="MySQL connection"):
        initialize_mysql_history(connection, confirm=True)


def test_record_rejects_bad_digest():
    with pytest.raises(ValueError, match="review_sha256"):
        MySQLHistoryRecord("bad", "a" * 64, "b" * 64, "schema", "table", "time")


def test_read_rejects_bad_digest_before_inspection():
    conn = Mock()
    conn.dialect.name = "mysql"
    with patch("aurora.database.mysql_migration_history._require_mysql"):
        with pytest.raises(ValueError, match="digest"):
            read_mysql_history(conn, "bad")


def test_initialization_requires_confirmation():
    conn = Mock()
    with patch("aurora.database.mysql_migration_history._require_mysql"):
        with pytest.raises(PermissionError):
            initialize_mysql_history(conn)
    conn.exec_driver_sql.assert_not_called()


def test_append_requires_independent_schema_verification():
    conn = Mock()
    with patch("aurora.database.mysql_migration_history._require_mysql"):
        with pytest.raises(PermissionError):
            append_verified_mysql_history(conn, request=None, sql_sha256="", schema_verified=False, confirm=True)
    conn.execute.assert_not_called()


def test_append_rejects_wrong_digest_before_write():
    conn = Mock()
    op = MySQLOperationContract("default", "disposable", "notes", "demo.models:Note")
    request = MySQLApprovalRequest(op, "a" * 64, "b" * 64)
    with patch("aurora.database.mysql_migration_history._require_mysql"):
        with pytest.raises(PermissionError, match="digest"):
            append_verified_mysql_history(conn, request=request, sql_sha256="c" * 64,
                                          schema_verified=True, confirm=True)
    conn.execute.assert_not_called()


def test_history_initialization_refuses_existing_table():
    conn = Mock()
    with patch("aurora.database.mysql_migration_history._require_mysql"), patch(
        "aurora.database.mysql_migration_history.history_table_exists", return_value=True
    ):
        with pytest.raises(FileExistsError):
            initialize_mysql_history(conn, confirm=True)
    conn.exec_driver_sql.assert_not_called()
