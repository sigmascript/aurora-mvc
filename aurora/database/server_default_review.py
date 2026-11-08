"""Read-only review of adding one validated database-side column default.

Removing or replacing an existing default requires separate historical proof.
This module never executes a database statement.
"""

from hashlib import sha256
import json

from .init_validation import validate_init_models
from .inspection import inspect_table
from .planning import _declared_type, _normalized_declaration, plan_schema
from .schema import _default_literal, _quote
from .server_model_review import ServerModelProposal


def review_server_default_addition(connection, models, alias, identity):
    """Propose one literal SQL DEFAULT from a provably default-free column."""
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql") or not alias or not identity:
        raise ValueError("A selected server database identity is required")

    ordered = validate_init_models(models, dialect=dialect)
    proposals = []

    for model in ordered:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if (not plan.exists or len(plan.changes) != 1
                or plan.changes[0].kind != "default_mismatch"):
            raise ValueError("Mixed or existing-default edits require separate review")

        name = plan.changes[0].column
        field = model.__columns__[name]
        table = inspect_table(connection, model.__table_name__)
        observed = {column.name: column for column in table.columns}
        expected_primary = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if (not table.exists or not table.uniqueness_verified or name not in observed
                or field.primary_key or field.unique or field.references is not None
                or field.server_default is not None or field.default is None
                or observed[name].primary_key or name in table.unique_columns
                or observed[name].default_sql is not None
                or observed[name].nullable != field.nullable
                or any(index != expected_primary for index in table.indexes)):
            raise ValueError("Existing/defaulted or constrained column requires manual review")

        if (_normalized_declaration(observed[name].sql_type, dialect)
                != _normalized_declaration(_declared_type(field, dialect), dialect)):
            raise ValueError("Default addition cannot change the column type")

        literal = _default_literal(field, dialect)

        if literal is None:
            raise ValueError("A validated SQL default literal is required")

        table_sql = _quote(model.__table_name__, dialect)
        column_sql = _quote(name, dialect)
        statement = f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} SET DEFAULT {literal}"
        proposals.append(({"kind": "add_default", "table": model.__table_name__,
                           "column": name}, statement))

    if len(proposals) != 1:
        raise ValueError("Exactly one verified SQL default addition is required")

    operations = (proposals[0][0],)
    statements = (proposals[0][1],)
    payload = json.dumps({"dialect": dialect, "alias": alias, "identity": identity,
                          "operations": operations, "statements": statements},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return ServerModelProposal(dialect, statements, operations,
                               sha256(payload.encode("utf-8")).hexdigest())
