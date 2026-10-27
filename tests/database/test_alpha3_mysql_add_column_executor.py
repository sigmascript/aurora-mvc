from sqlalchemy.dialects.mysql import VARCHAR
"""Offline guard tests for the explicit nullable ADD COLUMN executor."""
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.engine import Connection

from aurora.database.mysql_add_column_executor import execute_mysql_nullable_add_column
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal, prepare_mysql_nullable_add_column
from aurora.database.mysql_add_column_preflight import MySQLAddColumnPreflight


def _setup():
    proposal = MySQLAddColumnProposal(
        "main", "sandbox", "a3_nullable_column_probe",
        "tests.fixtures.mysql_add_column_probe:MySQLAddColumnProbe", "description",
    )
    sql, digest = prepare_mysql_nullable_add_column(proposal)
    reviewed = MySQLAddColumnPreflight(sql, digest, "sandbox", proposal.table_name, "description", ("id", "title"))
    connection = Mock(spec=Connection)
    connection.dialect = Mock(name="dialect")
    connection.dialect.name = "mysql"
    connection.execute.return_value.scalar_one.return_value = 1
    return connection, proposal, reviewed, digest


def _execute(connection, proposal, reviewed, digest, **kwargs):
    return execute_mysql_nullable_add_column(
        connection, proposal=proposal, reviewed=reviewed, approved_sql_sha256=digest, **kwargs
    )


def test_default_refuses_all_writes():
    conn, proposal, reviewed, digest = _setup()
    with pytest.raises(PermissionError, match="confirmation"):
        _execute(conn, proposal, reviewed, digest)
    conn.execute.assert_not_called()
    conn.exec_driver_sql.assert_not_called()


def test_mismatched_digest_refuses_lock_and_ddl():
    conn, proposal, reviewed, _ = _setup()
    with pytest.raises(PermissionError, match="approved"):
        _execute(conn, proposal, reviewed, "0" * 64, confirm=True)
    conn.execute.assert_not_called()
    conn.exec_driver_sql.assert_not_called()


def test_failed_lock_refuses_ddl():
    conn, proposal, reviewed, digest = _setup()
    conn.execute.return_value.scalar_one.return_value = 0
    with pytest.raises(RuntimeError, match="acquire"):
        _execute(conn, proposal, reviewed, digest, confirm=True)
    conn.exec_driver_sql.assert_not_called()


def test_stale_schema_refuses_ddl_and_releases_lock():
    conn, proposal, reviewed, digest = _setup()
    changed = MySQLAddColumnPreflight(reviewed.sql, digest, "sandbox", proposal.table_name, "description", ("id", "new"))
    with patch("aurora.database.mysql_add_column_executor.inspect_mysql_nullable_add_column", return_value=changed):
        with pytest.raises(RuntimeError, match="changed"):
            _execute(conn, proposal, reviewed, digest, confirm=True)
    conn.exec_driver_sql.assert_not_called()
    assert conn.execute.call_count == 2


def test_acknowledged_ddl_requires_schema_verification():
    conn, proposal, reviewed, digest = _setup()
    with patch("aurora.database.mysql_add_column_executor.inspect_mysql_nullable_add_column", return_value=reviewed), patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [{"name": "description", "type": VARCHAR(120), "nullable": True}]
        result = _execute(conn, proposal, reviewed, digest, confirm=True)
    assert result.status == "DDL_APPLIED_HISTORY_UNCONFIRMED"
    conn.exec_driver_sql.assert_called_once_with(reviewed.sql)
    assert conn.execute.call_count == 2


def test_failed_ddl_reports_uncertainty_without_retry():
    conn, proposal, reviewed, digest = _setup()
    conn.exec_driver_sql.side_effect = OSError("connection lost")
    with patch("aurora.database.mysql_add_column_executor.inspect_mysql_nullable_add_column", return_value=reviewed):
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(conn, proposal, reviewed, digest, confirm=True)
    conn.exec_driver_sql.assert_called_once()


def test_missing_column_after_ddl_is_uncertain():
    conn, proposal, reviewed, digest = _setup()
    with patch("aurora.database.mysql_add_column_executor.inspect_mysql_nullable_add_column", return_value=reviewed), patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [{"name": "id"}]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(conn, proposal, reviewed, digest, confirm=True)
    conn.exec_driver_sql.assert_called_once()


def test_lock_release_failure_after_ddl_is_uncertain():
    conn, proposal, reviewed, digest = _setup()
    conn.execute.return_value.scalar_one.side_effect = [1, 0]
    with patch("aurora.database.mysql_add_column_executor.inspect_mysql_nullable_add_column", return_value=reviewed), patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [{"name": "description", "type": VARCHAR(120), "nullable": True}]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(conn, proposal, reviewed, digest, confirm=True)
    conn.exec_driver_sql.assert_called_once()
