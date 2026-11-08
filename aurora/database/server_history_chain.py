"""Read-only evidence reconstruction for linear server migration histories.

Historical catalog states are derived from a verified current schema and the
recorded narrow operations. Reconstructed states are evidence to compare with
immutable journals, never permission to execute or repair database changes.
"""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json

from .model_identity import model_identity_map
from .models import Model, column
from .planning import _declared_type, _normalized_declaration
from .schema import _default_literal, _quote, create_table_sql
from .server_column_widening import _DECIMAL, _VARCHAR
from .server_model_review import _add_column_statement


def catalog_fingerprint(dialect, observations):
    """Reproduce the server catalog digest without changing its ordering."""
    payload = json.dumps({"dialect": dialect, "observed": observations},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


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

        if field.primary_key or field.unique or field.references is not None or field.server_default is not None:
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
    old_varchar = _VARCHAR.fullmatch(old)
    new_varchar = _VARCHAR.fullmatch(new)
    old_decimal = _DECIMAL.fullmatch(old)
    new_decimal = _DECIMAL.fullmatch(new)

    if old_varchar and new_varchar:
        return int(old_varchar[1]) < int(new_varchar[1])

    if old_decimal and new_decimal:
        old_precision, old_scale = map(int, old_decimal.groups())
        new_precision, new_scale = map(int, new_decimal.groups())
        return (new_scale >= old_scale and new_precision - new_scale >= old_precision - old_scale
                and (old_precision, old_scale) != (new_precision, new_scale))

    return False


def reviewed_statement(models, operation, dialect):
    """Recreate a supported successor's original reviewed DDL statement."""
    kind = operation["kind"]
    matching = [model for model in models if model.__table_name__ == operation["table"]]

    if len(matching) != 1:
        raise ValueError("Reviewed server table is missing or ambiguous")

    model = matching[0]

    if kind == "create_table":
        keys = [field for field in model.__columns__.values() if field.primary_key]

        if (len(keys) != 1 or keys[0].python_type is not int
                or any(field.references is not None or field.server_default is not None
                       for field in model.__columns__.values())):
            raise ValueError("Historical server table creation has unsupported constraints")

        return create_table_sql(model, dialect)

    name = operation["column"]

    if name not in model.__columns__:
        raise ValueError("Reviewed server column is missing")

    field = model.__columns__[name]
    table_sql = _quote(operation["table"], dialect)
    column_sql = _quote(name, dialect)

    if (field.primary_key or field.unique or field.references is not None
            or field.server_default is not None):
        raise ValueError("Reviewed server column has unsupported dependencies")

    if kind == "add_column":
        return _add_column_statement(model, name, dialect)

    if kind == "widen_column":
        if field.default is not None:
            raise ValueError("Defaulted column widening is not supported")

        if not _compatible_widening(operation["from"], operation["to"]):
            raise ValueError("Reviewed widening would lose precision or capacity")

        declared = _declared_type(field, dialect)

        if _normalized_declaration(declared, dialect) != operation["to"]:
            raise ValueError("Reviewed widening target differs from declaration")

        if dialect == "postgresql":
            return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} TYPE {declared}"

        nullability = " NULL" if field.nullable else " NOT NULL"
        return f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} {declared}{nullability}"

    if kind == "change_nullability":
        if field.default is not None:
            raise ValueError("Defaulted column nullability change is not supported")

        if field.nullable is not operation["to"]:
            raise ValueError("Reviewed nullability differs from declaration")

        if dialect == "postgresql":
            action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
            return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {action}"

        nullability = "NULL" if field.nullable else "NOT NULL"
        return (f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} "
                f"{_declared_type(field, dialect)} {nullability}")

    if kind == "add_default":
        literal = _default_literal(field, dialect)

        if literal is None:
            raise ValueError("Reviewed SQL default is missing")

        return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} SET DEFAULT {literal}"

    raise ValueError("Unsupported server operation evidence")


def preceding_observations(observations, operation, dialect):
    """Undo one approved catalog difference in read-only observation data."""
    before = deepcopy(list(observations))
    matching = [table for table in before if table["table"] == operation["table"]]

    if len(matching) != 1:
        raise ValueError("Historical catalog target is missing or ambiguous")

    table = matching[0]
    kind = operation["kind"]

    if kind == "create_table":
        before.remove(table)
        return before

    column_name = operation["column"]
    matches = [item for item in table["columns"] if item[0] == column_name]

    if (len(matches) != 1 or matches[0][3] or column_name in table["unique_columns"]
            or len(table["indexes"]) != 1 + len(table["unique_columns"])):
        raise ValueError("Historical catalog column has dependencies")

    observed = matches[0]

    if kind == "add_column":
        if not observed[2]:
            raise ValueError("Historical nullable column addition is incompatible")

        table["columns"] = [item for item in table["columns"] if item[0] != column_name]
    elif kind == "widen_column":
        if not _compatible_widening(operation["from"], operation["to"]):
            raise ValueError("Historical column widening is incompatible")

        current = _normalized_declaration(observed[1], dialect)

        if current != operation["to"]:
            raise ValueError("Historical column type differs from recorded successor")

        previous_type = operation["from"]

        if dialect == "postgresql":
            if previous_type.startswith("varchar("):
                previous_type = "character varying(" + previous_type[len("varchar("):]
            elif previous_type.startswith("decimal("):
                previous_type = "numeric(" + previous_type[len("decimal("):]

        table["columns"] = [(item[0], previous_type, *item[2:])
                            if item[0] == column_name else item for item in table["columns"]]
    elif kind == "change_nullability":
        if observed[2] is not operation["to"] or operation["from"] is operation["to"]:
            raise ValueError("Historical column nullability differs")

        table["columns"] = [(item[0], item[1], operation["from"], *item[3:])
                            if item[0] == column_name else item for item in table["columns"]]
    elif kind == "add_default":
        if observed[4] is None:
            raise ValueError("Historical column default is missing")

        table["columns"] = [(*item[:4], None) if item[0] == column_name else item
                            for item in table["columns"]]
    else:
        raise ValueError("Unsupported catalog history operation")

    return before


def expected_server_journal(models, timeline, dialect, identity, observations, *,
                            current_model_mapping=None):
    """Validate all recorded successor digests and reconstruct journal hashes.

    The caller must first verify current physical catalog equivalence and the
    timeline's strict structural rules. This function performs no SQL or I/O.
    """
    if not timeline or any(item.status != "applied" for item in timeline):
        raise RuntimeError("An unresolved server history cannot be reconstructed")

    remaining_models = tuple(models)
    remaining_catalog = deepcopy(list(observations))
    hashes = []
    model_mapping = dict(timeline[-1].model_tables)
    selected_mapping = (model_identity_map(remaining_models) if current_model_mapping is None
                        else current_model_mapping)

    if (catalog_fingerprint(dialect, remaining_catalog) != timeline[-1].schema_sha256
            or selected_mapping != model_mapping
            or any(item.database_alias != timeline[0].database_alias for item in timeline)):
        raise RuntimeError("Current server catalog or model mapping differs from history")

    for index in range(len(timeline) - 1, 0, -1):
        entry = timeline[index]
        statement = reviewed_statement(remaining_models, entry.operation, dialect)
        payload = json.dumps({"dialect": dialect, "alias": entry.database_alias,
                              "identity": identity, "operations": (entry.operation,),
                              "statements": (statement,)},
                             sort_keys=True, separators=(",", ":"), allow_nan=False)

        if sha256(payload.encode("utf-8")).hexdigest() != entry.review_sha256:
            raise RuntimeError("Historical reviewed server statement differs")

        hashes.append((entry.operation["table"], sha256(statement.encode("utf-8")).hexdigest()))
        remaining_models = predecessor_models(remaining_models, entry.operation, dialect)
        remaining_catalog = preceding_observations(remaining_catalog, entry.operation, dialect)

        if entry.operation["kind"] == "create_table":
            added = [key for key, value in model_mapping.items()
                     if value == entry.operation["table"]]

            if len(added) != 1:
                raise RuntimeError("Historical server model creation is ambiguous")

            del model_mapping[added[0]]

        if model_mapping != timeline[index - 1].model_tables:
            raise RuntimeError("Historical server model identity chain differs")

        if catalog_fingerprint(dialect, remaining_catalog) != timeline[index - 1].schema_sha256:
            raise RuntimeError("Historical server catalog fingerprint differs")

    if {model.__table_name__ for model in remaining_models} != set(timeline[0].model_tables.values()):
        raise RuntimeError("Historical Initial model tables differ")

    hashes.extend((model.__table_name__, sha256(create_table_sql(model, dialect).encode("utf-8")).hexdigest())
                  for model in remaining_models)
    return Counter(hashes)


def verify_server_applied_chain(connection, models, alias, identity, *,
                                current_model_mapping=None):
    """Prove a repeated native chain against the current catalog and journals.

    No timeline status, recorded hash or model declaration alone is enough.
    All three independent evidence families must agree without mutation.
    The Initial-only and single-successor legacy verifiers stay separate until
    compatibility acceptance establishes their unchanged behavior.
    """
    from .history import list_history
    from .inspection import inspect_table
    from .server_initial_verification import verify_server_declared_schema
    from .server_migration_timeline import inspect_server_timeline

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) < 2 or any(item.status != "applied" for item in timeline):
        return False

    try:
        journal_table = inspect_table(connection, "aurorasql_schema_history")
        journal_columns = {column.name: column for column in journal_table.columns}
        required = {"operation_id", "table_name", "dialect", "fingerprint", "status"}

        if (not journal_table.exists or set(journal_columns) != required
                or not journal_columns["operation_id"].primary_key
                or any(column.nullable for column in journal_columns.values())
                or any(column.primary_key for name, column in journal_columns.items()
                       if name != "operation_id")
                or not journal_table.uniqueness_verified
                or journal_table.unique_columns
                or len(journal_table.indexes) != 1):
            return False

        if connection.dialect == "mysql":
            triggers = connection.fetch_all(
                "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND EVENT_OBJECT_TABLE IN ('aurorasql_schema_history', 'aurorasql_migration_timeline')")
        elif connection.dialect == "postgresql":
            triggers = connection.fetch_all(
                "SELECT t.tgname AS name FROM pg_catalog.pg_trigger t "
                "WHERE t.tgrelid IN (to_regclass('aurorasql_schema_history'), "
                "to_regclass('aurorasql_migration_timeline')) AND NOT t.tgisinternal")
        else:
            return False

        if triggers:
            return False

        digest, observations = verify_server_declared_schema(
            connection, models, include_observations=True)

        if digest != timeline[-1].schema_sha256:
            return False

        expected = expected_server_journal(
            models, timeline, connection.dialect, identity, observations,
            current_model_mapping=current_model_mapping)
        journal = list_history(connection)

        if (len(journal) != sum(expected.values())
                or len({entry.operation_id for entry in journal}) != len(journal)
                or any(entry.status != "applied" or entry.dialect != connection.dialect
                       for entry in journal)):
            return False

        actual = Counter((entry.table, entry.fingerprint) for entry in journal)
        return actual == expected
    except (ValueError, TypeError, RuntimeError, KeyError, IndexError):
        return False


def verify_server_review_predecessor(connection, models, alias, identity, review):
    """Prove the entire physical state before a proposed later successor.

    Derive only one supported previous model state from the proposal. The
    caller rechecks the exact proposal and database identity under its held
    backend lock before using this as one prerequisite for a prepared intent.
    """
    from .server_migration_timeline import inspect_server_timeline

    if len(review.operations) != 1:
        raise ValueError("One reviewed server operation is required")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) < 2:
        raise RuntimeError("A previously applied successor is required")

    if any(item.status != "applied" for item in timeline):
        raise RuntimeError("Unresolved server history cannot authorize a successor")

    operation = review.operations[0]
    earlier = predecessor_models(models, operation, connection.dialect)
    mapping = dict(model_identity_map(models))

    if operation["kind"] == "create_table":
        added = [key for key, value in mapping.items() if value == operation["table"]]

        if len(added) != 1:
            raise ValueError("New model identity is ambiguous")

        del mapping[added[0]]

    if mapping != timeline[-1].model_tables:
        raise RuntimeError("Proposed model identities differ from the applied predecessor")

    if not verify_server_applied_chain(connection, earlier, alias, identity,
                                       current_model_mapping=mapping):
        raise RuntimeError("Complete prior server history or physical schema is unverified")

    return timeline[-1].number + 1
