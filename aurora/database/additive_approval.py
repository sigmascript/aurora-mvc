"""Fail-closed, non-executable approval for one additive column proposal.

This module deliberately does not grant DDL authority. A later executor must
revalidate live schema and durable history under the migration lock.
"""

from dataclasses import dataclass
from hashlib import sha256
import json

from .migration_format import migration_plan, validate_migration_plan
from .migration_preflight import preflight_migration
from .schema import _quote


@dataclass(frozen=True)
class AddColumnReview:
    """Immutable review token for exactly one nullable column addition."""

    migration_id: str
    dialect: str
    table: str
    column: str
    plan_fingerprint: str
    sql_preview: str
    execution_allowed: bool = False


def _column_sql(field, dialect):
    """Render a deliberately restricted nullable-column declaration."""
    if (field["primary_key"] or not field["nullable"] or field["unique"]
            or field["references"] is not None or field["server_default"] is not None):
        raise ValueError("Only plain nullable columns without constraints are reviewable")
    kind = field["type"]
    length = field["length"]
    if kind == "str":
        sql_type = f"VARCHAR({length})" if length is not None else "TEXT"
    elif kind == "int":
        sql_type = "INTEGER" if dialect == "sqlite" else "BIGINT"
    elif kind == "float":
        sql_type = {"sqlite": "REAL", "mysql": "DOUBLE", "postgresql": "DOUBLE PRECISION"}[dialect]
    elif kind == "bool":
        sql_type = "INTEGER" if dialect == "sqlite" else "BOOLEAN"
    elif kind == "date":
        sql_type = "DATE"
    elif kind == "datetime":
        sql_type = "DATETIME" if dialect == "mysql" else "TIMESTAMP"
    else:
        raise ValueError("Unsupported column type")
    if kind != "str" and length is not None:
        raise ValueError("Unexpected length on non-string column")
    return sql_type


def review_add_column(connection, before, after, plan):
    """Review a single additive column change; do not authorize or execute DDL.

    The existing preflight verifies column presence but not all live column
    definitions. This function therefore must not be used as an executor gate.
    """
    validate_migration_plan(plan)
    expected = migration_plan(before, after, migration_id=plan["id"])
    if expected != plan:
        raise ValueError("Migration plan does not match supplied snapshots")
    if len(plan["operations"]) != 1:
        raise ValueError("Review requires exactly one operation")
    operation = plan["operations"][0]
    if operation["kind"] != "added_column" or operation["risk"] != "additive":
        raise ValueError("Only additive column changes can be reviewed")
    preflight = preflight_migration(connection, before, after, plan)
    blockers = [item for item in preflight.findings if not item.startswith("Read-only preflight:")]
    if blockers:
        raise RuntimeError("Migration preflight findings: " + "; ".join(blockers))
    table = next(item for item in after["tables"] if item["name"] == operation["table"])
    field = next(item for item in table["columns"] if item["name"] == operation["column"])
    sql_type = _column_sql(field, connection.dialect)
    sql = (f"ALTER TABLE {_quote(operation['table'], connection.dialect)} "
           f"ADD COLUMN {_quote(operation['column'], connection.dialect)} {sql_type}")
    serialized = json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return AddColumnReview(plan["id"], connection.dialect, operation["table"],
                           operation["column"], sha256(serialized.encode()).hexdigest(), sql)
