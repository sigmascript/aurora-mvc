"""Conservative, read-only schema comparison; no migration executor."""

from dataclasses import dataclass

from .inspection import inspect_table
from .models import Model
from .schema import create_table_sql


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
    warnings = [
        "Database defaults, identity behavior, collation, and advanced type semantics are not verified."
    ]
    if not table.uniqueness_verified:
        warnings.append("Single-column uniqueness has not been verified for this backend.")
    if table.indexes:
        warnings.append("Existing indexes require manual review; model index declarations are not supported.")
    actual = {field.name: field for field in table.columns}
    for name, field in model.__columns__.items():
        observed = actual.get(name)
        if observed is None:
            changes.append(SchemaChange("missing_column", name, "Column is absent"))
            continue
        expected_type = {int: "int", float: "float", bool: "bool", str: "str"}.get(field.python_type)
        if expected_type is None:
            expected_type = "date" if field.python_type.__name__ == "date" else "datetime"
        actual_type = _type_family(observed.sql_type)
        # SQLite stores booleans as INTEGER; this is expected for our DDL.
        if connection.dialect == "sqlite" and expected_type == "bool" and actual_type == "int":
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
        if observed.default_sql is not None:
            warnings.append(f"Database-side default on {name} requires manual review.")
    for name in actual.keys() - model.__columns__.keys():
        changes.append(SchemaChange("extra_column", name, "Existing column is not declared by the model"))
    return SchemaPlan(table.name, True, tuple(changes), tuple(warnings))
