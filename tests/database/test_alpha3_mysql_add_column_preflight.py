"""Offline tests for the read-only MySQL ADD COLUMN preflight boundary."""
from __future__ import annotations

from unittest.mock import Mock, patch
from sqlalchemy.engine import Connection

import pytest

from aurora.database.mysql_add_column_preflight import (
    MySQLAddColumnPreflight,
    inspect_mysql_nullable_add_column,
    require_fresh_mysql_add_column_preflight,
)
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal


def _proposal():
    return MySQLAddColumnProposal("main", "sandbox", "a3_nullable_column_probe", "tests.fixtures.mysql_add_column_probe:MySQLAddColumnProbe", "description")


def _connection(schema="sandbox"):
    conn = Mock(spec=Connection)
    conn.dialect = Mock()
    conn.dialect.name = "mysql"
    conn.execute.return_value.scalar_one.return_value = schema
    return conn


def _inspector(*, table=True, columns=("id", "title")):
    inspector = Mock()
    inspector.has_table.return_value = table
    inspector.get_columns.return_value = [{"name": name} for name in columns]
    return inspector


def test_preflight_is_read_only():
    conn = _connection()
    inspector = _inspector()
    with patch("aurora.database.mysql_add_column_preflight.inspect", return_value=inspector):
        result = inspect_mysql_nullable_add_column(conn, proposal=_proposal())
    assert result.column_name == "description"
    assert result.existing_columns == ("id", "title")
    assert len(result.sql_sha256) == 64
    assert result.sql.startswith("ALTER TABLE")
    conn.exec_driver_sql.assert_not_called()
    assert conn.execute.call_count == 1


def test_wrong_schema_refused():
    with pytest.raises(RuntimeError, match="schema"):
        inspect_mysql_nullable_add_column(_connection("production"), proposal=_proposal())


def test_missing_table_refused():
    with patch("aurora.database.mysql_add_column_preflight.inspect", return_value=_inspector(table=False)):
        with pytest.raises(RuntimeError, match="does not exist"):
            inspect_mysql_nullable_add_column(_connection(), proposal=_proposal())


def test_existing_column_refused():
    with patch("aurora.database.mysql_add_column_preflight.inspect", return_value=_inspector(columns=("id", "description"))):
        with pytest.raises(RuntimeError, match="already exists"):
            inspect_mysql_nullable_add_column(_connection(), proposal=_proposal())


def test_duplicate_column_names_refused():
    with patch("aurora.database.mysql_add_column_preflight.inspect", return_value=_inspector(columns=("id", "id"))):
        with pytest.raises(RuntimeError, match="Ambiguous"):
            inspect_mysql_nullable_add_column(_connection(), proposal=_proposal())


def test_non_mysql_refused():
    conn = _connection()
    conn.dialect.name = "sqlite"
    with pytest.raises(ValueError, match="MySQL"):
        inspect_mysql_nullable_add_column(conn, proposal=_proposal())


def test_invalid_proposal_refused():
    with pytest.raises(TypeError, match="proposal"):
        inspect_mysql_nullable_add_column(_connection(), proposal=None)


def test_freshness_gate():
    reviewed = MySQLAddColumnPreflight("ALTER TABLE t ADD COLUMN c INT NULL", "a" * 64, "sandbox", "t", "c", ("id",))
    require_fresh_mysql_add_column_preflight(reviewed, reviewed, advisory_lock_held=True, approved_sql_sha256="a" * 64)
    with pytest.raises(PermissionError, match="lock"):
        require_fresh_mysql_add_column_preflight(reviewed, reviewed, advisory_lock_held=False, approved_sql_sha256="a" * 64)
    with pytest.raises(PermissionError, match="digest"):
        require_fresh_mysql_add_column_preflight(reviewed, reviewed, advisory_lock_held=True, approved_sql_sha256="b" * 64)
    changed = MySQLAddColumnPreflight(reviewed.sql, reviewed.sql_sha256, reviewed.schema_name, reviewed.table_name, reviewed.column_name, ("id", "other"))
    with pytest.raises(RuntimeError, match="changed"):
        require_fresh_mysql_add_column_preflight(reviewed, changed, advisory_lock_held=True, approved_sql_sha256="a" * 64)
