"""Offline tests for the CREATE TABLE + migration history execution boundary."""
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.engine import make_url

from aurora.database.mysql_create_table_executor import (
    execute_mysql_create_table,
    prepare_mysql_create_table,
)
from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_preflight import MySQLSchemaSnapshot


def _setup():
    operation = MySQLOperationContract(
        "main", "scratch", "d4_mysql_init_probe",
        "tests.fixtures.mysql_init_probe:MySQLInitProbe",
    )
    reviewed = MySQLSchemaSnapshot("main", "scratch", (operation.table_name,), ())
    sql, digest = prepare_mysql_create_table(operation)
    request = MySQLApprovalRequest(operation, digest, reviewed.fingerprint())
    config = MagicMock()
    config.resolve.return_value = make_url("mysql+pymysql://u:p@localhost/scratch")
    connection = MagicMock()
    connection.execute.return_value.scalar_one.side_effect = ["scratch", 1, "scratch", 1]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    return config, request, reviewed, digest, engine, connection


def _execute(config, request, reviewed, digest):
    return execute_mysql_create_table(
        config, request=request, reviewed=reviewed,
        approved_review_sha256=request.review_sha256(),
        approved_sql_sha256=digest, confirm=True,
    )


def test_success_records_history_and_releases_lock():
    config, request, reviewed, digest, engine, connection = _setup()
    with (patch("aurora.database.mysql_create_table_executor.create_engine", return_value=engine),
          patch("aurora.database.mysql_create_table_executor.inspect") as inspector,
          patch("aurora.database.mysql_create_table_executor.require_clean_mysql_history_before_ddl") as guard,
          patch("aurora.database.mysql_create_table_executor.append_verified_mysql_history") as append,
          patch("aurora.database.mysql_create_table_executor.read_mysql_history") as read):
        read.return_value = MagicMock(review_sha256=request.review_sha256(), operation_sha256=request.operation.identity_sha256(), sql_sha256=digest, schema_name="scratch", table_name=request.operation.table_name)
        inspector.return_value.get_table_names.side_effect = [[], [request.operation.table_name]]
        result = _execute(config, request, reviewed, digest)
    assert result.status == "APPLIED_HISTORY_RECORDED"
    guard.assert_called_once()
    append.assert_called_once()
    connection.exec_driver_sql.assert_called_once()
    connection.commit.assert_called_once()
    assert connection.execute.call_count == 4
    engine.dispose.assert_called_once()


def test_history_guard_refuses_before_ddl():
    config, request, reviewed, digest, engine, connection = _setup()
    with (patch("aurora.database.mysql_create_table_executor.create_engine", return_value=engine),
          patch("aurora.database.mysql_create_table_executor.inspect") as inspector,
          patch("aurora.database.mysql_create_table_executor.require_clean_mysql_history_before_ddl", side_effect=FileNotFoundError("history absent"))):
        inspector.return_value.get_table_names.return_value = []
        with pytest.raises(RuntimeError, match="PRECHECK_FAILED"):
            _execute(config, request, reviewed, digest)
    connection.exec_driver_sql.assert_not_called()
    connection.commit.assert_not_called()


def test_history_write_failure_is_uncertain():
    config, request, reviewed, digest, engine, connection = _setup()
    with (patch("aurora.database.mysql_create_table_executor.create_engine", return_value=engine),
          patch("aurora.database.mysql_create_table_executor.inspect") as inspector,
          patch("aurora.database.mysql_create_table_executor.require_clean_mysql_history_before_ddl"),
          patch("aurora.database.mysql_create_table_executor.append_verified_mysql_history", side_effect=RuntimeError("insert failed"))):
        inspector.return_value.get_table_names.side_effect = [[], [request.operation.table_name]]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(config, request, reviewed, digest)
    connection.exec_driver_sql.assert_called_once()
    connection.commit.assert_not_called()


def test_lock_release_failure_after_commit_is_uncertain():
    config, request, reviewed, digest, engine, connection = _setup()
    connection.execute.return_value.scalar_one.side_effect = ["scratch", 1, "scratch", 0]
    with (patch("aurora.database.mysql_create_table_executor.create_engine", return_value=engine),
          patch("aurora.database.mysql_create_table_executor.inspect") as inspector,
          patch("aurora.database.mysql_create_table_executor.require_clean_mysql_history_before_ddl"),
          patch("aurora.database.mysql_create_table_executor.append_verified_mysql_history"),
          patch("aurora.database.mysql_create_table_executor.read_mysql_history") as read):
        read.return_value = MagicMock(review_sha256=request.review_sha256(), operation_sha256=request.operation.identity_sha256(), sql_sha256=digest, schema_name="scratch", table_name=request.operation.table_name)
        inspector.return_value.get_table_names.side_effect = [[], [request.operation.table_name]]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(config, request, reviewed, digest)
    connection.commit.assert_called_once()


def test_history_commit_without_matching_persisted_record_is_uncertain():
    config, request, reviewed, digest, engine, connection = _setup()
    with (patch("aurora.database.mysql_create_table_executor.create_engine", return_value=engine),
          patch("aurora.database.mysql_create_table_executor.inspect") as inspector,
          patch("aurora.database.mysql_create_table_executor.require_clean_mysql_history_before_ddl"),
          patch("aurora.database.mysql_create_table_executor.append_verified_mysql_history"),
          patch("aurora.database.mysql_create_table_executor.read_mysql_history", return_value=None)):
        inspector.return_value.get_table_names.side_effect = [[], [request.operation.table_name]]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(config, request, reviewed, digest)
    connection.commit.assert_called_once()
    connection.exec_driver_sql.assert_called_once()


def test_history_read_failure_after_commit_is_uncertain():
    config, request, reviewed, digest, engine, connection = _setup()
    with (patch("aurora.database.mysql_create_table_executor.create_engine", return_value=engine),
          patch("aurora.database.mysql_create_table_executor.inspect") as inspector,
          patch("aurora.database.mysql_create_table_executor.require_clean_mysql_history_before_ddl"),
          patch("aurora.database.mysql_create_table_executor.append_verified_mysql_history"),
          patch("aurora.database.mysql_create_table_executor.read_mysql_history", side_effect=OSError("lost"))):
        inspector.return_value.get_table_names.side_effect = [[], [request.operation.table_name]]
        with pytest.raises(RuntimeError, match="OUTCOME_UNKNOWN"):
            _execute(config, request, reviewed, digest)
    connection.commit.assert_called_once()
