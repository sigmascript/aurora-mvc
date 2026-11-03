"""Conservative, pure model-set validation for future recorded initialization.

This module never connects, creates tables, or changes existing AuroraSQL APIs.
"""

from .models import Model
from .schema import create_table_sql


def validate_init_models(models, dialect="sqlite"):
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
        # Validate full dialect-specific DDL before any future write.
        create_table_sql(model, dialect)

        if any(field.server_default is not None for field in model.__columns__.values()):
            raise ValueError("SQL server defaults require manual migration review")

    dependencies = {table: set() for table in tables}

    for table, model in tables.items():
        for field in model.__columns__.values():
            if field.references is None:
                continue

            target_table, target_column = field.references.split(".", 1)

            if target_table not in tables:
                raise ValueError("Foreign key references a table outside the selected model set")

            target = tables[target_table].__columns__.get(target_column)

            if target is None or not (target.primary_key or target.unique):
                raise ValueError("Foreign key target must be a declared unique or primary-key column")

            if target.python_type is not field.python_type:
                raise ValueError("Foreign key column types do not match")

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
