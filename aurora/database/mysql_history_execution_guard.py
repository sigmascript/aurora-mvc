"""Fail-closed MySQL history inspection before an approved DDL attempt.

This read-only guard does not approve DDL, initialize history, or reconcile
uncertain outcomes. Call while holding the cooperative migration lock.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_history import history_table_exists


@dataclass(frozen=True)
class MySQLHistoryExecutionGuard:
    review_sha256: str
    operation_sha256: str
    existing_records: int
    schema_name: str


def require_clean_mysql_history_before_ddl(
    connection: Connection,
    *,
    request: MySQLApprovalRequest,
    sql_sha256: str,
    advisory_lock_held: bool,
) -> MySQLHistoryExecutionGuard:
    """Refuse missing history, replay, conflicting identity or wrong schema.

    A successful return is only a prerequisite for execution, not permission.
    Existing unrelated migration history is permitted. The caller must still
    revalidate the complete schema and obtain explicit operator confirmation.
    """
    if not isinstance(connection, Connection) or connection.dialect.name != "mysql":
        raise ValueError("An explicit SQLAlchemy MySQL connection is required")
    if not isinstance(request, MySQLApprovalRequest):
        raise TypeError("Validated approval request required")
    if advisory_lock_held is not True:
        raise PermissionError("Confirmed migration advisory lock required")
    if sql_sha256 != request.model_schema_sha256:
        raise PermissionError("SQL differs from reviewed operation")

    schema = connection.execute(text("SELECT DATABASE()")).scalar_one()
    if schema != request.operation.schema_name:
        raise RuntimeError("Selected schema differs from approved operation")
    if not history_table_exists(connection):
        raise FileNotFoundError("Initialize MySQL migration history explicitly before DDL")

    # Read every row rather than checking only the review digest: a different
    # review can target the same operation identity after schema drift.
    rows = connection.execute(text(
        "SELECT review_sha256, operation_sha256, sql_sha256, schema_name, table_name "
        "FROM aurora_mysql_migration_history"
    )).all()
    review = request.review_sha256()
    operation = request.operation.identity_sha256()
    for row in rows:
        if row[0] == review:
            raise FileExistsError("Approved migration review already recorded; no retry")
        if row[1] == operation:
            raise RuntimeError("Operation identity already recorded under a different review")
        if row[3] != schema:
            raise RuntimeError("Migration history contains a different schema; manual review required")
    return MySQLHistoryExecutionGuard(review, operation, len(rows), schema)
