"""Read-only, bounded review of one MySQL/PostgreSQL nullability change.

Review never grants execution permission. Both backend catalogs and historical
model declarations must be independently proven before any ALTER is enabled.
"""

from hashlib import sha256
import json

from .init_validation import validate_init_models
from .inspection import inspect_table
from .planning import _declared_type, _normalized_declaration, plan_schema
from .schema import _quote
from .server_model_review import ServerModelProposal


def review_server_nullability_change(connection, models, alias, identity):
    """Propose a single plain-column change; refuse unsafe non-null tightening."""
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
                or plan.changes[0].kind != "nullability_mismatch"):
            raise ValueError("Mixed or unsafe nullability changes require separate review")

        name = plan.changes[0].column
        field = model.__columns__[name]
        table = inspect_table(connection, model.__table_name__)
        actual = {column.name: column for column in table.columns}
        expected_primary = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if (not table.exists or not table.uniqueness_verified or name not in actual
                or field.primary_key or field.unique or field.references is not None
                or field.default is not None or field.server_default is not None
                or actual[name].primary_key or name in table.unique_columns
                or actual[name].default_sql is not None
                or any(index != expected_primary for index in table.indexes)):
            raise ValueError("Constrained or indexed nullability change is unsupported")

        declaration = _declared_type(field, dialect)

        if (_normalized_declaration(actual[name].sql_type, dialect)
                != _normalized_declaration(declaration, dialect)):
            raise ValueError("Nullability edit cannot change the column type")

        if actual[name].nullable == field.nullable:
            raise ValueError("No nullability change is present")

        table_sql = _quote(model.__table_name__, dialect)
        column_sql = _quote(name, dialect)

        if not field.nullable:
            # An indexed read can be used by the server, but the result must
            # remain bounded to one row. Recheck after taking the DDL lock.
            existing_null = connection.fetch_first(
                f"SELECT 1 AS found FROM {table_sql} WHERE {column_sql} IS NULL LIMIT 1")

            if existing_null is not None:
                raise ValueError("Existing NULL rows prevent a NOT NULL change")

        if dialect == "postgresql":
            action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
            statement = f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {action}"
        else:
            nullability = "NULL" if field.nullable else "NOT NULL"
            statement = f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} {declaration} {nullability}"

        proposals.append(({"kind": "change_nullability", "table": model.__table_name__,
                           "column": name, "from": actual[name].nullable,
                           "to": field.nullable}, statement))

    if len(proposals) != 1:
        raise ValueError("Exactly one verified nullability change is required")

    operations = (proposals[0][0],)
    statements = (proposals[0][1],)
    payload = json.dumps({"dialect": dialect, "alias": alias, "identity": identity,
                          "operations": operations, "statements": statements},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return ServerModelProposal(dialect, statements, operations,
                               sha256(payload.encode("utf-8")).hexdigest())
