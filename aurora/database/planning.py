"""Conservative, read-only schema comparison; no migration executor."""

from dataclasses import dataclass
from datetime import date, datetime
import re

from .inspection import inspect_table
from .models import Model
from .schema import _default_literal, _SQL_TYPES, create_table_sql


@dataclass(frozen=True)
class SchemaChange:
    kind: str
    column: str | None
    detail: str


@dataclass(frozen=True)
class SchemaPlan:
    table: str
    exists: bool
    changes: tuple[SchemaChange, ...]
    warnings: tuple[str, ...]
    create_sql_preview: str | None = None
    execution_allowed: bool = False

    @property
    def requires_review(self):
        return bool(self.changes or self.warnings)


def _type_family(sql_type):
    """Classify obvious types only; unknowns must be reviewed."""
    name = sql_type.lower().strip()

    if name in ("integer", "bigint", "int", "int4", "int8") or name.startswith(("bigint(", "int(")):
        return "int"

    if name in ("real", "double", "double precision", "float", "float8") or name.startswith("double("):
        return "float"

    if name in ("boolean", "bool"):
        return "bool"

    if name in ("text", "character varying", "varchar") or name.startswith(("varchar(", "character varying(")):
        return "str"

    if name == "date":
        return "date"

    if name in ("datetime", "timestamp", "timestamp without time zone"):
        return "datetime"

    return None


def _sqlite_declared_type(field):
    """Mirror the bounded SQLite declaration types for exact drift checks."""
    if field.sql_type is not None:
        return field.sql_type

    if field.python_type is date:
        return "DATE"

    if field.python_type is datetime:
        return "TIMESTAMP"

    if field.python_type is str and field.length is not None:
        return f"VARCHAR({field.length})"

    return _SQL_TYPES["sqlite"][field.python_type]


def _normalized_declaration(declaration, dialect):
    """Normalize only proven type aliases, not general SQL expressions."""
    value = re.sub(r"\s+", " ", declaration.strip().lower())
    value = re.sub(r"\s*([(),])\s*", r"\1", value)

    if dialect == "mysql":
        if value == "tinyint(1)":
            return "boolean"

        return value

    if dialect == "postgresql":
        value = value.replace("character varying", "varchar")
        value = re.sub(r"^numeric\(", "decimal(", value)

        if value == "timestamp without time zone":
            return "timestamp"

        return value

    return value


def _declared_type(field, dialect):
    if field.sql_type is not None:
        return field.sql_type

    if field.python_type is date:
        return "DATE"

    if field.python_type is datetime:
        return "DATETIME" if dialect == "mysql" else "TIMESTAMP"

    if field.python_type is str and field.length is not None:
        return f"VARCHAR({field.length})"

    return _SQL_TYPES[dialect][field.python_type]


def plan_schema(connection, model):
    """Compare model columns with an existing table; NEVER execute DDL.

    A missing table receives a CREATE TABLE preview. For existing tables,
    comparisons are advisory: constraints, defaults, identity, indexes,
    collation, and dialect-specific type semantics need separate review.
    """

    if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
        raise TypeError("Expected an AuroraSQL Model subclass")
    # Validate that the model can be represented by the requested dialect.
    preview = create_table_sql(model, connection.dialect)
    table = inspect_table(connection, model.__table_name__)

    if not table.exists:
        return SchemaPlan(table.name, False,
                          (SchemaChange("create_table", None, "Table is missing"),),
                          ("Preview only: creating a table requires explicit approval and execution.",),
                          preview)

    changes = []
    warnings = []

    for field_name, field in model.__columns__.items():
        if field.server_default is not None:
            warnings.append(
                f"Model server_default for {field_name} is not emitted by create_table_sql; "
                "database-side default requires explicit review."
            )

    if not table.uniqueness_verified:
        warnings.append("Single-column uniqueness has not been verified for this backend.")

    # Primary-key indexes are a normal consequence of model table creation.
    # Retain manual review for all other indexes, including user-added ones.
    primary_index = {
        "mysql": "PRIMARY",
        "postgresql": f"{table.name}_pkey",
    }.get(connection.dialect)

    if any(index != primary_index and not (
        connection.dialect == "sqlite" and index.startswith("sqlite_autoindex_")
    ) for index in table.indexes):
        warnings.append("Existing indexes require manual review; model index declarations are not supported.")

    if connection.dialect == "sqlite" and not table.constraints_verified:
        warnings.append("Composite or unsupported foreign-key constraints require manual review.")

    actual = {field.name: field for field in table.columns}

    for name, field in model.__columns__.items():
        observed = actual.get(name)

        if observed is None:
            changes.append(SchemaChange("missing_column", name, "Column is absent"))
            continue

        expected_type = {int: "int", float: "float", bool: "bool", str: "str"}.get(field.python_type)

        if connection.dialect == "sqlite":
            expected_declaration = _sqlite_declared_type(field)

            if observed.sql_type.upper().replace(" ", "") != expected_declaration.upper().replace(" ", ""):
                changes.append(SchemaChange(
                    "type_mismatch", name,
                    f"Expected {expected_declaration}, found {observed.sql_type}",
                ))
        elif field.sql_type is not None or field.length is not None:
            declared = _declared_type(field, connection.dialect)
            expected = _normalized_declaration(declared, connection.dialect)
            observed_type = _normalized_declaration(observed.sql_type, connection.dialect)

            if expected != observed_type:
                changes.append(SchemaChange(
                    "type_mismatch", name,
                    f"Expected {declared}, found {observed.sql_type}",
                ))
        else:
            if expected_type is None:
                expected_type = "date" if field.python_type.__name__ == "date" else "datetime"

            actual_type = _type_family(observed.sql_type)

            if (connection.dialect == "mysql" and expected_type == "bool"
                    and _normalized_declaration(observed.sql_type, "mysql") == "boolean"):
                actual_type = "bool"

            if actual_type is None:
                changes.append(SchemaChange("unknown_type", name, f"Unrecognized database type: {observed.sql_type}"))
            elif actual_type != expected_type:
                changes.append(SchemaChange("type_mismatch", name, f"Expected {expected_type}, found {observed.sql_type}"))

        if observed.nullable != (field.nullable and not field.primary_key):
            changes.append(SchemaChange("nullability_mismatch", name, "Nullability differs"))

        if observed.primary_key != field.primary_key:
            changes.append(SchemaChange("primary_key_mismatch", name, "Primary-key status differs"))

        if table.uniqueness_verified and not field.primary_key:
            actual_unique = name in table.unique_columns

            if actual_unique != field.unique:
                changes.append(SchemaChange(
                    "unique_mismatch", name,
                    "Expected unique column" if field.unique else "Unexpected unique column",
                ))

        if connection.dialect == "sqlite" and table.constraints_verified:
            expected_reference = field.references
            actual_reference = next(
                ((target, target_column) for source, target, target_column in table.foreign_keys
                 if source == name), None,
            )

            if ((expected_reference.split(".", 1) if expected_reference else None)
                    != (list(actual_reference) if actual_reference else None)):
                changes.append(SchemaChange(
                    "foreign_key_mismatch", name,
                    "Foreign-key target differs or is missing",
                ))

        if connection.dialect == "sqlite" and field.server_default is None:
            expected_default = _default_literal(field, "sqlite")

            if observed.default_sql != expected_default:
                changes.append(SchemaChange(
                    "default_mismatch", name,
                    "Database-side default differs from the validated model literal",
                ))
        elif connection.dialect in ("mysql", "postgresql") and field.server_default is None:
            expected_default = _default_literal(field, connection.dialect)

            if (expected_default is None) != (observed.default_sql is None):
                changes.append(SchemaChange("default_mismatch", name,
                                            "Database-side default presence differs"))
            elif expected_default is not None:
                warnings.append(f"Database-side default on {name} requires backend-specific equivalence review.")
        elif observed.default_sql is not None:
            if connection.dialect == "sqlite":
                changes.append(SchemaChange("default_mismatch", name,
                                            "Unexpected database-side default"))
            else:
                warnings.append(f"Database-side default on {name} requires manual review.")

    for name in sorted(actual.keys() - model.__columns__.keys()):
        changes.append(SchemaChange("extra_column", name, "Existing column is not declared by the model"))

    return SchemaPlan(table.name, True, tuple(changes), tuple(warnings))
