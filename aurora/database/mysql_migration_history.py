"""Explicit MySQL migration history primitives (no DDL execution).

A history row is evidence, not authorization. MySQL DDL and history inserts
are NOT atomic; callers must independently verify schema after DDL. This
module never creates history automatically when reading or recording.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from aurora.database.mysql_execution_approval import MySQLApprovalRequest

_HISTORY_TABLE = "aurora_mysql_migration_history"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CREATE = """CREATE TABLE aurora_mysql_migration_history (
    review_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    operation_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    sql_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    schema_name VARCHAR(64) NOT NULL,
    table_name VARCHAR(64) NOT NULL,
    applied_at_utc VARCHAR(32) NOT NULL
) ENGINE=InnoDB"""
_SELECT = """SELECT review_sha256, operation_sha256, sql_sha256,
 schema_name, table_name, applied_at_utc FROM aurora_mysql_migration_history
 WHERE review_sha256 = :review_sha256"""
_INSERT = """INSERT INTO aurora_mysql_migration_history
 (review_sha256, operation_sha256, sql_sha256, schema_name, table_name, applied_at_utc)
 VALUES (:review_sha256, :operation_sha256, :sql_sha256, :schema_name,
 :table_name, :applied_at_utc)"""


@dataclass(frozen=True)
class MySQLHistoryRecord:
    review_sha256: str
    operation_sha256: str
    sql_sha256: str
    schema_name: str
    table_name: str
    applied_at_utc: str

    def __post_init__(self):
        for name in ("review_sha256", "operation_sha256", "sql_sha256"):
            if not isinstance(getattr(self, name), str) or not _SHA.fullmatch(getattr(self, name)):
                raise ValueError(f"Invalid {name}")
        for name in ("schema_name", "table_name", "applied_at_utc"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"Invalid {name}")


def _require_mysql(connection: Connection) -> None:
    if not isinstance(connection, Connection) or connection.dialect.name != "mysql":
        raise ValueError("An explicit SQLAlchemy MySQL connection is required")


def history_table_exists(connection: Connection) -> bool:
    """Read-only check; absence must never cause implicit creation."""
    _require_mysql(connection)
    return inspect(connection).has_table(_HISTORY_TABLE)


def initialize_mysql_history(connection: Connection, *, confirm: bool = False) -> None:
    """Explicit DDL for a new history table; caller owns schema isolation.

    Does not commit or retry. MySQL CREATE TABLE may implicitly commit.
    """
    _require_mysql(connection)
    if confirm is not True:
        raise PermissionError("Explicit history initialization confirmation required")
    if history_table_exists(connection):
        raise FileExistsError("MySQL migration history table already exists")
    connection.exec_driver_sql(_CREATE)


def read_mysql_history(connection: Connection, review_sha256: str) -> MySQLHistoryRecord | None:
    _require_mysql(connection)
    if not isinstance(review_sha256, str) or not _SHA.fullmatch(review_sha256):
        raise ValueError("Invalid review digest")
    if not history_table_exists(connection):
        raise FileNotFoundError("MySQL migration history has not been initialized")
    rows = connection.execute(text(_SELECT), {"review_sha256": review_sha256}).all()
    if len(rows) > 1:
        raise RuntimeError("Duplicate history evidence")
    return MySQLHistoryRecord(*rows[0]) if rows else None


def append_verified_mysql_history(connection: Connection, *, request: MySQLApprovalRequest,
                                  sql_sha256: str, schema_verified: bool,
                                  confirm: bool = False) -> MySQLHistoryRecord:
    """Record independently verified DDL; never infer success or auto-repair.

    Caller must hold the migration advisory lock and independently verify the
    exact reviewed schema. The connection's transaction/commit is caller-owned.
    This function cannot prove the caller's verification was correct.
    """
    _require_mysql(connection)
    if confirm is not True or schema_verified is not True:
        raise PermissionError("Explicit confirmation and verified schema required")
    if not isinstance(request, MySQLApprovalRequest):
        raise TypeError("Validated approval request required")
    if sql_sha256 != request.model_schema_sha256:
        raise PermissionError("SQL digest differs from reviewed model")
    if not history_table_exists(connection):
        raise FileNotFoundError("MySQL migration history has not been initialized")
    review = request.review_sha256()
    if read_mysql_history(connection, review) is not None:
        raise FileExistsError("Migration review already recorded")
    op = request.operation
    record = MySQLHistoryRecord(
        review_sha256=review,
        operation_sha256=op.identity_sha256(),
        sql_sha256=sql_sha256,
        schema_name=op.schema_name,
        table_name=op.table_name,
        applied_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    connection.execute(text(_INSERT), vars(record))
    return record
