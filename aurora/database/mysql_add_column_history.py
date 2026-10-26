"""Explicit, independent history for restricted MySQL nullable ADD COLUMN.

DDL and history are never atomic. No implicit initialization, reconciliation,
or retry is performed. This API is not wired into the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from aurora.database.mysql_add_column_review import (
    MySQLAddColumnProposal, prepare_mysql_nullable_add_column,
)

_TABLE = "aurora_mysql_add_column_history"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CREATE = """CREATE TABLE aurora_mysql_add_column_history (
    review_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    operation_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    sql_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    schema_name VARCHAR(64) NOT NULL,
    table_name VARCHAR(64) NOT NULL,
    column_name VARCHAR(64) NOT NULL
) ENGINE=InnoDB"""


@dataclass(frozen=True)
class MySQLAddColumnHistoryReview:
    proposal: MySQLAddColumnProposal
    sql_sha256: str
    existing_columns: tuple[str, ...]
    operation_sha256: str
    review_sha256: str


def prepare_mysql_add_column_history_review(
    proposal: MySQLAddColumnProposal, *, existing_columns: tuple[str, ...]
) -> MySQLAddColumnHistoryReview:
    """Bind exact SQL and prior columns into a non-authorizing review identity."""
    if not isinstance(proposal, MySQLAddColumnProposal):
        raise TypeError("Structured ADD COLUMN proposal required")
    if (not isinstance(existing_columns, tuple)
            or not all(isinstance(c, str) and c for c in existing_columns)
            or tuple(sorted(set(existing_columns))) != existing_columns
            or proposal.column_name in existing_columns):
        raise ValueError("Expected sorted unique existing columns without the new column")
    _, digest = prepare_mysql_nullable_add_column(proposal)
    operation = dict(database_alias=proposal.database_alias, schema_name=proposal.schema_name,
                     table_name=proposal.table_name, column_name=proposal.column_name,
                     model_reference=proposal.model_reference, kind="ADD_NULLABLE_COLUMN")
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    operation_sha = hashlib.sha256(canonical(operation)).hexdigest()
    review_sha = hashlib.sha256(canonical(dict(operation=operation, sql_sha256=digest,
                                                 existing_columns=existing_columns))).hexdigest()
    return MySQLAddColumnHistoryReview(proposal, digest, existing_columns, operation_sha, review_sha)


def _require_connection(connection: Connection, schema: str) -> None:
    if not isinstance(connection, Connection) or connection.dialect.name != "mysql":
        raise ValueError("Explicit MySQL connection required")
    if connection.execute(text("SELECT DATABASE()")).scalar_one() != schema:
        raise RuntimeError("Selected MySQL schema differs from reviewed target")


def mysql_add_column_history_exists(connection: Connection) -> bool:
    if not isinstance(connection, Connection) or connection.dialect.name != "mysql":
        raise ValueError("Explicit MySQL connection required")
    return inspect(connection).has_table(_TABLE)


def initialize_mysql_add_column_history(connection: Connection, *, schema_name: str,
                                        confirm: bool = False) -> None:
    """Explicit one-time DDL; never auto-initialize during migration execution."""
    if confirm is not True:
        raise PermissionError("Explicit history initialization confirmation required")
    _require_connection(connection, schema_name)
    if mysql_add_column_history_exists(connection):
        raise FileExistsError("ADD COLUMN history already exists")
    connection.exec_driver_sql(_CREATE)


def read_mysql_add_column_history(connection: Connection, review_sha256: str):
    if not isinstance(review_sha256, str) or not _SHA.fullmatch(review_sha256):
        raise ValueError("Invalid review digest")
    if not mysql_add_column_history_exists(connection):
        raise FileNotFoundError("ADD COLUMN history has not been initialized")
    rows = connection.execute(text(
        "SELECT review_sha256, operation_sha256, sql_sha256, schema_name, table_name, column_name "
        "FROM aurora_mysql_add_column_history WHERE review_sha256 = :review"
    ), {"review": review_sha256}).all()
    if len(rows) > 1:
        raise RuntimeError("Duplicate history records")
    return tuple(rows[0]) if rows else None


def require_clean_mysql_add_column_history(connection: Connection, *,
                                           review: MySQLAddColumnHistoryReview,
                                           advisory_lock_held: bool) -> None:
    """Read-only replay/conflict guard. Must run under the migration lock."""
    if advisory_lock_held is not True:
        raise PermissionError("Confirmed advisory lock required")
    if not isinstance(review, MySQLAddColumnHistoryReview):
        raise TypeError("Validated ADD COLUMN history review required")
    _require_connection(connection, review.proposal.schema_name)
    if not mysql_add_column_history_exists(connection):
        raise FileNotFoundError("Initialize ADD COLUMN history explicitly before DDL")
    rows = connection.execute(text(
        "SELECT review_sha256, operation_sha256, schema_name, table_name, column_name "
        "FROM aurora_mysql_add_column_history"
    )).all()
    for row in rows:
        if row[0] == review.review_sha256:
            raise FileExistsError("This review was already applied")
        if row[1] == review.operation_sha256:
            raise RuntimeError("Operation identity already recorded under another review")
        if row[2] != review.proposal.schema_name:
            raise RuntimeError("History contains a different schema")
        if row[3] == review.proposal.table_name and row[4] == review.proposal.column_name:
            raise RuntimeError("Target column already recorded under another operation")


def append_verified_mysql_add_column_history(connection: Connection, *,
                                             review: MySQLAddColumnHistoryReview,
                                             schema_verified: bool,
                                             advisory_lock_held: bool,
                                             confirm: bool = False) -> None:
    """Insert verified history in caller-owned transaction; does NOT commit."""
    if confirm is not True or schema_verified is not True:
        raise PermissionError("Confirmation and independent schema verification required")
    require_clean_mysql_add_column_history(connection, review=review,
                                           advisory_lock_held=advisory_lock_held)
    connection.execute(text(
        "INSERT INTO aurora_mysql_add_column_history "
        "(review_sha256, operation_sha256, sql_sha256, schema_name, table_name, column_name) "
        "VALUES (:review, :operation, :sql, :schema, :table, :column)"
    ), dict(review=review.review_sha256, operation=review.operation_sha256,
            sql=review.sql_sha256, schema=review.proposal.schema_name,
            table=review.proposal.table_name, column=review.proposal.column_name))
