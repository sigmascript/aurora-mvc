"""Explicit, restricted MySQL nullable ADD COLUMN executor.

The Alpha 3 CLI uses this executor with explicit history approval and
post-commit verification. No DDL retries or implicit schema creation.
DDL can commit implicitly; uncertain outcomes require manual reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib

from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

from sqlalchemy import text
from sqlalchemy.engine import Connection

from aurora.database.mysql_add_column_preflight import (
    MySQLAddColumnPreflight,
    inspect_mysql_nullable_add_column,
    require_fresh_mysql_add_column_preflight,
)
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal, prepare_mysql_nullable_add_column
from aurora.database.mysql_add_column_history import (
    MySQLAddColumnHistoryReview,
    append_verified_mysql_add_column_history,
    prepare_mysql_add_column_history_review,
    read_mysql_add_column_history,
    require_clean_mysql_add_column_history,
)


def _verify_applied_column_definition(connection: Connection, proposal: MySQLAddColumnProposal) -> None:
    """Verify reflected type and nullability before recording migration history.

    Any unavailable or ambiguous reflection is treated as an uncertain outcome.
    """
    module_name, class_name = proposal.model_reference.split(":")
    model = getattr(importlib.import_module(module_name), class_name)
    expected = model.__sql_table__.c[proposal.column_name]
    columns = inspect(connection).get_columns(proposal.table_name, schema=proposal.schema_name)
    matches = [column for column in columns if column["name"] == proposal.column_name]
    if len(matches) != 1:
        raise RuntimeError("Added column absent or ambiguous after acknowledged DDL")
    actual = matches[0]
    expected_type = expected.type.compile(dialect=mysql.dialect()).upper()
    actual_type = actual["type"].compile(dialect=mysql.dialect()).upper()
    # MySQL reflection may report the inherited column collation even when
    # the model did not explicitly request one. This is not a type mismatch.
    # Keep explicit collation, type, and length requirements strict.
    expected_collation = getattr(expected.type, "collation", None)
    reflected_type = actual["type"]
    reflected_collation = getattr(reflected_type, "collation", None)
    if (
        expected_collation is None
        and reflected_collation is not None
        and isinstance(reflected_type, mysql.VARCHAR)
        and expected_type.startswith("VARCHAR(")
    ):
        suffix = " COLLATE " + reflected_collation.upper()
        if actual_type.endswith(suffix):
            actual_type = actual_type[:-len(suffix)]

    if actual_type != expected_type or actual["nullable"] is not True:
        raise RuntimeError("Added column definition differs from approved model")


@dataclass(frozen=True)
class MySQLAddColumnExecutionResult:
    status: str
    table_name: str
    column_name: str
    detail: str


def execute_mysql_nullable_add_column(
    connection: Connection,
    *,
    proposal: MySQLAddColumnProposal,
    reviewed: MySQLAddColumnPreflight,
    approved_sql_sha256: str,
    confirm: bool = False,
    history_review: MySQLAddColumnHistoryReview | None = None,
    approved_history_sha256: str | None = None,
) -> MySQLAddColumnExecutionResult:
    """Execute one operator-confirmed DDL with a fresh schema check under a lock.

    Caller supplies an isolated MySQL connection and owns its lifecycle. The
    history_review enables guarded, committed, post-verified history. Never retry.
    """
    if confirm is not True:
        raise PermissionError("Explicit ADD COLUMN confirmation required")
    if not isinstance(connection, Connection) or connection.dialect.name != "mysql":
        raise ValueError("An explicit SQLAlchemy MySQL connection is required")
    if not isinstance(proposal, MySQLAddColumnProposal) or not isinstance(reviewed, MySQLAddColumnPreflight):
        raise TypeError("Validated proposal and reviewed preflight required")
    sql, digest = prepare_mysql_nullable_add_column(proposal)
    if (digest != approved_sql_sha256 or reviewed.sql_sha256 != digest
            or reviewed.sql != sql or reviewed.schema_name != proposal.schema_name
            or reviewed.table_name != proposal.table_name or reviewed.column_name != proposal.column_name):
        raise PermissionError("SQL or proposal differs from approved review")

    if history_review is not None:
        if not isinstance(history_review, MySQLAddColumnHistoryReview):
            raise TypeError("Validated history review required")
        expected = prepare_mysql_add_column_history_review(
            proposal, existing_columns=reviewed.existing_columns
        )
        if (history_review != expected or approved_history_sha256 != expected.review_sha256
                or history_review.sql_sha256 != digest):
            raise PermissionError("ADD COLUMN history review differs from approved operation")
    elif approved_history_sha256 is not None:
        raise ValueError("History approval requires a history review")

    lock_hash = hashlib.sha256((proposal.schema_name + "\0" + proposal.database_alias).encode()).hexdigest()[:47]
    lock_name = "aurora:migration:" + lock_hash
    acquired = connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar_one()
    if acquired != 1:
        raise RuntimeError("Unable to acquire MySQL migration advisory lock")
    ddl_attempted = False
    outcome = None
    error = None
    try:
        current = inspect_mysql_nullable_add_column(connection, proposal=proposal)
        require_fresh_mysql_add_column_preflight(
            reviewed, current, advisory_lock_held=True, approved_sql_sha256=approved_sql_sha256
        )
        if history_review is not None:
            require_clean_mysql_add_column_history(
                connection, review=history_review, advisory_lock_held=True
            )
        ddl_attempted = True
        connection.exec_driver_sql(sql)
        # An acknowledged DDL response is insufficient: verify the complete
        # approved column definition before writing any history.
        _verify_applied_column_definition(connection, proposal)
        if history_review is not None:
            append_verified_mysql_add_column_history(
                connection, review=history_review, schema_verified=True,
                advisory_lock_held=True, confirm=True,
            )
            # MySQL DDL and history commit are not atomic. A failed commit or
            # post-commit read is an uncertain outcome, never a clean rollback.
            connection.commit()
            record = read_mysql_add_column_history(connection, history_review.review_sha256)
            if record != (
                history_review.review_sha256, history_review.operation_sha256,
                history_review.sql_sha256, proposal.schema_name,
                proposal.table_name, proposal.column_name,
            ):
                raise RuntimeError("Persisted ADD COLUMN history does not match review")
        outcome = MySQLAddColumnExecutionResult(
            "APPLIED_HISTORY_RECORDED" if history_review is not None else "DDL_APPLIED_HISTORY_UNCONFIRMED",
            proposal.table_name, proposal.column_name,
            "DDL and committed history verified" if history_review is not None
            else "Column observed after DDL; history must be recorded separately",
        )
    except Exception as exc:
        error = exc
    finally:
        try:
            released = connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}).scalar_one()
            if released != 1:
                raise RuntimeError("Advisory lock release unconfirmed")
        except Exception as exc:
            if ddl_attempted:
                raise RuntimeError("OUTCOME_UNKNOWN: DDL may have applied; inspect manually") from exc
            raise
    if error is not None:
        if ddl_attempted:
            raise RuntimeError("OUTCOME_UNKNOWN: DDL may have applied; inspect manually") from error
        raise error
    return outcome
