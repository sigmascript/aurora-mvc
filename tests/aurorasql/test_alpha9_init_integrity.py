"""Fail-closed post-DDL integrity verification for coordinated SQLite init."""

import sqlite3
from unittest.mock import patch

import pytest

from aurora.aurorasql import Model, column
from aurora.aurorasql.cli_init import _execute_batch
from aurora.aurorasql.core import Connection


class IntegrityParent(Model):
    __table__ = "integrity_parent"
    id = column(int, primary_key=True)


class IntegrityChild(Model):
    __table__ = "integrity_child"
    id = column(int, primary_key=True)
    parent_id = column(int, references="integrity_parent.id")


def _run(tmp_path, inspection):
    raw = sqlite3.connect(tmp_path / "integrity.sqlite3", isolation_level=None)
    with Connection(raw, "sqlite") as connection:
        with patch("aurora.aurorasql.cli_init._foreign_key_violations", side_effect=inspection):
            with pytest.raises(RuntimeError):
                _execute_batch(connection, (IntegrityParent, IntegrityChild))
        statuses = connection.fetch_all(
            "SELECT table_name, status FROM aurorasql_schema_history ORDER BY rowid")
        assert statuses == [
            {"table_name": "integrity_parent", "status": "uncertain"},
            {"table_name": "integrity_child", "status": "uncertain"},
        ]
        with pytest.raises(RuntimeError, match="Existing migration journal"):
            _execute_batch(connection, (IntegrityParent, IntegrityChild))


def test_foreign_key_violations_leave_uncertain_history(tmp_path):
    _run(tmp_path, lambda connection: [("integrity_child", 1, "integrity_parent", 0)])


def test_foreign_key_inspection_failure_leaves_uncertain_history(tmp_path):
    def unavailable(connection):
        raise RuntimeError("integrity inspection interrupted")
    _run(tmp_path, unavailable)
