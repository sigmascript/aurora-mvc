"""Read-only classification of narrowly compatible server column widening.

Review is advisory only. No ALTER execution is enabled by this module.
"""

from hashlib import sha256
import json
import re

from .inspection import inspect_table
from .planning import _declared_type, _normalized_declaration, plan_schema
from .schema import _default_literal, _quote
from .server_model_review import ServerModelProposal


_VARCHAR = re.compile(r"varchar\((\d+)\)\Z")
_DECIMAL = re.compile(r"decimal\((\d+),(\d+)\)\Z")


def review_server_column_widening(connection, models, alias, identity):
    """Review precisely one widening without changing data or catalog state."""
    from .init_validation import validate_init_models

    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql") or not alias or not identity:
        raise ValueError("A selected server database identity is required")

    ordered = validate_init_models(models, dialect=dialect)
    candidates = []

    for model in ordered:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if (not plan.exists or len(plan.changes) != 1
                or plan.changes[0].kind != "type_mismatch"):
            raise ValueError("Mixed or unsafe server column changes require separate review")

        name = plan.changes[0].column
        field = model.__columns__[name]
        table = inspect_table(connection, model.__table_name__)
        actual = {column.name: column for column in table.columns}

        if (not table.uniqueness_verified or name not in actual
                or field.primary_key or field.unique or field.references is not None
                or field.server_default is not None or field.default is not None
                or actual[name].default_sql is not None
                or actual[name].nullable != field.nullable
                or actual[name].primary_key or name in table.unique_columns):
            raise ValueError("Indexed or constrained column widening is unsupported")

        allowed_index = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if any(index != allowed_index for index in table.indexes):
            raise ValueError("Additional indexes require dedicated widening verification")

        old = _normalized_declaration(actual[name].sql_type, dialect)
        new = _normalized_declaration(_declared_type(field, dialect), dialect)
        old_varchar, new_varchar = _VARCHAR.fullmatch(old), _VARCHAR.fullmatch(new)
        old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)

        if old_varchar and new_varchar:
            if int(new_varchar[1]) <= int(old_varchar[1]):
                raise ValueError("VARCHAR narrowing or unchanged length is not supported")
        elif old_decimal and new_decimal:
            old_precision, old_scale = map(int, old_decimal.groups())
            new_precision, new_scale = map(int, new_decimal.groups())

            if (new_scale < old_scale or new_precision - new_scale < old_precision - old_scale
                    or (new_scale == old_scale and new_precision == old_precision)):
                raise ValueError("DECIMAL precision/scale cannot be reduced")
        else:
            raise ValueError("Only compatible VARCHAR or DECIMAL widening is supported")

        table_name = _quote(model.__table_name__, dialect)
        column_name = _quote(name, dialect)
        sql_type = _declared_type(field, dialect)

        if dialect == "postgresql":
            statement = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE {sql_type}"
        else:
            nullability = " NULL" if field.nullable else " NOT NULL"
            default = _default_literal(field, dialect)
            statement = f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} {sql_type}{nullability}"

            if default is not None:
                statement += f" DEFAULT {default}"

        candidates.append(({"kind": "widen_column", "table": model.__table_name__,
                            "column": name, "from": old, "to": new}, statement))

    if len(candidates) != 1:
        raise ValueError("Exactly one verified widening candidate is required")

    operation, statement = candidates[0]
    payload = json.dumps({"dialect": dialect, "alias": alias, "identity": identity,
                          "operations": (operation,), "statements": (statement,)},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return ServerModelProposal(dialect, (statement,), (operation,),
                               sha256(payload.encode("utf-8")).hexdigest())
