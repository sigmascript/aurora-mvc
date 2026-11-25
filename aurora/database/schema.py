"""Explicit, create-only table DDL for AuroraSQL models.

No introspection, migration, or implicit schema modifications are performed.
Validated constant model defaults are emitted as database-side SQL defaults.
"""

import math
from datetime import date, datetime
from decimal import Decimal

from .models import Model, _identifier

_SQL_TYPES = {
    "sqlite": {int: "INTEGER", float: "REAL", bool: "INTEGER", str: "TEXT"},
    "mysql": {int: "BIGINT", float: "DOUBLE", bool: "BOOLEAN", str: "TEXT"},
    "postgresql": {int: "BIGINT", float: "DOUBLE PRECISION", bool: "BOOLEAN", str: "TEXT"},
}


def _quote(name, dialect):
    _identifier(name)
    return f"`{name}`" if dialect == "mysql" else f'"{name}"'


def _default_literal(field, dialect):
    """Render a validated constant, never an arbitrary SQL expression."""
    value = field.default

    if value is None:
        return None

    field.validate(value)

    if type(value) is bool:
        if dialect == "postgresql":
            return "TRUE" if value else "FALSE"

        return "1" if value else "0"

    if type(value) is int:
        return str(value)

    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Database default must be finite")

        return repr(value)

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Database default must be finite")

        return format(value, "f")

    if isinstance(value, (str, date, datetime)):
        literal = (
            value
            if isinstance(value, str)
            else value.isoformat(sep=" ")
            if isinstance(value, datetime)
            else value.isoformat()
        )
        return "'" + literal.replace("'", "''") + "'"

    raise TypeError("Unsupported database default literal")


def _resolved_foreign_keys(model, models):
    """Resolve declared targets within only the explicitly selected model set."""
    selected = tuple(models or ())
    if models is not None and model not in selected:
        raise ValueError("DDL model must belong to selected database models")
    by_name = {}
    by_table = {}
    for candidate in selected:
        if (
            not isinstance(candidate, type)
            or not issubclass(candidate, Model)
            or candidate is Model
        ):
            raise TypeError("Expected AuroraSQL Model subclasses")
        if candidate.__name__ in by_name or candidate.__table_name__ in by_table:
            raise ValueError("Ambiguous model identity in selected database")
        by_name[candidate.__name__] = candidate
        by_table[candidate.__table_name__] = candidate

    result = {}
    for field_name, field in model.__columns__.items():
        if field.references is None:
            continue
        table_name, column_name = field.references.split(".")
        if field.model_reference:
            if not selected:
                raise ValueError("Model-qualified reference requires selected database models")
            target = by_name.get(table_name)
            if target is None:
                raise ValueError("Unknown model-qualified foreign-key target")
        else:
            target = by_table.get(table_name)
            if selected and target is None:
                raise ValueError("Foreign key target is outside selected database models")
        if target is not None:
            referenced = target.__columns__.get(column_name)
            if referenced is None or not (referenced.primary_key or referenced.unique):
                raise ValueError("Foreign-key target must be a primary or unique column")
            if referenced.python_type is not field.python_type:
                raise ValueError("Foreign-key target type mismatch")

            def declared_type(column):
                if column.sql_type is not None:
                    return column.sql_type
                if column.python_type is str:
                    return f"VARCHAR({column.length})" if column.length else "TEXT"
                return None

            source_sql, target_sql = declared_type(field), declared_type(referenced)
            if (source_sql is not None or target_sql is not None) and source_sql != target_sql:
                raise ValueError("Foreign key SQL column declarations do not match")
            table_name = target.__table_name__
        result[field_name] = (table_name, column_name, field.on_update, field.on_delete)
    return result


def create_table_sql(model, dialect, *, models=None):
    """Generate CREATE TABLE DDL without connecting to a database."""

    if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
        raise TypeError("Expected an AuroraSQL Model subclass")

    if dialect not in _SQL_TYPES:
        raise ValueError("Unsupported database dialect")

    from datetime import date, datetime

    columns = model.__columns__
    primary_keys = [name for name, field in columns.items() if field.primary_key]

    if len(primary_keys) > 1:
        raise ValueError("Composite primary keys are not supported yet")

    extended = any(
        field.model_reference or field.on_update != "NO ACTION" or field.on_delete != "NO ACTION"
        for field in model.__columns__.values()
    )
    if extended and models is None:
        raise ValueError("Extended foreign keys require an explicit selected model set")
    if dialect == "mysql" and any(
        "SET DEFAULT" in (field.on_update, field.on_delete) for field in model.__columns__.values()
    ):
        raise ValueError("MySQL does not support SET DEFAULT foreign-key actions")
    resolved = _resolved_foreign_keys(model, models)
    declarations = []
    foreign_keys = []

    for name, field in columns.items():
        kind = field.python_type

        if field.sql_type is not None:
            sql_type = field.sql_type
        elif kind is date:
            sql_type = "DATE"
        elif kind is datetime:
            sql_type = "DATETIME" if dialect == "mysql" else "TIMESTAMP"
        elif kind is str and field.length is not None:
            sql_type = f"VARCHAR({field.length})"
        else:
            sql_type = _SQL_TYPES[dialect][kind]

        if (
            dialect == "mysql"
            and field.unique
            and kind is str
            and field.length is None
            and (field.sql_type is None or field.sql_type == "TEXT")
        ):
            raise ValueError("Unique MySQL string columns require an explicit length")

        parts = [_quote(name, dialect), sql_type]

        if field.primary_key:
            if kind is int:
                if dialect == "mysql":
                    parts.append("AUTO_INCREMENT")
                elif dialect == "postgresql":
                    parts.append("GENERATED BY DEFAULT AS IDENTITY")

            parts.append("PRIMARY KEY")

        if not field.nullable and not field.primary_key:
            parts.append("NOT NULL")

        if field.unique and not field.primary_key:
            parts.append("UNIQUE")

        default_sql = _default_literal(field, dialect)

        if default_sql is not None:
            parts.append(f"DEFAULT {default_sql}")

        declarations.append(" ".join(parts))

        if field.references is not None:
            referenced_table, referenced_column, update, delete = resolved[name]
            action_sql = ""
            if update != "NO ACTION" or delete != "NO ACTION":
                action_sql = f" ON UPDATE {update} ON DELETE {delete}"
            foreign_keys.append(
                f"FOREIGN KEY ({_quote(name, dialect)}) "
                f"REFERENCES {_quote(referenced_table, dialect)}({_quote(referenced_column, dialect)}){action_sql}"
            )

    declarations.extend(foreign_keys)
    return f"CREATE TABLE {_quote(model.__table_name__, dialect)} (" + ", ".join(declarations) + ")"


def create_table(connection, model):
    """Create a new table explicitly; existing tables cause a database error.

    This is not a migration operation and never uses IF NOT EXISTS.
    Caller controls transaction scope. DDL may implicitly commit on MySQL.
    """

    if connection.dialect not in _SQL_TYPES:
        raise ValueError("Unsupported database dialect")

    return connection.execute_write(create_table_sql(model, connection.dialect))


def create_tables(connection, *models):
    """Create multiple new tables in supplied order; not an atomic migration."""

    if not models:
        raise ValueError("At least one model is required")

    ordered = validate_init_models(models, dialect=connection.dialect, allow_extended=True)
    statements = [create_table_sql(model, connection.dialect, models=ordered) for model in ordered]
    for sql in statements:
        connection.execute_write(sql)


def validate_init_models(models, dialect="sqlite", *, allow_extended=False):
    """Validate all models before DDL and return dependency-first table order.

    Extended relationships require explicit create-only opt-in. This validation
    never authorizes changes to existing tables or historical migrations.
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

        extended = any(
            field.model_reference
            or field.on_update != "NO ACTION"
            or field.on_delete != "NO ACTION"
            for field in model.__columns__.values()
        )
        if extended and not allow_extended:
            raise ValueError(
                "Extended foreign keys require explicit create-only acceptance; migration/init path not supported"
            )
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
                raise ValueError(
                    "Foreign key target must be a declared unique or primary-key column"
                )

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
