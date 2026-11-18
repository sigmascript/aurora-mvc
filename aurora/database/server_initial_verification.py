"""Bounded post-DDL evidence for MySQL/PostgreSQL initial schemas.

Supports only verified single-column create-time foreign keys when an explicit
selected model set is supplied. Existing-table constraint changes remain refused.
"""

from hashlib import sha256
import json
from decimal import Decimal, InvalidOperation
import re
from datetime import date, datetime

from .inspection import inspect_table
from .init_validation import validate_init_models
from .planning import _declared_type, _normalized_declaration
from .schema import _default_literal, _resolved_foreign_keys


_JOURNALS = frozenset({"aurorasql_schema_history", "aurorasql_migration_timeline"})


# PostgreSQL pg_get_expr may spell a constant with a known type cast. Reject
# expressions, arithmetic, nested casts, and casts to unrelated types.
_PG_QUOTED = re.compile(r"'((?:[^']|'')*)'(?:\:\:([a-z ]+))?\Z", re.IGNORECASE)
_PG_NUMBER = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\:\:([a-z ]+))?\Z", re.IGNORECASE)
_PG_BOOLEAN = re.compile(r"(true|false)(?:\:\:([a-z ]+))?\Z", re.IGNORECASE)


def postgres_literal_value(field, observed):
    """Return a validated PostgreSQL constant's literal text or None.

    The result is not a SQL fragment and is never used to generate DDL.
    This parser only establishes catalog evidence for a known model type.
    """
    if not isinstance(observed, str):
        return None
    kind = field.python_type
    if kind in (str, date, datetime):
        match = _PG_QUOTED.fullmatch(observed)
        casts = {str: ('text', 'character varying'),
                 date: ('date',),
                 datetime: ('timestamp', 'timestamp without time zone')}
        if match is None or (match[2] is not None and match[2].lower() not in casts[kind]):
            return None
        return match[1].replace("''", "'")
    if kind is bool:
        match = _PG_BOOLEAN.fullmatch(observed)
        if match is None or (match[2] is not None and match[2].lower() != 'boolean'):
            return None
        return match[1].lower()
    if kind in (int, float) or kind is Decimal:
        match = _PG_NUMBER.fullmatch(observed)
        casts = {int: ('bigint', 'integer', 'smallint'),
                 float: ('double precision', 'real'),
                 Decimal: ('numeric', 'decimal')}
        if match is None or (match[2] is not None and match[2].lower() not in casts[kind]):
            return None
        if kind is int and ('.' in match[1] or Decimal(match[1]) != int(Decimal(match[1]))):
            return None
        return match[1]
    return None


def server_default_matches(field, observed, dialect):
    """Recognize bounded literal defaults, never arbitrary SQL expressions.

    PostgreSQL returns SQL expressions from pg_get_expr; accept only exact
    literal spellings with known casts. MySQL returns normalized values and
    requires separate EXTRA/DEFAULT_GENERATED catalog checks by its caller.
    """
    expected = _default_literal(field, dialect)

    if expected is None:
        return observed is None

    if not isinstance(observed, str) or dialect not in ("mysql", "postgresql"):
        return False

    value = field.default
    if dialect == "postgresql":
        observed = postgres_literal_value(field, observed)
        if observed is None:
            return False

    if type(value) is bool:
        candidates = {"1", "true"} if value else {"0", "false"}
        return observed.lower() in candidates

    if type(value) is int or isinstance(value, Decimal):
        try:
            return Decimal(observed) == Decimal(str(value))
        except InvalidOperation:
            return False

    if type(value) is float:
        try:
            return Decimal(observed) == Decimal(str(value))
        except InvalidOperation:
            return False

    if isinstance(value, (date, datetime)):
        literal = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    elif isinstance(value, str):
        literal = value
    else:
        return False

    if dialect == "mysql":
        return observed == literal

    return observed == literal


def _verify_declared_table(connection, model, models=None):
    """Require ordinary native table metadata equivalent to a model declaration."""
    dialect = connection.dialect
    name = model.__table_name__
    table = inspect_table(connection, name)
    actual = {column.name: column for column in table.columns}
    declared = model.__columns__
    unique = {key for key, field in declared.items() if field.unique and not field.primary_key}
    keys = [key for key, field in declared.items() if field.primary_key]
    expected_fks = _resolved_foreign_keys(model, models) if models is not None else {}
    expected_targets = sorted((source, target, col)
                              for source, (target, col, _, _) in expected_fks.items())
    expected_actions = sorted((source, update, delete)
                              for source, (_, _, update, delete) in expected_fks.items())
    if models is not None:
        if (not table.constraints_verified or table.foreign_key_actions is None
                or list(table.foreign_keys) != expected_targets):
            raise RuntimeError("Initial table foreign-key target differs from its model")
        actual_actions = list(table.foreign_key_actions)
        if dialect == "mysql":
            # MySQL treats NO ACTION as RESTRICT for non-deferrable constraints.
            normalize = lambda action: "RESTRICT" if action == "NO ACTION" else action
            actual_actions = [(source, normalize(up), normalize(down))
                              for source, up, down in actual_actions]
            expected_actions = [(source, normalize(up), normalize(down))
                                for source, up, down in expected_actions]
        if actual_actions != expected_actions:
            raise RuntimeError("Initial table foreign-key actions differ from its model")

    expected_index_count = 1 + len(unique)
    if dialect == "mysql" and expected_fks:
        # MySQL may create a supporting non-unique index for each FK. Verify
        # its exact single source column, never ignore arbitrary extra indexes.
        rows = connection.fetch_all(
            "SELECT INDEX_NAME AS name, COLUMN_NAME AS col, NON_UNIQUE AS non_unique, "
            "SEQ_IN_INDEX AS position FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX", {"table": name})
        groups = {}
        for row in rows:
            groups.setdefault(row["name"], []).append(row)
        extras = [members for index, members in groups.items()
                  if index != "PRIMARY" and members[0]["col"] not in unique]
        if any(len(members) != 1 or members[0]["position"] != 1
               or not members[0]["non_unique"]
               or members[0]["col"] not in expected_fks for members in extras):
            raise RuntimeError("MySQL initial table has unexpected supporting indexes")
        expected_index_count += len(extras)
        if set(table.indexes) != set(groups):
            raise RuntimeError("MySQL initial table index evidence differs")

    if (not table.exists or not table.uniqueness_verified or set(actual) != set(declared)
            or len(keys) != 1 or declared[keys[0]].python_type is not int
            or set(table.unique_columns) != unique
            or len(table.indexes) != expected_index_count
            or any(field.server_default is not None for field in declared.values())
            or (models is None and any(field.references is not None
                                      for field in declared.values()))):
        raise RuntimeError("Initial table has unsupported structure or constraint drift")

    if dialect == "mysql":
        constraints = connection.fetch_all(
            "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table", {"table": name})
        kinds = sorted(item["kind"] for item in constraints)

        if kinds != sorted(["PRIMARY KEY"] + ["UNIQUE"] * len(unique)
                           + ["FOREIGN KEY"] * len(expected_fks)):
            raise RuntimeError("MySQL table has unsupported constraints")

        if connection.fetch_first(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
            {"table": name},
        ) is not None:
            raise RuntimeError("MySQL table has unverified triggers")

        metadata = connection.fetch_all(
            "SELECT COLUMN_NAME AS name, EXTRA AS extra, GENERATION_EXPRESSION AS generation "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": name})

        if {item["name"] for item in metadata} != set(actual) or len(metadata) != len(actual):
            raise RuntimeError("MySQL column catalog is incomplete")

        for item in metadata:
            extra = (item["extra"] or "").lower()

            if item["name"] == keys[0]:
                if extra != "auto_increment":
                    raise RuntimeError("MySQL primary key identity differs")
            elif extra or item["generation"]:
                raise RuntimeError("MySQL generated or unusual column requires review")
    else:
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT count(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal) AS triggers, "
            "(SELECT count(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {"table": name})

        if (metadata is None or metadata["persistence"] != "p" or metadata["partitioned"]
                or metadata["inherited"] or metadata["rls"] or metadata["triggers"] != 0
                or metadata["parents"] != 0):
            raise RuntimeError("PostgreSQL table has unsupported catalog features")

        constraints = connection.fetch_all(
            "SELECT contype AS kind FROM pg_catalog.pg_constraint "
            "WHERE conrelid=to_regclass(:table)", {"table": name})

        if sorted(item["kind"] for item in constraints) != sorted(["p"] + ["u"] * len(unique)
                                                                   + ["f"] * len(expected_fks)):
            raise RuntimeError("PostgreSQL table has unsupported constraints")

        identities = connection.fetch_all(
            "SELECT attname AS name, attidentity AS identity, attgenerated AS generated "
            "FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass(:table) "
            "AND attnum>0 AND NOT attisdropped", {"table": name})

        if {item["name"] for item in identities} != set(actual) or len(identities) != len(actual):
            raise RuntimeError("PostgreSQL column catalog is incomplete")

        for item in identities:
            if item["name"] == keys[0]:
                if item["identity"] != "d" or item["generated"]:
                    raise RuntimeError("PostgreSQL primary key identity differs")
            elif item["identity"] or item["generated"]:
                raise RuntimeError("PostgreSQL generated column requires review")

    for key, field in declared.items():
        observed = actual[key]
        expected_type = _normalized_declaration(_declared_type(field, dialect), dialect)
        observed_type = _normalized_declaration(observed.sql_type, dialect)

        if (observed.primary_key != field.primary_key
                or observed.nullable != (field.nullable and not field.primary_key)
                or expected_type != observed_type
                or not server_default_matches(field, observed.default_sql, dialect)):
            raise RuntimeError("Initial column differs from its validated model")

    return table


def verify_server_declared_schema(connection, models, *, evidence_models=None,
                                  historical_exclusion=None, historical_type_override=None,
                                  historical_nullability_override=None,
                                  historical_default_exclusion=None,
                                  include_observations=False, history_tables=None,
                                  allow_extended=False):
    """Verify all declared server tables, optionally fingerprinting a subset.

    Require no unexpected tables or views, no extra indexes/constraints, and
    the narrow per-table catalog assurances already used by additive safety.
    Subset evidence lets a successor independently check that an earlier
    Initial schema remains unchanged while also checking newly added tables.
    A subset never weakens full physical catalog verification.
    """
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Initial server verification requires MySQL or PostgreSQL")

    ordered = validate_init_models(models, dialect=dialect, allow_extended=allow_extended)
    evidence = ordered if evidence_models is None else validate_init_models(
        evidence_models, dialect=dialect, allow_extended=allow_extended)

    if not evidence or any(model not in ordered for model in evidence):
        raise ValueError("Schema evidence models must be a nonempty subset of verified models")

    if (historical_exclusion is not None and
            (not isinstance(historical_exclusion, tuple) or len(historical_exclusion) != 2
             or historical_exclusion[0] not in {model.__table_name__ for model in evidence})):
        raise ValueError("Historical column exclusion must belong to the verified schema")

    overrides = sum(option is not None for option in
                    (historical_exclusion, historical_type_override,
                     historical_nullability_override, historical_default_exclusion))

    if overrides > 1:
        raise ValueError("Only one historical schema transformation can be verified")

    if include_observations and (overrides or evidence_models is not None):
        raise ValueError("Complete current catalog evidence is required for observations")

    if historical_type_override is not None:
        if (not isinstance(historical_type_override, tuple)
                or len(historical_type_override) != 3
                or historical_type_override[0] not in {model.__table_name__ for model in evidence}
                or not all(isinstance(value, str) for value in historical_type_override)):
            raise ValueError("Historical type evidence must identify a verified column")

    if historical_nullability_override is not None:
        if (not isinstance(historical_nullability_override, tuple)
                or len(historical_nullability_override) != 3
                or historical_nullability_override[0] not in
                {model.__table_name__ for model in evidence}
                or not isinstance(historical_nullability_override[1], str)
                or type(historical_nullability_override[2]) is not bool):
            raise ValueError("Historical nullability evidence must identify a verified column")

    if historical_default_exclusion is not None:
        if (not isinstance(historical_default_exclusion, tuple)
                or len(historical_default_exclusion) != 2
                or historical_default_exclusion[0] not in
                {model.__table_name__ for model in evidence}
                or not isinstance(historical_default_exclusion[1], str)):
            raise ValueError("Historical default evidence must identify a verified column")

    expected = {model.__table_name__ for model in ordered} | (
        _JOURNALS if history_tables is None else set(history_tables))

    if dialect == "mysql":
        objects = connection.fetch_all(
            "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE()"
        )
        names = {row["name"] for row in objects}
        valid_kinds = all(row["kind"] == "BASE TABLE" for row in objects)
    else:
        objects = connection.fetch_all(
            "SELECT c.relname AS name, c.relkind AS kind "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
            "AND c.relkind IN ('r','p','v','m','f')"
        )
        names = {row["name"] for row in objects}
        valid_kinds = all(row["kind"] == "r" for row in objects)

    # Backup catalogs are internal evidence, not application model tables.
    # Recognize them only after validating their exact known definition; never
    # exempt an arbitrary table merely because it uses the reserved name.
    if "__backups__" in names:
        from .server_backup_catalog import _catalog_format
        if _catalog_format(connection) is None:
            raise RuntimeError("Server backup catalog disappeared during verification")
        expected.add("__backups__")

    if not valid_kinds or names != expected or len(objects) != len(expected):
        raise RuntimeError("Server Initial contains unexpected or missing schema objects")

    observations = []
    historical_observations = []
    evidence_tables = {model.__table_name__ for model in evidence}

    for model in ordered:
        table = _verify_declared_table(connection, model, ordered if allow_extended else None)

        if table.name in evidence_tables:
            observed = {
                "table": table.name,
                "columns": [(column.name, column.sql_type, column.nullable,
                             column.primary_key, column.default_sql)
                            for column in table.columns],
                "indexes": list(table.indexes),
                "unique_columns": list(table.unique_columns),
            }
            if allow_extended:
                observed["foreign_keys"] = list(table.foreign_keys)
                observed["foreign_key_actions"] = list(table.foreign_key_actions)
            observations.append(observed)

            if overrides:
                historical = {**observed}

                if historical_exclusion is not None:
                    target, removed = historical_exclusion

                    if table.name == target:
                        if (removed not in {column[0] for column in observed["columns"]}
                                or removed in observed["unique_columns"]):
                            raise RuntimeError("Removed-column historical evidence is invalid")

                        historical["columns"] = [column for column in observed["columns"]
                                                 if column[0] != removed]
                elif historical_type_override is not None:
                    target, changed, previous_type = historical_type_override

                    if table.name == target:
                        matching = [column for column in observed["columns"] if column[0] == changed]

                        if (len(matching) != 1 or changed in observed["unique_columns"]
                                or matching[0][3]):
                            raise RuntimeError("Historical type evidence is not a plain column")

                        # PostgreSQL format_type() uses these long spellings.
                        # Reconstruct the original catalog representation for
                        # its stored digest; the reviewed operation itself
                        # deliberately uses normalized type names.
                        if dialect == "postgresql":
                            current_type = matching[0][1].lower()

                            if (current_type.startswith("character varying(")
                                    and previous_type.startswith("varchar(")):
                                previous_type = "character varying(" + previous_type[len("varchar("):]
                            elif (current_type.startswith("numeric(")
                                  and previous_type.startswith("decimal(")):
                                previous_type = "numeric(" + previous_type[len("decimal("):]

                        historical["columns"] = [
                            (column[0], previous_type, *column[2:]) if column[0] == changed
                            else column for column in observed["columns"]]
                elif historical_nullability_override is not None:
                    target, changed, previous_nullable = historical_nullability_override

                    if table.name == target:
                        matching = [column for column in observed["columns"] if column[0] == changed]

                        if (len(matching) != 1 or changed in observed["unique_columns"]
                                or matching[0][3] or matching[0][2] is previous_nullable):
                            raise RuntimeError("Historical nullability evidence is not a plain transition")

                        historical["columns"] = [
                            (column[0], column[1], previous_nullable, *column[3:])
                            if column[0] == changed else column
                            for column in observed["columns"]]
                else:
                    target, changed = historical_default_exclusion

                    if table.name == target:
                        matching = [column for column in observed["columns"] if column[0] == changed]

                        if (len(matching) != 1 or changed in observed["unique_columns"]
                                or matching[0][3] or matching[0][4] is None):
                            raise RuntimeError("Historical default evidence is not an added literal")

                        historical["columns"] = [
                            (*column[:4], None) if column[0] == changed else column
                            for column in observed["columns"]]

                historical_observations.append(historical)

    payload = json.dumps({"dialect": dialect, "observed": observations},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    current = sha256(payload.encode("utf-8")).hexdigest()

    if not overrides:
        return (current, tuple(observations)) if include_observations else current

    previous_payload = json.dumps({"dialect": dialect, "observed": historical_observations},
                                  sort_keys=True, separators=(",", ":"), allow_nan=False)
    return current, sha256(previous_payload.encode("utf-8")).hexdigest()


def verify_server_initial_schema(connection, models):
    """Verify a newly initialized server schema before marking Initial applied."""
    return verify_server_declared_schema(connection, models)
