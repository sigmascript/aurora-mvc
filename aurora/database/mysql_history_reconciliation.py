"""Read-only reconciliation of approved MySQL migration history and schema evidence.

This is an inspection boundary, not a migration executor or repair facility.
A matching history row alone never proves the schema matches the reviewed DDL.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Connection

from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_history import (
    MySQLHistoryRecord,
    history_table_exists,
    read_mysql_history,
)
from aurora.database.mysql_migration_recovery import (
    MySQLRecoveryEvidence,
    MySQLRecoveryState,
    classify_mysql_recovery,
)


@dataclass(frozen=True)
class MySQLHistoryReconciliation:
    """Read-only result; no state authorizes an automatic retry or repair."""

    state: MySQLRecoveryState
    history_record: MySQLHistoryRecord | None
    history_table_present: bool
    history_matches_review: bool | None


def reconcile_mysql_history(
    connection: Connection,
    *,
    request: MySQLApprovalRequest,
    sql_sha256: str,
    ddl_attempted: bool,
    schema_matches_review: bool | None,
    lock_release_confirmed: bool | None,
) -> MySQLHistoryReconciliation:
    """Compare stored history with the exact review identity, without writes.

    ``schema_matches_review`` is independent evidence from a trusted, fresh,
    complete schema comparison; callers must not substitute table existence.
    ``None`` means that comparison could not be completed.
    """
    if not isinstance(request, MySQLApprovalRequest):
        raise TypeError("Validated approval request required")
    if not isinstance(sql_sha256, str) or sql_sha256 != request.model_schema_sha256:
        raise ValueError("SQL digest differs from reviewed model")
    if type(ddl_attempted) is not bool:
        raise ValueError("ddl_attempted must be a boolean")
    for name, value in (("schema_matches_review", schema_matches_review),
                        ("lock_release_confirmed", lock_release_confirmed)):
        if value is not None and type(value) is not bool:
            raise ValueError(f"{name} must be a boolean or None")

    # Never initialize a history table as a side effect of inspection.
    present = history_table_exists(connection)
    record = read_mysql_history(connection, request.review_sha256()) if present else None
    match = None
    if record is not None:
        op = request.operation
        match = (
            record.review_sha256 == request.review_sha256()
            and record.operation_sha256 == op.identity_sha256()
            and record.sql_sha256 == sql_sha256
            and record.schema_name == op.schema_name
            and record.table_name == op.table_name
        )
    elif present:
        match = False

    # Missing history storage is unavailable evidence, not evidence of rollback.
    evidence = MySQLRecoveryEvidence(
        ddl_attempted=ddl_attempted,
        schema_matches_review=schema_matches_review,
        history_matches_review=match,
        lock_release_confirmed=lock_release_confirmed,
    )
    state = classify_mysql_recovery(evidence)
    return MySQLHistoryReconciliation(state, record, present, match)
