"""Read-only validation of native MySQL/PostgreSQL linear migration history.

The legacy per-table journal remains independent evidence. A recorded applied
state is never promoted to a verified current migration without a separate
physical catalog comparison and backend-specific recovery acceptance.
"""

from dataclasses import dataclass
import json
import re

from .inspection import inspect_table
from .model_identity import canonical_model_map
from .schema import _quote


_TABLE = "aurorasql_migration_timeline"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_WIDEN_TYPE = re.compile(r"(?:varchar\([1-9][0-9]{0,4}\)|decimal\([1-9][0-9]?,[0-9]{1,2}\))\Z")
_STATES = frozenset({"prepared", "applied", "uncertain"})


@dataclass(frozen=True)
class ServerMigrationEntry:
    number: int
    name: str
    title: str
    database_alias: str
    review_sha256: str
    model_tables: dict
    schema_sha256: str | None
    status: str
    physical_history_consistent: bool = False
    verified_current: bool = False
    operation: dict | None = None


def server_timeline_ddl(dialect):
    """Describe a new journal; this function does not execute its DDL."""
    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Native server history requires MySQL or PostgreSQL")

    table = _quote(_TABLE, dialect)
    primary = "BIGINT PRIMARY KEY"
    text = "LONGTEXT" if dialect == "mysql" else "TEXT"

    return (
        f"CREATE TABLE {table} ("
        f"migration_id {primary}, "
        "name VARCHAR(80) NOT NULL UNIQUE, "
        "title VARCHAR(160) NOT NULL, "
        "database_alias VARCHAR(80) NOT NULL, "
        "review_sha256 CHAR(64) NOT NULL, "
        f"models_json {text} NOT NULL, "
        f"operation_json {text}, "
        "schema_sha256 CHAR(64), "
        "state VARCHAR(16) NOT NULL, "
        "CHECK (migration_id >= 1), "
        "CHECK (state IN ('prepared', 'applied', 'uncertain'))"
        ")"
    )


def prepare_server_initial(connection, models, alias, review_sha256, title="Initial migration"):
    """Persist Initial intent only inside the caller's backend-specific lock.

    PostgreSQL requires a transaction so the intent rolls back with DDL.
    MySQL requires a held migration lock and stores intent durably before any
    application DDL. This function never marks an operation successful.
    """
    from .model_identity import model_identity_map

    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Native server initialization requires MySQL or PostgreSQL")

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL native history requires an active transaction")
    elif not getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL native history requires a held migration lock")

    if (not isinstance(review_sha256, str) or not _HASH.fullmatch(review_sha256)
            or not isinstance(alias, str) or not alias
            or not isinstance(title, str) or not title.strip() or len(title) > 160):
        raise ValueError("Invalid Initial migration identity or title")

    mapping = canonical_model_map(model_identity_map(models))

    if inspect_table(connection, _TABLE).exists:
        raise RuntimeError("Existing native migration history requires manual review")

    connection.execute_write(server_timeline_ddl(connection.dialect))
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, NULL, NULL, :state)",
        {"number": 1, "name": "Initial", "title": title, "alias": alias,
         "review": review_sha256, "models": mapping, "state": "prepared"},
    )


def mark_server_initial_uncertain(connection, alias):
    """Quarantine a prepared MySQL Initial intent without erasing evidence."""
    if connection.dialect != "mysql" or not getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Uncertain Initial marking requires a held MySQL migration lock")

    if not isinstance(alias, str) or not alias:
        raise ValueError("Database alias is required")

    table = _quote(_TABLE, "mysql")
    affected = connection.execute_write(
        f"UPDATE {table} SET state=:state "
        "WHERE migration_id=:number AND name=:name "
        "AND database_alias=:alias AND state=:before",
        {"state": "uncertain", "number": 1, "name": "Initial", "alias": alias,
         "before": "prepared"},
    )

    if affected != 1:
        raise RuntimeError("Prepared MySQL Initial intent cannot be quarantined")


def complete_server_initial(connection, alias, schema_sha256):
    """Record bounded postcondition evidence inside the held backend scope.

    The caller must obtain schema_sha256 from verified read-only catalog
    inspection. This records applied DDL, not full independent verification.
    """
    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL Initial completion needs an active transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL Initial completion needs a held migration lock")
    else:
        raise ValueError("Unsupported native server history backend")

    if (not isinstance(alias, str) or not alias or
            not isinstance(schema_sha256, str) or not _HASH.fullmatch(schema_sha256)):
        raise ValueError("Verified schema evidence and database alias are required")

    table = _quote(_TABLE, connection.dialect)
    affected = connection.execute_write(
        f"UPDATE {table} SET schema_sha256=:schema, state=:state "
        "WHERE migration_id=:number AND name=:name "
        "AND database_alias=:alias AND state=:before",
        {"schema": schema_sha256, "state": "applied", "number": 1,
         "name": "Initial", "alias": alias, "before": "prepared"},
    )

    if affected != 1:
        raise RuntimeError("Prepared server Initial intent is missing or changed")


def inspect_server_timeline(connection, alias):
    """Read a native chain, refusing malformed or unverified journal metadata.

    The last entry's recorded status cannot establish physical schema equality.
    Callers must not use this inspection as permission to execute another DDL.
    """
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Native server history requires MySQL or PostgreSQL")

    if not isinstance(alias, str) or not alias:
        raise ValueError("Selected migration alias is required")

    observed = inspect_table(connection, _TABLE)

    if not observed.exists:
        return None

    expected = {
        "migration_id", "name", "title", "database_alias", "review_sha256",
        "models_json", "operation_json", "schema_sha256", "state",
    }
    columns = {column.name: column for column in observed.columns}

    if (set(columns) != expected or not columns["migration_id"].primary_key
            or any(column.primary_key for name, column in columns.items()
                   if name != "migration_id")
            or any(column.nullable for name, column in columns.items()
                   if name not in ("schema_sha256", "operation_json"))
            or not observed.uniqueness_verified
            or "name" not in observed.unique_columns):
        raise RuntimeError("Native migration journal structure cannot be trusted")

    table = _quote(_TABLE, connection.dialect)
    rows = connection.fetch_all(
        "SELECT migration_id, name, title, database_alias, review_sha256, "
        f"models_json, operation_json, schema_sha256, state FROM {table} ORDER BY migration_id"
    )

    if not rows:
        raise RuntimeError("Native migration journal exists without Initial migration")

    records = []
    names = set()

    for expected_number, row in enumerate(rows, 1):
        number = row["migration_id"]
        name = row["name"]
        title = row["title"]
        review = row["review_sha256"]
        schema = row["schema_sha256"]
        state = row["state"]

        if (records and records[-1].status != "applied"):
            raise RuntimeError("Native migration history contains a successor after unresolved DDL")

        if (type(number) is not int or number != expected_number
                or not isinstance(name, str) or not name or len(name) > 80
                or (number == 1) != (name == "Initial") or name in names
                or not isinstance(title, str) or not title.strip() or len(title) > 160
                or row["database_alias"] != alias
                or not isinstance(review, str) or not _HASH.fullmatch(review)
                or state not in _STATES
                or (schema is not None and
                    (not isinstance(schema, str) or not _HASH.fullmatch(schema)))
                or (state == "applied" and schema is None)
                or (state == "prepared" and schema is not None)):
            raise RuntimeError("Native migration history is malformed or non-linear")

        mapping = row["models_json"]
        operation_json = row["operation_json"]

        try:
            parsed = json.loads(mapping)

            if canonical_model_map(parsed) != mapping:
                raise ValueError("Noncanonical model mapping")
        except (TypeError, ValueError) as error:
            raise RuntimeError("Native migration model identity is untrusted") from error

        if number == 1:
            if operation_json is not None:
                raise RuntimeError("Initial migration cannot claim a successor operation")

            operation = None
        else:
            try:
                operation = json.loads(operation_json)

                if (not isinstance(operation, dict)
                        or set(operation) not in ({"kind", "table"}, {"kind", "table", "column"},
                                                  {"kind", "table", "column", "from", "to"})
                        or operation.get("kind") not in ("create_table", "add_column",
                                                         "widen_column", "change_nullability",
                                                         "add_default")
                        or (operation["kind"] == "create_table" and "column" in operation)
                        or (operation["kind"] == "add_column"
                            and set(operation) != {"kind", "table", "column"})
                        or (operation["kind"] == "add_default"
                            and set(operation) != {"kind", "table", "column"})
                        or (operation["kind"] == "widen_column"
                            and (set(operation) != {"kind", "table", "column", "from", "to"}
                                 or not isinstance(operation["from"], str)
                                 or not isinstance(operation["to"], str)
                                 or not _WIDEN_TYPE.fullmatch(operation["from"])
                                 or not _WIDEN_TYPE.fullmatch(operation["to"])))
                        or (operation["kind"] == "change_nullability"
                            and (set(operation) != {"kind", "table", "column", "from", "to"}
                                 or type(operation["from"]) is not bool
                                 or type(operation["to"]) is not bool
                                 or operation["from"] is operation["to"]))
                        or canonical_operation_json(operation) != operation_json):
                    raise ValueError("Noncanonical successor operation")

                _quote(operation["table"], connection.dialect)

                if "column" in operation:
                    _quote(operation["column"], connection.dialect)
            except (TypeError, ValueError, KeyError) as error:
                raise RuntimeError("Native successor operation evidence is untrusted") from error

        names.add(name)
        records.append(ServerMigrationEntry(number, name, title, alias, review,
                                            parsed, schema, state, operation=operation))

    return tuple(records)


def canonical_operation_json(operation):
    """Serialize one validated intent without developer-facing format metadata."""
    return json.dumps(operation, sort_keys=True, separators=(",", ":"), allow_nan=False)


def verify_server_initial_current(connection, models, alias):
    """Verify the single native Initial against models, legacy intent and schema.

    This is bounded to the supported initial schema shape. Any successor,
    unresolved journal, unknown catalog object or model drift returns False.
    It never initializes, reconciles or modifies history.
    """
    from hashlib import sha256

    from .history import list_history, review_history
    from .model_identity import model_identity_map
    from .schema import create_table_sql
    from .server_initial_verification import verify_server_initial_schema

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) != 1 or timeline[0].status != "applied":
        return False

    initial = timeline[0]

    if initial.model_tables != model_identity_map(models):
        return False

    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(models):
        return False

    expected = {
        model.__table_name__: sha256(create_table_sql(model, connection.dialect).encode("utf-8")).hexdigest()
        for model in models
    }

    if ({entry.table for entry in history} != set(expected)
            or any(entry.status != "applied" or entry.table not in expected
           or entry.dialect != connection.dialect or entry.fingerprint != expected[entry.table]
                   for entry in history)):
        return False

    # A journal trigger could change history while preserving the table/row
    # shape. Reject these before trusting an applied record for another DDL.
    if connection.dialect == "mysql":
        triggers = connection.fetch_all(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() "
            "AND EVENT_OBJECT_TABLE IN ('aurorasql_schema_history', 'aurorasql_migration_timeline')"
        )
    else:
        triggers = connection.fetch_all(
            "SELECT t.tgname AS name FROM pg_catalog.pg_trigger t "
            "WHERE t.tgrelid IN (to_regclass('aurorasql_schema_history'), "
            "to_regclass('aurorasql_migration_timeline')) AND NOT t.tgisinternal"
        )

    if triggers:
        return False

    try:
        digest = verify_server_initial_schema(connection, models)
    except (ValueError, RuntimeError):
        return False

    return digest == initial.schema_sha256


def verify_server_successor_current(connection, models, alias):
    """Verify one applied new-table or column successor against its catalog.

    A timeline status alone never establishes schema equality. Require both
    historical and current model mappings, legacy per-table journal evidence,
    and the original Initial fingerprint computed while verifying *all* tables.
    """
    from hashlib import sha256

    from .history import list_history, review_history
    from .server_column_history import column_addition_predecessors
    from .server_widening_history import widening_predecessor_models
    from .server_nullability_history import nullability_predecessor_models
    from .server_default_history import default_addition_predecessor_models
    from .model_identity import model_identity_map
    from .schema import create_table_sql
    from .server_model_review import _add_column_statement
    from .server_initial_verification import verify_server_declared_schema

    timeline = inspect_server_timeline(connection, alias)

    if (timeline is None or len(timeline) != 2
            or any(entry.status != "applied" for entry in timeline)):
        return False

    initial, successor = timeline
    current = model_identity_map(models)
    previous = initial.model_tables

    operation = successor.operation

    if (successor.model_tables != current or not isinstance(operation, dict)
            or any(key.startswith("untrusted:") for key in current)):
        return False

    is_addition = operation.get("kind") == "add_column"
    is_widening = operation.get("kind") == "widen_column"
    is_nullability = operation.get("kind") == "change_nullability"
    is_default = operation.get("kind") == "add_default"

    if is_addition:
        if current != previous:
            return False

        try:
            original_models = column_addition_predecessors(models, operation)
            target = next(model for model in models if model.__table_name__ == operation["table"])
            alter = _add_column_statement(target, operation["column"], connection.dialect)
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif is_widening:
        if current != previous:
            return False

        try:
            original_models = widening_predecessor_models(models, operation)
            from .planning import _declared_type, _normalized_declaration
            from .server_column_widening import _DECIMAL, _VARCHAR

            target = next(model for model in models if model.__table_name__ == operation["table"])
            field = target.__columns__[operation["column"]]
            if (_normalized_declaration(_declared_type(field, connection.dialect),
                                        connection.dialect) != operation["to"]):
                return False

            old_v = _VARCHAR.fullmatch(operation["from"])
            new_v = _VARCHAR.fullmatch(operation["to"])
            old_d = _DECIMAL.fullmatch(operation["from"])
            new_d = _DECIMAL.fullmatch(operation["to"])

            if old_v and new_v:
                if int(old_v[1]) >= int(new_v[1]):
                    return False
            elif old_d and new_d:
                old_p, old_s = map(int, old_d.groups())
                new_p, new_s = map(int, new_d.groups())
                if (new_s < old_s or new_p - new_s < old_p - old_s
                        or (new_p, new_s) == (old_p, old_s)):
                    return False
            else:
                return False

            table_name = _quote(operation["table"], connection.dialect)
            column_name = _quote(operation["column"], connection.dialect)
            declared_type = _declared_type(field, connection.dialect)
            if connection.dialect == "postgresql":
                alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE {declared_type}"
            else:
                nullability = " NULL" if field.nullable else " NOT NULL"
                alter = f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} {declared_type}{nullability}"
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif is_nullability:
        if current != previous:
            return False

        try:
            original_models = nullability_predecessor_models(models, operation)
            target = next(model for model in models if model.__table_name__ == operation["table"])
            field = target.__columns__[operation["column"]]
            table_name = _quote(operation["table"], connection.dialect)
            column_name = _quote(operation["column"], connection.dialect)

            if connection.dialect == "postgresql":
                action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
                alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} {action}"
            else:
                from .planning import _declared_type

                nullability = "NULL" if field.nullable else "NOT NULL"
                alter = (f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} "
                         f"{_declared_type(field, connection.dialect)} {nullability}")
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif is_default:
        if current != previous:
            return False

        try:
            original_models = default_addition_predecessor_models(models, operation)
            target = next(model for model in models if model.__table_name__ == operation["table"])
            from .schema import _default_literal

            literal = _default_literal(target.__columns__[operation["column"]], connection.dialect)

            if literal is None:
                return False

            table_name = _quote(operation["table"], connection.dialect)
            column_name = _quote(operation["column"], connection.dialect)
            alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET DEFAULT {literal}"
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif operation.get("kind") == "create_table":
        if (len(current) != len(previous) + 1
                or any(current.get(key) != value for key, value in previous.items())):
            return False

        added = set(current) - set(previous)

        if len(added) != 1 or operation.get("table") != current[next(iter(added))]:
            return False

        original_models = [model for model in models
                           if f"{model.__module__}:{model.__qualname__}" in previous]
    else:
        return False

    if len(original_models) != len(previous):
        return False

    journal = list_history(connection)

    if (review_history(connection, entries=journal,
                       verified_successor_table=operation["table"])
            or len(journal) != len(models) + int(is_addition or is_widening
                                                or is_nullability or is_default)):
        return False

    expected = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode("utf-8")
        ).hexdigest()
        for model in (original_models if (is_addition or is_widening or is_nullability
                                         or is_default) else models)
    }

    if is_addition or is_widening or is_nullability or is_default:
        expected_records = {(table, fingerprint) for table, fingerprint in expected.items()}
        expected_records.add((operation["table"], sha256(alter.encode("utf-8")).hexdigest()))
    else:
        expected_records = {(table, fingerprint) for table, fingerprint in expected.items()}

    if (len(expected_records) != len(journal)
            or {(entry.table, entry.fingerprint) for entry in journal} != expected_records
            or any(entry.status != "applied" or entry.dialect != connection.dialect
                   for entry in journal)):
        return False

    if connection.dialect == "mysql":
        triggers = connection.fetch_all(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() "
            "AND EVENT_OBJECT_TABLE IN ('aurorasql_schema_history', 'aurorasql_migration_timeline')"
        )
    elif connection.dialect == "postgresql":
        triggers = connection.fetch_all(
            "SELECT t.tgname AS name FROM pg_catalog.pg_trigger t "
            "WHERE t.tgrelid IN (to_regclass('aurorasql_schema_history'), "
            "to_regclass('aurorasql_migration_timeline')) AND NOT t.tgisinternal"
        )
    else:
        return False

    if triggers:
        return False

    try:
        if is_addition:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection, models,
                historical_exclusion=(operation["table"], operation["column"]))
        elif is_widening:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection, models, historical_type_override=(operation["table"],
                                                             operation["column"], operation["from"]))
        elif is_nullability:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection, models, historical_nullability_override=(operation["table"],
                                                                     operation["column"],
                                                                     operation["from"]))
        elif is_default:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection, models, historical_default_exclusion=(operation["table"],
                                                                  operation["column"]))
        else:
            initial_digest = verify_server_declared_schema(
                connection, models, evidence_models=original_models)
            successor_digest = verify_server_declared_schema(connection, models)
    except (ValueError, RuntimeError):
        return False

    return (initial_digest == initial.schema_sha256
            and successor_digest == successor.schema_sha256)


def _reviewed_server_successor_number(connection, models, alias, identity, review,
                                      initial_predecessor_verifier):
    """Choose the next sequence only after complete prior-history verification.

    The first successor retains its original verified Initial proof. Later
    successors must verify the whole applied chain and physical predecessor.
    """
    from .model_identity import model_identity_map

    timeline = inspect_server_timeline(connection, alias)

    if not timeline or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied server history is required")

    if len(timeline) == 1:
        if timeline[0].model_tables != model_identity_map(models):
            raise RuntimeError("Verified original model history is required")

        initial_predecessor_verifier(connection, models, alias, review)
        return 2

    from .server_history_chain import verify_server_review_predecessor

    return verify_server_review_predecessor(connection, models, alias, identity, review)


def prepare_server_successor(connection, models, alias, identity, review, approval,
                             name, title="Untitled"):
    """Prepare one new-table successor after independently verified Initial.

    The reviewed proposal must be reconstructed under the caller's lock. This
    primitive never runs application DDL or marks a successor applied.
    """
    import hmac

    from .model_identity import model_identity_map
    from .server_model_review import review_server_model_changes

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL successor requires an active transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL successor requires a held migration lock")
    else:
        raise ValueError("Unsupported native server successor backend")

    if (not isinstance(name, str) or not name or name == "Initial" or len(name) > 80
            or not isinstance(title, str) or not title.strip() or len(title) > 160):
        raise ValueError("Successor migration name or title is invalid")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("A complete applied native history is required")

    previous = timeline[-1].model_tables
    mapping = model_identity_map(models)

    if any(reference.startswith("untrusted:") for reference in mapping):
        raise ValueError("Server successors require stable model identities")

    before = [model for model in models
              if f"{model.__module__}:{model.__qualname__}" in previous]
    added = set(mapping) - set(previous)

    if (len(added) != 1 or any(mapping.get(key) != value for key, value in previous.items())
            or (len(timeline) == 1 and not verify_server_initial_current(connection, before, alias))):
        raise RuntimeError("Historical model mapping or Initial schema is unverified")

    new_model = next(model for model in models
                     if f"{model.__module__}:{model.__qualname__}" in added)
    keys = [field for field in new_model.__columns__.values() if field.primary_key]

    if (len(keys) != 1 or keys[0].python_type is not int
            or any(field.references is not None or field.server_default is not None
                   for field in new_model.__columns__.values())):
        raise ValueError("New server table needs separately verified key/constraint semantics")

    candidate = review_server_model_changes(connection, models, alias, identity)

    if (len(candidate.operations) != 1
            or candidate.operations[0]["kind"] != "create_table"
            or candidate.operations[0]["table"] != mapping[next(iter(added))]
            or candidate != review
            or not isinstance(approval, str)
            or not hmac.compare_digest(candidate.review_sha256, approval)):
        raise PermissionError("Exact reviewed new-table successor approval is required")

    if len(timeline) == 1:
        number = 2
    else:
        from .server_history_chain import verify_server_review_predecessor

        number = verify_server_review_predecessor(
            connection, models, alias, identity, candidate)

    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {"number": number, "name": name, "title": title, "alias": alias,
         "review": candidate.review_sha256, "models": canonical_model_map(mapping),
         "operation": canonical_operation_json(candidate.operations[0]),
         "state": "prepared"},
    )

    return number


def prepare_server_column_successor(connection, models, alias, identity, review,
                                    approval, name, title="Untitled"):
    """Record one reviewed column addition only after historical proof under lock.

    This function does not execute application ALTER TABLE. Existing table
    additions must have trusted Initial model identity and physical evidence.
    """
    import hmac

    from .model_identity import model_identity_map
    from .server_column_history import verify_server_column_addition_predecessor
    from .server_model_review import review_server_model_changes

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL column successor requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL column successor requires a held lock")
    else:
        raise ValueError("Unsupported server column successor backend")

    if (not isinstance(name, str) or not name or name == "Initial" or len(name) > 80
            or not isinstance(title, str) or not title.strip() or len(title) > 160):
        raise ValueError("Invalid column successor name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied history is required for column addition")

    mapping = model_identity_map(models)

    if any(key.startswith("untrusted:") for key in mapping):
        raise RuntimeError("Historical model identities cannot authorize column addition")

    candidate = review_server_model_changes(connection, models, alias, identity)

    if (candidate != review or not isinstance(approval, str)
            or not hmac.compare_digest(candidate.review_sha256, approval)
            or len(candidate.operations) != 1
            or candidate.operations[0]["kind"] != "add_column"):
        raise PermissionError("Exact reviewed column addition approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate,
        verify_server_column_addition_predecessor)
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {"number": number, "name": name, "title": title, "alias": alias,
         "review": candidate.review_sha256, "models": canonical_model_map(mapping),
         "operation": canonical_operation_json(candidate.operations[0]),
         "state": "prepared"},
    )

    return number


def prepare_server_widening_successor(connection, models, alias, identity, review,
                                     approval, name, title="Untitled"):
    """Prepare exactly one history-verified widening under backend coordination.

    No ALTER is executed here. The proposal is reconstructed inside the held
    transaction/lock so a stale read-only review cannot authorize DDL.
    """
    import hmac

    from .model_identity import model_identity_map
    from .server_column_widening import review_server_column_widening
    from .server_widening_history import verify_server_widening_predecessor

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL widening requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL widening requires a held lock")
    else:
        raise ValueError("Unsupported widening backend")

    if (not isinstance(name, str) or not name or name == "Initial" or len(name) > 80
            or not isinstance(title, str) or not title.strip() or len(title) > 160):
        raise ValueError("Invalid widening migration name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied model history is required")

    candidate = review_server_column_widening(connection, models, alias, identity)

    if (candidate != review or not isinstance(approval, str)
            or not hmac.compare_digest(candidate.review_sha256, approval)
            or len(candidate.operations) != 1
            or candidate.operations[0]["kind"] != "widen_column"):
        raise PermissionError("Exact reviewed widening approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate,
        verify_server_widening_predecessor)
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {"number": number, "name": name, "title": title, "alias": alias,
         "review": candidate.review_sha256,
         "models": canonical_model_map(model_identity_map(models)),
         "operation": canonical_operation_json(candidate.operations[0]),
         "state": "prepared"},
    )
    return number


def prepare_server_nullability_successor(connection, models, alias, identity, review,
                                        approval, name, title="Untitled"):
    """Prepare one historically proven nullable-flag edit under a backend lock.

    The reviewer repeats its bounded NULL-row probe under the held scope.
    This operation records intent; it never executes ALTER TABLE itself.
    """
    import hmac

    from .model_identity import model_identity_map
    from .server_nullability_review import review_server_nullability_change
    from .server_nullability_history import verify_server_nullability_predecessor

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL nullability requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL nullability requires a held migration lock")
    else:
        raise ValueError("Unsupported nullability backend")

    if (not isinstance(name, str) or not name or name == "Initial" or len(name) > 80
            or not isinstance(title, str) or not title.strip() or len(title) > 160):
        raise ValueError("Invalid nullability migration name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied model history is required")

    candidate = review_server_nullability_change(connection, models, alias, identity)

    if (candidate != review or not isinstance(approval, str)
            or not hmac.compare_digest(candidate.review_sha256, approval)
            or len(candidate.operations) != 1
            or candidate.operations[0]["kind"] != "change_nullability"):
        raise PermissionError("Exact reviewed nullability approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate,
        verify_server_nullability_predecessor)
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {"number": number, "name": name, "title": title, "alias": alias,
         "review": candidate.review_sha256,
         "models": canonical_model_map(model_identity_map(models)),
         "operation": canonical_operation_json(candidate.operations[0]),
         "state": "prepared"},
    )
    return number


def prepare_server_default_successor(connection, models, alias, identity, review,
                                    approval, name, title="Untitled"):
    """Record one historically proven default addition under backend coordination."""
    import hmac

    from .model_identity import model_identity_map
    from .server_default_review import review_server_default_addition
    from .server_default_history import verify_server_default_predecessor

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL default addition requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL default addition requires a held migration lock")
    else:
        raise ValueError("Unsupported default addition backend")

    if (not isinstance(name, str) or not name or name == "Initial" or len(name) > 80
            or not isinstance(title, str) or not title.strip() or len(title) > 160):
        raise ValueError("Invalid default migration name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied model history is required")

    candidate = review_server_default_addition(connection, models, alias, identity)

    if (candidate != review or not isinstance(approval, str)
            or not hmac.compare_digest(candidate.review_sha256, approval)
            or len(candidate.operations) != 1
            or candidate.operations[0]["kind"] != "add_default"):
        raise PermissionError("Exact reviewed default approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate,
        verify_server_default_predecessor)
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {"number": number, "name": name, "title": title, "alias": alias,
         "review": candidate.review_sha256,
         "models": canonical_model_map(model_identity_map(models)),
         "operation": canonical_operation_json(candidate.operations[0]),
         "state": "prepared"},
    )
    return number


def complete_server_successor(connection, number, name, alias, schema_sha256):
    """Record an independently verified successor inside the held backend scope."""
    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL successor completion requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL successor completion requires a migration lock")
    else:
        raise ValueError("Unsupported native server successor backend")

    if (type(number) is not int or number < 2 or not isinstance(name, str) or not name
            or not isinstance(alias, str) or not alias
            or not isinstance(schema_sha256, str) or not _HASH.fullmatch(schema_sha256)):
        raise ValueError("Invalid reviewed successor identity or schema evidence")

    table = _quote(_TABLE, connection.dialect)
    affected = connection.execute_write(
        f"UPDATE {table} SET schema_sha256=:schema, state=:state "
        "WHERE migration_id=:number AND name=:name AND database_alias=:alias AND state=:before",
        {"schema": schema_sha256, "state": "applied", "number": number,
         "name": name, "alias": alias, "before": "prepared"},
    )

    if affected != 1:
        raise RuntimeError("Prepared server successor is missing or changed")


def mark_server_successor_uncertain(connection, number, name, alias):
    """Preserve MySQL uncertainty after implicit-commit successor DDL."""
    if connection.dialect != "mysql" or not getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Uncertain successor requires a held MySQL migration lock")

    if (type(number) is not int or number < 2 or not isinstance(name, str) or not name
            or not isinstance(alias, str) or not alias):
        raise ValueError("Invalid successor identity")

    table = _quote(_TABLE, "mysql")
    affected = connection.execute_write(
        f"UPDATE {table} SET state=:state "
        "WHERE migration_id=:number AND name=:name AND database_alias=:alias AND state=:before",
        {"state": "uncertain", "number": number, "name": name,
         "alias": alias, "before": "prepared"},
    )

    if affected != 1:
        raise RuntimeError("Prepared MySQL successor cannot be quarantined")
