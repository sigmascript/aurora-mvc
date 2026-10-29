"""Explicit, restricted MySQL CREATE TABLE executor used by the Alpha 3 CLI.

MySQL DDL may commit implicitly. A failed attempt is never auto-retried.
The caller must independently approve the exact review digest and invoke this
API explicitly. The history-aware path requires an explicitly initialized history table.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import re

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable

from aurora.database.model import Model
from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_history_execution_guard import require_clean_mysql_history_before_ddl
from aurora.database.mysql_migration_history import append_verified_mysql_history, read_mysql_history
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_preflight import MySQLSchemaSnapshot, validate_preflight_freshness

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class MySQLCreateTableResult:
    status: str
    table_name: str
    detail: str


def _trusted_table(operation: MySQLOperationContract):
    module, name = operation.model_reference.split(":")
    cls = getattr(importlib.import_module(module), name, None)
    if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
        raise ValueError("Reference does not identify an Aurora model")
    table = cls.__sql_table__
    if table.schema is not None or table.name != operation.table_name:
        raise ValueError("Model table does not match approved operation")
    if not _IDENTIFIER.fullmatch(table.name) or not table.columns:
        raise ValueError("Invalid or empty model table")
    # Reject cross-table dependencies and unsupported DDL extensions in M1B.
    if table.foreign_keys or table.constraints - {table.primary_key}:
        raise ValueError("Foreign keys and additional constraints are not supported")
    if table.indexes:
        raise ValueError("Indexes are not supported in restricted CREATE TABLE")
    for column in table.columns:
        if not _IDENTIFIER.fullmatch(column.name):
            raise ValueError("Invalid column identifier")
        if column.foreign_keys or column.computed is not None or column.identity is not None:
            raise ValueError("Foreign keys and generated columns are unsupported")
        if column.unique or column.default is not None:
            raise ValueError("Unique and Python defaults require manual review")
        if column.server_default is not None:
            raise ValueError("Server defaults require manual review")
    return table


def prepare_mysql_create_table(operation: MySQLOperationContract):
    """Return deterministic MySQL DDL and digest; never run SQL."""
    if not isinstance(operation, MySQLOperationContract):
        raise ValueError("Structured MySQL operation required")
    table = _trusted_table(operation)
    sql = str(CreateTable(table).compile(dialect=mysql.dialect()))
    if "CREATE TABLE" not in sql.upper() or "IF NOT EXISTS" in sql.upper():
        raise ValueError("Unexpected CREATE TABLE compilation")
    return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()


def execute_mysql_create_table(config, *, request: MySQLApprovalRequest,
                               reviewed: MySQLSchemaSnapshot, approved_review_sha256: str,
                               approved_sql_sha256: str, confirm: bool = False):
    """Execute one approved table creation with verified history; never retry DDL.

    Requires operator approval obtained out-of-band. A digest is a comparison
    value, not a credential. Schema state is rechecked under a cooperative lock.
    """
    if confirm is not True:
        raise PermissionError("Explicit operator confirmation is required")
    if not isinstance(request, MySQLApprovalRequest) or not isinstance(reviewed, MySQLSchemaSnapshot):
        raise ValueError("Validated request and reviewed snapshot required")
    operation = request.operation
    if request.review_sha256() != approved_review_sha256:
        raise PermissionError("Review approval does not match the operation")
    sql, digest = prepare_mysql_create_table(operation)
    if digest != approved_sql_sha256 or digest != request.model_schema_sha256:
        raise PermissionError("Compiled model DDL differs from reviewed DDL")
    if reviewed.fingerprint() != request.database_state_sha256:
        raise PermissionError("Reviewed schema fingerprint differs")
    if operation.database_alias != reviewed.database_alias or operation.schema_name != reviewed.schema_name:
        raise ValueError("Target does not match reviewed schema")
    url = config.resolve(operation.database_alias)
    if url.get_backend_name() != "mysql" or url.get_driver_name() != "pymysql":
        raise ValueError("Executor requires mysql+pymysql")

    lock_digest = hashlib.sha256((reviewed.schema_name + "\0" + reviewed.database_alias).encode()).hexdigest()[:47]
    lock_name = "aurora:migration:" + lock_digest
    engine = create_engine(url)
    ddl_attempted = False
    history_committed = False
    lock_released = False
    try:
        with engine.connect() as connection:
            schema = connection.execute(text("SELECT DATABASE()")).scalar_one()
            if schema != reviewed.schema_name:
                raise RuntimeError("Selected MySQL schema differs")
            acquired = connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar_one()
            if acquired != 1:
                raise RuntimeError("Unable to acquire migration advisory lock")
            try:
                actual = connection.execute(text("SELECT DATABASE()")).scalar_one()
                existing = tuple(sorted(inspect(connection).get_table_names()))
                current = MySQLSchemaSnapshot(operation.database_alias, actual, reviewed.selected_tables, existing)
                validate_preflight_freshness(
                    reviewed=reviewed, current=current, candidate=operation, advisory_lock_held=True
                )
                require_clean_mysql_history_before_ddl(
                    connection, request=request, sql_sha256=digest, advisory_lock_held=True
                )
                # An explicit history table must exist before DDL. No implicit
                # initialization, retry, or fallback to untracked execution.
                ddl_attempted = True
                connection.exec_driver_sql(sql)
                # Verify the actual table before claiming a successful operation.
                if operation.table_name not in inspect(connection).get_table_names():
                    raise RuntimeError("Created table absent after acknowledged DDL")
                append_verified_mysql_history(
                    connection, request=request, sql_sha256=digest,
                    schema_verified=True, confirm=True,
                )
                # MySQL DDL and this commit are not atomic. Commit failures are
                # uncertain outcomes, not rollback evidence.
                connection.commit()
                # Verify persisted evidence, not merely an acknowledged commit.
                # Re-reading through the connection is required before reporting
                # a completed migration; a failed read is an uncertain outcome.
                record = read_mysql_history(connection, request.review_sha256())
                if (record is None or record.review_sha256 != request.review_sha256()
                        or record.operation_sha256 != operation.identity_sha256()
                        or record.sql_sha256 != digest
                        or record.schema_name != operation.schema_name
                        or record.table_name != operation.table_name):
                    raise RuntimeError("Persisted MySQL migration history does not match approval")
                history_committed = True
            finally:
                try:
                    released = connection.execute(
                        text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}
                    ).scalar_one()
                    lock_released = released == 1
                except Exception:
                    lock_released = False
                if not lock_released:
                    if ddl_attempted:
                        raise RuntimeError(
                            "OUTCOME_UNKNOWN: advisory lock release unconfirmed; "
                            "inspect schema and history manually"
                        )
                    raise RuntimeError("Advisory lock release unconfirmed; no DDL attempted")
        if not history_committed:
            raise RuntimeError("OUTCOME_UNKNOWN: history commit not confirmed")
        return MySQLCreateTableResult(
            "APPLIED_HISTORY_RECORDED", operation.table_name,
            "DDL verified and migration history committed; advisory lock released",
        )
    except Exception as exc:
        if ddl_attempted:
            raise RuntimeError(
                "OUTCOME_UNKNOWN: DDL may have applied; inspect schema and history manually"
            ) from exc
        if isinstance(exc, (SQLAlchemyError, OSError)):
            raise RuntimeError("PRECHECK_FAILED: no DDL attempted") from exc
        raise
    finally:
        engine.dispose()
