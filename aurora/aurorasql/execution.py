"""Explicit, create-only schema execution for the experimental AuroraSQL layer.

This is not a general migration engine. No existing table is altered or dropped.
"""

from dataclasses import dataclass
from hashlib import sha256

from .inspection import inspect_table
from .planning import plan_schema
from .schema import create_table_sql


@dataclass(frozen=True)
class CreateApproval:
    """One-time intent to create exactly the reviewed table definition."""

    table: str
    dialect: str
    fingerprint: str


def prepare_create(connection, model):
    """Review a missing table and return its exact create-only approval token."""
    plan = plan_schema(connection, model)
    if plan.exists or len(plan.changes) != 1 or plan.changes[0].kind != "create_table":
        raise ValueError("Only missing-table creation can be approved")
    sql = plan.create_sql_preview
    return CreateApproval(plan.table, connection.dialect, sha256(sql.encode("utf-8")).hexdigest())


def execute_create(connection, model, approval, *, confirm=False):
    """Create a reviewed missing table only with explicit confirmation.

    Never retries DDL. MySQL DDL can commit implicitly. An uncertain outcome
    must be investigated by inspecting the actual database before proceeding.
    """
    if not confirm:
        raise PermissionError("Explicit confirm=True is required for schema creation")
    if not isinstance(approval, CreateApproval):
        raise TypeError("Expected a CreateApproval token")
    sql = create_table_sql(model, connection.dialect)
    expected = CreateApproval(
        model.__table_name__, connection.dialect, sha256(sql.encode("utf-8")).hexdigest()
    )
    if approval != expected:
        raise ValueError("Approval does not match the model and database dialect")
    if inspect_table(connection, model.__table_name__).exists:
        raise RuntimeError("Table already exists; refusing to modify existing schema")
    # No IF NOT EXISTS: a concurrent creator must cause an explicit DB error.
    # No catch/retry: a failed or interrupted DDL may have an uncertain outcome.
    return connection.execute_write(sql)
