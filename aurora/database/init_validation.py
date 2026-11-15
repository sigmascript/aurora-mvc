"""Conservative, pure model-set validation for future recorded initialization.

This module never connects, creates tables, or changes existing AuroraSQL APIs.
"""

from .models import Model
from .schema import create_table_sql, _resolved_foreign_keys


def validate_init_models(models, dialect="sqlite", *, allow_extended=False):
    """Validate all models before DDL and return dependency-first table order.

    Foreign keys are checked for internal consistency but remain unsupported by
    the current write-capable CLI pilot. No approval is issued by this function.
    """

    if not models:
        raise ValueError("At least one model is required")

    tables = {}

    for model in models:
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise TypeError("Expected AuroraSQL Model subclasses")

        table = model.__table_name__

        if table in tables:
            raise ValueError("Duplicate model table names")

        tables[table] = model

        extended = any(field.model_reference or field.on_update != "NO ACTION" or
                       field.on_delete != "NO ACTION" for field in model.__columns__.values())
        if extended and not allow_extended:
            raise ValueError("Extended foreign keys require explicit create-only acceptance; migration/init path not supported")
        # Validate full DDL with explicit registry only for audited create-only use.
        create_table_sql(model, dialect, models=models if allow_extended else None)

        if any(field.server_default is not None for field in model.__columns__.values()):
            raise ValueError("SQL server defaults require manual migration review")

    dependencies = {table: set() for table in tables}

    for table, model in tables.items():
        for field_name, field in model.__columns__.items():
            if field.references is None:
                continue

            target_table, target_column, _, _ = _resolved_foreign_keys(model, models)[field_name]

            if target_table not in tables:
                raise ValueError("Foreign key references a table outside the selected model set")

            target = tables[target_table].__columns__.get(target_column)

            if target is None or not (target.primary_key or target.unique):
                raise ValueError("Foreign key target must be a declared unique or primary-key column")

            if target.python_type is not field.python_type:
                raise ValueError("Foreign key column types do not match")

            # Python type equality alone cannot prove FK SQL compatibility.
            # Compare validated SQL declarations whenever they carry concrete
            # string/decimal width or scale; do not infer a backend coercion.
            def declared_type(column):
                if column.sql_type is not None:
                    return column.sql_type
                if column.python_type is str:
                    return f"VARCHAR({column.length})" if column.length else "TEXT"
                return None

            source_sql = declared_type(field)
            target_sql = declared_type(target)
            if (source_sql is not None or target_sql is not None) and source_sql != target_sql:
                raise ValueError("Foreign key SQL column declarations do not match")

            if target_table != table:
                dependencies[table].add(target_table)

    ordered = []
    remaining = {table: set(deps) for table, deps in dependencies.items()}

    while remaining:
        ready = sorted(table for table, deps in remaining.items() if not deps)

        if not ready:
            raise ValueError("Cyclic foreign-key dependencies require manual migration review")

        for table in ready:
            ordered.append(tables[table])
            del remaining[table]

        for deps in remaining.values():
            deps.difference_update(ready)

    return tuple(ordered)
