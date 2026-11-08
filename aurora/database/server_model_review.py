"""Read-only MySQL/PostgreSQL model-change eligibility review.

An eligible proposal is not execution authorization. Server-side DDL needs
separate history, backup, locking and postcondition guarantees.
"""

from dataclasses import dataclass
from hashlib import sha256
import json

from .inspection import inspect_table
from .init_validation import validate_init_models
from .planning import plan_schema
from .schema import _default_literal, _quote, create_table_sql


@dataclass(frozen=True)
class ServerModelProposal:
    dialect: str
    statements: tuple[str, ...]
    operations: tuple[dict, ...]
    review_sha256: str
    execution_allowed: bool = False


def _add_column_statement(model, field_name, dialect):
    """Permit only a simple nullable field without key dependencies."""
    from .schema import _SQL_TYPES

    field = model.__columns__[field_name]

    if (field.primary_key or field.unique or field.references is not None
            or not field.nullable or field.server_default is not None):
        raise ValueError("Column addition requires separate constraint or backfill review")

    if field.sql_type is not None:
        sql_type = field.sql_type
    elif field.python_type.__name__ == "date":
        sql_type = "DATE"
    elif field.python_type.__name__ == "datetime":
        sql_type = "DATETIME" if dialect == "mysql" else "TIMESTAMP"
    elif field.python_type is str and field.length is not None:
        sql_type = f"VARCHAR({field.length})"
    else:
        sql_type = _SQL_TYPES[dialect][field.python_type]

    default = _default_literal(field, dialect)
    declaration = f"{_quote(field_name, dialect)} {sql_type}"

    if default is not None:
        declaration += f" DEFAULT {default}"

    return f"ALTER TABLE {_quote(model.__table_name__, dialect)} ADD COLUMN {declaration}"


def review_server_model_changes(connection, models, alias, database_identity):
    """Describe one narrowly supported change without writing to the server.

    The caller must independently verify catalog identity and history before
    execution. Mixed changes are deliberately refused until dependency and
    intermediate-state checks are implemented.
    """
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Server model review requires MySQL or PostgreSQL")

    if (not isinstance(alias, str) or not alias or
            not isinstance(database_identity, str) or not database_identity):
        raise ValueError("Selected database identity and alias are required")

    ordered = validate_init_models(models, dialect=dialect)
    proposals = []

    for model in ordered:
        plan = plan_schema(connection, model)
        changes = plan.changes

        if not changes:
            continue

        if not plan.exists:
            if len(changes) != 1 or changes[0].kind != "create_table":
                raise ValueError("Unsupported missing-table schema change")

            statement = create_table_sql(model, dialect)
            proposals.append(({"kind": "create_table", "table": model.__table_name__}, statement))
            continue

        if len(changes) != 1 or changes[0].kind != "missing_column":
            raise ValueError("Server migration requires separate review of existing-column drift")

        # Index and constraint reflection is not complete enough to prove a
        # complex ALTER safe. Keep this narrow path for plain source tables.
        table = inspect_table(connection, model.__table_name__)

        expected_primary = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if not table.uniqueness_verified or table.indexes and any(
                index != expected_primary for index in table.indexes):
            raise ValueError("Indexed source table needs dedicated migration review")

        name = changes[0].column
        statement = _add_column_statement(model, name, dialect)
        proposals.append(({"kind": "add_column", "table": model.__table_name__, "column": name}, statement))

    if len(proposals) != 1:
        raise ValueError("Server migration requires exactly one independently reviewed change")

    operations = tuple(item[0] for item in proposals)
    statements = tuple(item[1] for item in proposals)
    payload = json.dumps({"dialect": dialect, "alias": alias, "identity": database_identity,
                          "operations": operations, "statements": statements},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)

    return ServerModelProposal(dialect, statements, operations,
                               sha256(payload.encode("utf-8")).hexdigest())
