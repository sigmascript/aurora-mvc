"""Read-only MySQL nullable ADD COLUMN preflight.

This check is not execution approval. The executor repeats it while
holding its cooperative migration lock, immediately before DDL.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from aurora.database.mysql_add_column_review import (
    MySQLAddColumnProposal,
    prepare_mysql_nullable_add_column,
)


@dataclass(frozen=True)
class MySQLAddColumnPreflight:
    sql: str
    sql_sha256: str
    schema_name: str
    table_name: str
    column_name: str
    existing_columns: tuple[str, ...]


def inspect_mysql_nullable_add_column(
    connection: Connection,
    *,
    proposal: MySQLAddColumnProposal,
) -> MySQLAddColumnPreflight:
    """Inspect a selected MySQL schema without changing it.

    Table and column names are compared exactly. No DDL or history writes occur.
    This deliberately does not claim compatibility of existing model columns.
    """
    if not isinstance(connection, Connection) or connection.dialect.name != "mysql":
        raise ValueError("A SQLAlchemy MySQL connection is required")
    if not isinstance(proposal, MySQLAddColumnProposal):
        raise TypeError("Structured ADD COLUMN proposal required")

    sql, digest = prepare_mysql_nullable_add_column(proposal)
    selected = connection.execute(text("SELECT DATABASE()")).scalar_one()
    if selected != proposal.schema_name:
        raise RuntimeError("Selected MySQL schema does not match proposal")
    inspector = inspect(connection)
    if not inspector.has_table(proposal.table_name):
        raise RuntimeError("Target table does not exist")
    columns = tuple(sorted(column["name"] for column in inspector.get_columns(proposal.table_name)))
    if len(set(columns)) != len(columns):
        raise RuntimeError("Ambiguous existing column names")
    if proposal.column_name in columns:
        raise RuntimeError("Target column already exists; manual reconciliation required")
    return MySQLAddColumnPreflight(
        sql=sql,
        sql_sha256=digest,
        schema_name=selected,
        table_name=proposal.table_name,
        column_name=proposal.column_name,
        existing_columns=columns,
    )


def require_fresh_mysql_add_column_preflight(
    reviewed: MySQLAddColumnPreflight,
    current: MySQLAddColumnPreflight,
    *,
    advisory_lock_held: bool,
    approved_sql_sha256: str,
) -> None:
    """Reject stale proposals; passing is not authorization to execute."""
    if not isinstance(reviewed, MySQLAddColumnPreflight) or not isinstance(current, MySQLAddColumnPreflight):
        raise TypeError("Validated preflight snapshots required")
    if advisory_lock_held is not True:
        raise PermissionError("Migration advisory lock required")
    if reviewed != current:
        raise RuntimeError("MySQL schema or proposal changed; new review required")
    if approved_sql_sha256 != current.sql_sha256:
        raise PermissionError("Reviewed SQL digest differs")
