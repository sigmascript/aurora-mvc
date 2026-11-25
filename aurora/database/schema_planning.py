"""Conservative, read-only schema comparison; no migration executor."""

import json
import re
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256

from .schema_inspection import inspect_table
from .migration_timeline import inspect_native_history
from .model_discovery import model_identity_map, table_rename_candidates
from .migration_review import create_review
from .snapshots import schema_snapshot
from .models import Model, column
from .schema import _default_literal, _SQL_TYPES, create_table_sql, _resolved_foreign_keys


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

    if name in ("real", "double", "double precision", "float", "float8") or name.startswith(
        "double("
    ):
        return "float"

    if name in ("boolean", "bool"):
        return "bool"

    if name in ("text", "character varying", "varchar") or name.startswith(
        ("varchar(", "character varying(")
    ):
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


def plan_schema(connection, model, *, models=None, trusted_indexes=()):
    """Compare model columns with an existing table; NEVER execute DDL.

    A missing table receives a CREATE TABLE preview. For existing tables,
    comparisons are advisory: constraints, defaults, identity, indexes,
    collation, and dialect-specific type semantics need separate review.
    """

    if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
        raise TypeError("Expected an AuroraSQL Model subclass")
    # Validate that the model can be represented by the requested dialect.
    preview = create_table_sql(model, connection.dialect, models=models)
    resolved = _resolved_foreign_keys(model, models)
    table = inspect_table(connection, model.__table_name__)

    if not table.exists:
        return SchemaPlan(
            table.name,
            False,
            (SchemaChange("create_table", None, "Table is missing"),),
            (),
            preview,
        )

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

    if any(
        index != primary_index
        and index not in trusted_indexes
        and not (connection.dialect == "sqlite" and index.startswith("sqlite_autoindex_"))
        for index in table.indexes
    ):
        warnings.append(
            "Existing indexes require manual review; model index declarations are not supported."
        )

    if not table.constraints_verified:
        if connection.dialect == "sqlite":
            warnings.append(
                "Composite or unsupported foreign-key constraints require manual review."
            )
        elif table.foreign_key_actions is not None:
            warnings.append("Unverified foreign-key constraints require manual review.")

    # MySQL/PostgreSQL inspection currently inventories columns and indexes,
    # but does not verify FK targets or referential actions. Never report a
    # matching schema without recording this unresolved constraint evidence.
    if (
        connection.dialect in ("mysql", "postgresql")
        and not table.constraints_verified
        and any(field.references is not None for field in model.__columns__.values())
    ):
        warnings.append(
            "Foreign-key targets and ON UPDATE/ON DELETE actions have not been "
            "verified for this backend; manual schema review is required."
        )

    # Historical/constructed SQLite TableInfo may lack action evidence.
    if (
        connection.dialect == "sqlite"
        and table.constraints_verified
        and table.foreign_key_actions is None
        and any(field.references is not None for field in model.__columns__.values())
    ):
        warnings.append(
            "Foreign-key ON UPDATE/ON DELETE actions were not inspected; "
            "manual schema review is required."
        )

    actual = {field.name: field for field in table.columns}

    for name, field in model.__columns__.items():
        observed = actual.get(name)

        if observed is None:
            changes.append(SchemaChange("missing_column", name, "Column is absent"))
            continue

        expected_type = {int: "int", float: "float", bool: "bool", str: "str"}.get(
            field.python_type
        )

        if connection.dialect == "sqlite":
            expected_declaration = _sqlite_declared_type(field)

            if observed.sql_type.upper().replace(" ", "") != expected_declaration.upper().replace(
                " ", ""
            ):
                changes.append(
                    SchemaChange(
                        "type_mismatch",
                        name,
                        f"Expected {expected_declaration}, found {observed.sql_type}",
                    )
                )
        elif field.sql_type is not None or field.length is not None:
            declared = _declared_type(field, connection.dialect)
            expected = _normalized_declaration(declared, connection.dialect)
            observed_type = _normalized_declaration(observed.sql_type, connection.dialect)

            if expected != observed_type:
                changes.append(
                    SchemaChange(
                        "type_mismatch",
                        name,
                        f"Expected {declared}, found {observed.sql_type}",
                    )
                )
        else:
            if expected_type is None:
                expected_type = "date" if field.python_type.__name__ == "date" else "datetime"

            actual_type = _type_family(observed.sql_type)

            if (
                connection.dialect == "mysql"
                and expected_type == "bool"
                and _normalized_declaration(observed.sql_type, "mysql") == "boolean"
            ):
                actual_type = "bool"

            if actual_type is None:
                changes.append(
                    SchemaChange(
                        "unknown_type", name, f"Unrecognized database type: {observed.sql_type}"
                    )
                )
            elif actual_type != expected_type:
                changes.append(
                    SchemaChange(
                        "type_mismatch",
                        name,
                        f"Expected {expected_type}, found {observed.sql_type}",
                    )
                )

        if observed.nullable != (field.nullable and not field.primary_key):
            changes.append(SchemaChange("nullability_mismatch", name, "Nullability differs"))

        if observed.primary_key != field.primary_key:
            changes.append(SchemaChange("primary_key_mismatch", name, "Primary-key status differs"))

        if table.uniqueness_verified and not field.primary_key:
            actual_unique = name in table.unique_columns

            if actual_unique != field.unique:
                changes.append(
                    SchemaChange(
                        "unique_mismatch",
                        name,
                        "Expected unique column" if field.unique else "Unexpected unique column",
                    )
                )

        if table.constraints_verified:
            reference = resolved.get(name)
            expected_reference = f"{reference[0]}.{reference[1]}" if reference else None
            actual_reference = next(
                (
                    (target, target_column)
                    for source, target, target_column in table.foreign_keys
                    if source == name
                ),
                None,
            )

            if (expected_reference.split(".", 1) if expected_reference else None) != (
                list(actual_reference) if actual_reference else None
            ):
                changes.append(
                    SchemaChange(
                        "foreign_key_mismatch",
                        name,
                        "Foreign-key target differs or is missing",
                    )
                )

            # Compare both actions against the explicitly resolved declaration.
            # This planner never approves or executes DDL.
            if table.foreign_key_actions is not None and expected_reference and actual_reference:
                actual_actions = [
                    (update, delete)
                    for source, update, delete in table.foreign_key_actions
                    if source == name
                ]
            else:
                actual_actions = None
            expected_actions = [(field.on_update, field.on_delete)]
            if connection.dialect == "mysql" and actual_actions is not None:
                # MySQL implements NO ACTION as immediate RESTRICT.
                def normalized(actions):
                    return [
                        (
                            "RESTRICT" if up == "NO ACTION" else up,
                            "RESTRICT" if down == "NO ACTION" else down,
                        )
                        for up, down in actions
                    ]

                actual_actions = normalized(actual_actions)
                expected_actions = normalized(expected_actions)
            if actual_actions is not None and actual_actions != expected_actions:
                changes.append(
                    SchemaChange(
                        "foreign_key_action_mismatch",
                        name,
                        "Existing foreign-key ON UPDATE/ON DELETE actions differ from the model declaration",
                    )
                )

        if connection.dialect == "sqlite" and field.server_default is None:
            expected_default = _default_literal(field, "sqlite")

            if observed.default_sql != expected_default:
                changes.append(
                    SchemaChange(
                        "default_mismatch",
                        name,
                        "Database-side default differs from the validated model literal",
                    )
                )
        elif connection.dialect in ("mysql", "postgresql") and field.server_default is None:
            from .schema_inspection import server_default_matches

            if not server_default_matches(field, observed.default_sql, connection.dialect):
                changes.append(
                    SchemaChange(
                        "default_mismatch",
                        name,
                        "Database-side literal default differs from the model declaration",
                    )
                )
        elif observed.default_sql is not None:
            if connection.dialect == "sqlite":
                changes.append(
                    SchemaChange("default_mismatch", name, "Unexpected database-side default")
                )
            else:
                warnings.append(f"Database-side default on {name} requires manual review.")

    for name in sorted(actual.keys() - model.__columns__.keys()):
        changes.append(
            SchemaChange("extra_column", name, "Existing column is not declared by the model")
        )

    return SchemaPlan(table.name, True, tuple(changes), tuple(warnings))


class NoModelChanges(ValueError):
    """The verified SQLite schema already matches the selected models."""


def create_sqlite_model_review(connection, models, alias="default"):
    """Build an additive review; refuse uncertain history or structural drift.

    No SQL is executed and the returned review cannot authorize migration.
    The physical table DDL is retained as a baseline witness for the existing
    executor's exact catalog comparison at application time.
    """
    if connection.dialect != "sqlite" or not models:
        raise ValueError("Model-driven review requires SQLite models")

    models = tuple(models)
    current = model_identity_map(models)
    timeline, _, _ = inspect_native_history(connection, alias)

    if not timeline or not timeline[-1]["physical_history_consistent"]:
        raise RuntimeError("Verified native migration history is required")

    previous = timeline[-1]["model_tables"]

    if set(previous) - set(current) or table_rename_candidates(previous, models):
        raise ValueError("Removed or renamed models need separate explicit review")

    after = schema_snapshot(*models, dialect="sqlite")
    before = deepcopy(after)
    prior_tables = []

    for table in before["tables"]:
        target = next(model for model in models if model.__table_name__ == table["name"])
        plan = plan_schema(connection, target)
        kinds = {change.kind for change in plan.changes}

        if not plan.exists:
            if kinds != {"create_table"} or table["name"] in previous.values():
                raise RuntimeError("Missing historical table requires reconciliation")

            continue

        if kinds - {"missing_column"}:
            raise RuntimeError("Unsupported existing-column drift needs explicit review")

        missing = {change.column for change in plan.changes}

        if any(
            (
                (not target.__columns__[name].nullable and target.__columns__[name].default is None)
                or target.__columns__[name].unique
                or target.__columns__[name].primary_key
                or target.__columns__[name].references is not None
            )
            for name in missing
        ):
            raise ValueError("Only ordinary nullable or defaulted required columns are supported")

        row = connection._raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table["name"],),
        ).fetchone()

        if row is None or not isinstance(row[0], str) or not row[0].startswith("CREATE TABLE "):
            raise RuntimeError("A canonical existing table definition is required")

        table["columns"] = [column for column in table["columns"] if column["name"] not in missing]
        table["create_sql_preview"] = row[0]
        prior_tables.append(table)

    before["tables"] = prior_tables
    identity = connection._raw.execute("PRAGMA database_list").fetchall()
    main = [row[2] for row in identity if row[1] == "main"]

    if len(main) != 1 or not main[0]:
        raise RuntimeError("Model review requires a named SQLite database")

    body = json.dumps(
        {"before": before, "after": after, "alias": alias, "database": main[0]},
        sort_keys=True,
        separators=(",", ":"),
    )
    migration_id = "Model" + sha256(body.encode("utf-8")).hexdigest()[:24]
    review = create_review(before, after, migration_id=migration_id)

    if not review["plan"]["operations"]:
        raise NoModelChanges("There are no supported model changes to migrate")

    return review


# Ephemeral predecessor declarations for read-only migration planning.
def column_addition_predecessors(models, operation):
    """Reconstruct old table declarations without modifying user model classes."""
    if not isinstance(operation, dict) or operation.get("kind") != "add_column":
        raise ValueError("One explicit column-addition operation is required")

    target = operation["table"]
    field_name = operation["column"]
    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != target:
            predecessor.append(model)
            continue

        if matched or field_name not in model.__columns__:
            raise RuntimeError("Column addition target is ambiguous")

        remaining = {name: field for name, field in model.__columns__.items() if name != field_name}

        if not remaining or not any(field.primary_key for field in remaining.values()):
            raise ValueError("Existing primary-key structure must remain unchanged")

        # The ephemeral class is never imported, registered or installed on a
        # user model. It exists only to regenerate the old CREATE fingerprint.
        historic = type("HistoricalTable", (Model,), {"__table__": target, **remaining})
        predecessor.append(historic)
        matched = True

    if not matched:
        raise ValueError("Reviewed added column does not belong to a selected model")

    return tuple(predecessor)


def default_addition_predecessor_models(models, operation):
    """Construct old declarations without modifying the user's default fields."""
    if (
        not isinstance(operation, dict)
        or operation.get("kind") != "add_default"
        or set(operation) != {"kind", "table", "column"}
    ):
        raise ValueError("A reviewed SQL default addition is required")

    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != operation["table"]:
            predecessor.append(model)
            continue

        name = operation["column"]

        if matched or name not in model.__columns__:
            raise ValueError("Historical default target is ambiguous")

        current = model.__columns__[name]

        if (
            current.primary_key
            or current.unique
            or current.references is not None
            or current.default is None
            or current.server_default is not None
        ):
            raise ValueError("Historical default cannot change constrained columns")

        previous = copy(current)
        previous.default = None
        fields = {
            field: previous if field == name else declaration
            for field, declaration in model.__columns__.items()
        }
        predecessor.append(
            type("HistoricalTable", (Model,), {"__table__": model.__table_name__, **fields})
        )
        matched = True

    if not matched:
        raise ValueError("Historical default model is missing")

    return tuple(predecessor)


def nullability_predecessor_models(models, operation):
    """Reconstruct the prior nullable flag without changing application models."""
    if (
        not isinstance(operation, dict)
        or operation.get("kind") != "change_nullability"
        or type(operation.get("from")) is not bool
        or type(operation.get("to")) is not bool
        or operation["from"] == operation["to"]
    ):
        raise ValueError("A reviewed nullability transition is required")

    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != operation["table"]:
            predecessor.append(model)
            continue

        name = operation["column"]

        if matched or name not in model.__columns__:
            raise ValueError("Historical nullability target is ambiguous")

        current = model.__columns__[name]

        if (
            current.nullable is not operation["to"]
            or current.primary_key
            or current.unique
            or current.references is not None
            or current.default is not None
            or current.server_default is not None
        ):
            raise ValueError("Historical nullability cannot change constrained columns")

        previous = copy(current)
        previous.nullable = operation["from"]
        fields = {
            field: previous if field == name else declaration
            for field, declaration in model.__columns__.items()
        }
        predecessor.append(
            type("HistoricalTable", (Model,), {"__table__": model.__table_name__, **fields})
        )
        matched = True

    if not matched:
        raise ValueError("Historical nullability model is missing")

    return tuple(predecessor)


def widening_predecessor_models(models, operation):
    """Reconstruct old types without mutating application model declarations."""
    if not isinstance(operation, dict) or operation.get("kind") != "widen_column":
        raise ValueError("A reviewed widening operation is required")

    table = operation["table"]
    name = operation["column"]
    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != table:
            predecessor.append(model)
            continue

        if matched or name not in model.__columns__:
            raise ValueError("Historical widening target is ambiguous")

        current = model.__columns__[name]

        if (
            current.primary_key
            or current.unique
            or current.references is not None
            or current.default is not None
            or current.server_default is not None
        ):
            raise ValueError("Historical widening cannot change constrained columns")

        previous_field = column(operation["from"], nullable=current.nullable)
        fields = {
            field: previous_field if field == name else declaration
            for field, declaration in model.__columns__.items()
        }
        predecessor.append(type("HistoricalTable", (Model,), {"__table__": table, **fields}))
        matched = True

    if not matched:
        raise ValueError("Historical widening model is missing")

    return tuple(predecessor)


# Native server successor predecessor reconstruction (read-only planning).
def _historical_column(field, *, sql_type=None, nullable=None, remove_default=False):
    """Copy one declaration without changing application-owned model columns."""
    declaration = sql_type if sql_type is not None else (field.sql_type or field.python_type)
    return column(
        declaration,
        primary_key=field.primary_key,
        nullable=field.nullable if nullable is None else nullable,
        default=None if remove_default else field.default,
        length=field.length if sql_type is None and field.sql_type is None else None,
        unique=field.unique,
        server_default=field.server_default,
        references=field.references,
    )


def predecessor_models(models, operation, dialect="mysql"):
    """Undo one recorded supported operation in temporary model declarations."""
    kind = operation["kind"]
    target = operation["table"]
    predecessors = []
    found = False

    for model in models:
        if model.__table_name__ != target:
            predecessors.append(model)
            continue

        if found:
            raise ValueError("Ambiguous server operation target")

        found = True

        if kind == "create_table":
            continue

        field_name = operation["column"]
        fields = dict(model.__columns__)

        if field_name not in fields:
            raise ValueError("Historical column is absent from the model")

        field = fields[field_name]

        if (
            field.primary_key
            or field.unique
            or field.references is not None
            or field.server_default is not None
        ):
            raise ValueError("Dependent historical column cannot be reconstructed")

        if kind == "add_column":
            if not field.nullable:
                raise ValueError("Historical column addition requires a nullable field")

            del fields[field_name]
        elif kind == "widen_column":
            old = operation["from"]
            new = operation["to"]
            current = _normalized_declaration(_declared_type(field, dialect), dialect)

            if current != new or not _compatible_widening(old, new):
                raise ValueError("Historical widening is not monotonic")

            fields[field_name] = _historical_column(field, sql_type=old.upper())
        elif kind == "change_nullability":
            if field.nullable is not operation["to"] or operation["from"] is operation["to"]:
                raise ValueError("Historical nullability transition differs")

            fields[field_name] = _historical_column(field, nullable=operation["from"])
        elif kind == "add_default":
            if field.default is None:
                raise ValueError("Historical SQL default addition is absent")

            fields[field_name] = _historical_column(field, remove_default=True)
        else:
            raise ValueError("Unsupported historical server operation")

        if not fields or not any(item.primary_key for item in fields.values()):
            raise ValueError("Historical table lost its primary key")

        predecessors.append(type("HistoricalTable", (Model,), {"__table__": target, **fields}))

    if not found:
        raise ValueError("Historical server operation target is missing")

    return tuple(predecessors)


def _compatible_widening(old, new):
    from .migration_review import _DECIMAL, _VARCHAR

    old_varchar = _VARCHAR.fullmatch(old)
    new_varchar = _VARCHAR.fullmatch(new)
    old_decimal = _DECIMAL.fullmatch(old)
    new_decimal = _DECIMAL.fullmatch(new)

    if old_varchar and new_varchar:
        return int(old_varchar[1]) < int(new_varchar[1])

    if old_decimal and new_decimal:
        old_precision, old_scale = map(int, old_decimal.groups())
        new_precision, new_scale = map(int, new_decimal.groups())
        return (
            new_scale >= old_scale
            and new_precision - new_scale >= old_precision - old_scale
            and (old_precision, old_scale) != (new_precision, new_scale)
        )

    return False
