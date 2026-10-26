from sqlalchemy.dialects.mysql import VARCHAR
"""Offline regression checks for guarded nullable ADD COLUMN history integration."""
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.engine import Connection

from aurora.database.mysql_add_column_executor import execute_mysql_nullable_add_column
from aurora.database.mysql_add_column_history import prepare_mysql_add_column_history_review
from aurora.database.mysql_add_column_preflight import MySQLAddColumnPreflight
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal, prepare_mysql_nullable_add_column


def setup():
    proposal = MySQLAddColumnProposal(
        "main", "sandbox", "a3_nullable_column_probe",
        "tests.fixtures.mysql_add_column_probe:MySQLAddColumnProbe", "description",
    )
    sql, digest = prepare_mysql_nullable_add_column(proposal)
    reviewed = MySQLAddColumnPreflight(sql, digest, "sandbox", proposal.table_name,
                                       "description", ("id", "title"))
    history = prepare_mysql_add_column_history_review(proposal, existing_columns=reviewed.existing_columns)
    conn = Mock(spec=Connection)
    conn.dialect = Mock()
    conn.dialect.name = "mysql"
    conn.execute.return_value.scalar_one.return_value = 1
    return conn, proposal, reviewed, digest, history


def invoke(conn, proposal, reviewed, digest, history, **overrides):
    params = dict(proposal=proposal, reviewed=reviewed, approved_sql_sha256=digest,
                  history_review=history, approved_history_sha256=history.review_sha256,
                  confirm=True)
    params.update(overrides)
    return execute_mysql_nullable_add_column(conn, **params)


def patches(reviewed):
    return (
        patch("aurora.database.mysql_add_column_executor.inspect_mysql_nullable_add_column", return_value=reviewed),
        patch("aurora.database.mysql_add_column_executor.inspect"),
        patch("aurora.database.mysql_add_column_executor.require_clean_mysql_add_column_history"),
        patch("aurora.database.mysql_add_column_executor.append_verified_mysql_add_column_history"),
        patch("aurora.database.mysql_add_column_executor.read_mysql_add_column_history"),
    )


def test_approval_mismatch_refuses_lock_and_ddl():
    conn, proposal, reviewed, digest, history = setup()
    with pytest.raises(PermissionError, match="history review"):
        invoke(conn, proposal, reviewed, digest, history, approved_history_sha256="0" * 64)
    conn.execute.assert_not_called()
    conn.exec_driver_sql.assert_not_called()


def test_missing_history_guard_refuses_ddl():
    conn, proposal, reviewed, digest, history = setup()
    pre, inspector, guard, append, read = patches(reviewed)
    with pre, inspector, guard as g, append, read:
        g.side_effect = FileNotFoundError("history not initialized")
        with pytest.raises(FileNotFoundError):
            invoke(conn, proposal, reviewed, digest, history)
    conn.exec_driver_sql.assert_not_called()


def test_verified_ddl_commits_and_checks_persisted_history():
    conn, proposal, reviewed, digest, history = setup()
    pre, inspector, guard, append, read = patches(reviewed)
    with pre, inspector as i, guard, append as a, read as r:
        i.return_value.get_columns.return_value = [{"name": "description", "type": VARCHAR(120), "nullable": True}]
        r.return_value = (history.review_sha256, history.operation_sha256,
                          history.sql_sha256, proposal.schema_name,
                          proposal.table_name, proposal.column_name)
        result = invoke(conn, proposal, reviewed, digest, history)
        a.assert_called_once()
        r.assert_called_once()
    assert result.status == "APPLIED_HISTORY_RECORDED"
    conn.commit.assert_called_once()
    conn.exec_driver_sql.assert_called_once_with(reviewed.sql)


def test_commit_failure_is_uncertain():
    conn, proposal, reviewed, digest, history = setup()
    conn.commit.side_effect = OSError("connection lost")
    pre, inspector, guard, append, read = patches(reviewed)
    with pre, inspector as i, guard, append, read:
        i.return_value.get_columns.return_value = [{"name": "description", "type": VARCHAR(120), "nullable": True}]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            invoke(conn, proposal, reviewed, digest, history)
    conn.exec_driver_sql.assert_called_once()


def test_persisted_history_mismatch_is_uncertain():
    conn, proposal, reviewed, digest, history = setup()
    pre, inspector, guard, append, read = patches(reviewed)
    with pre, inspector as i, guard, append, read as r:
        i.return_value.get_columns.return_value = [{"name": "description", "type": VARCHAR(120), "nullable": True}]
        r.return_value = None
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            invoke(conn, proposal, reviewed, digest, history)
    conn.commit.assert_called_once()


def test_pre_ddl_stale_review_refuses_without_history_write():
    conn, proposal, reviewed, digest, history = setup()
    pre, inspector, guard, append, read = patches(reviewed)
    with pre as p, inspector, guard, append as a, read:
        p.return_value = MySQLAddColumnPreflight(reviewed.sql, digest, "sandbox", proposal.table_name,
                                                 "description", ("id",))
        with pytest.raises(RuntimeError, match="changed"):
            invoke(conn, proposal, reviewed, digest, history)
        a.assert_not_called()
    conn.exec_driver_sql.assert_not_called()
