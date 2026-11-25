"""Guarded SQLite Initial record and read-only physical-schema verification.

This supplements, rather than replaces, the historical per-table journal.
Legacy databases must not be adopted or rewritten merely by reading history.
"""

import json
import re
from dataclasses import dataclass
from hashlib import sha256

from .migration_journal import list_intents, _require_journal, _validate_intent
from .schema import create_table_sql, _quote
from .schema import _quote as _server_quote
from .model_discovery import canonical_model_map, model_identity_map


_TABLE = "aurorasql_migration_timeline"
_TABLE_DDL = (
    f'CREATE TABLE "{_TABLE}" ('
    "migration_id INTEGER PRIMARY KEY CHECK(migration_id >= 1), "
    "name TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
    "database_alias TEXT NOT NULL, review_sha256 TEXT NOT NULL, "
    "models_json TEXT NOT NULL, "
    "schema_sha256 TEXT, state TEXT NOT NULL "
    "CHECK(state IN ('prepared','applied','uncertain')), "
    "CHECK((migration_id = 1 AND name = 'Initial') OR "
    "(migration_id > 1 AND name != 'Initial')))"
)
_JOURNALS = frozenset(
    {
        _TABLE,
        "aurorasql_schema_history",
        "aurorasql_migration_intents_v2",
        "aurorasql_additive_history_v1",
    }
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _schema_digest(raw):
    """Fingerprint catalog objects without reading application row contents."""
    rows = raw.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
    ).fetchall()
    catalog = [list(row) for row in rows if row[1] not in _JOURNALS]
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _initial_review(raw, models, alias):
    identity = raw.execute("PRAGMA database_list").fetchall()
    main = [row[2] for row in identity if row[1] == "main"]

    if len(main) != 1 or not main[0]:
        raise RuntimeError("Initial migration requires a named SQLite database")

    if not isinstance(alias, str) or not alias:
        raise ValueError("Database alias is required")

    payload = json.dumps(
        {
            "database": main[0],
            "alias": alias,
            "ddl": [create_table_sql(model, "sqlite") for model in models],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def prepare_initial(raw, models, alias, title="Initial migration"):
    """Write an immutable prepared Initial intent inside the caller's lock."""
    if not raw.in_transaction:
        raise RuntimeError("Initial migration requires an active locked transaction")

    if raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone():
        raise RuntimeError("Initial migration already exists; manual review required")

    review = _initial_review(raw, models, alias)
    mapping = canonical_model_map(model_identity_map(models))
    raw.execute(_TABLE_DDL)
    raw.execute(
        f'INSERT INTO "{_TABLE}" VALUES (1, ?, ?, ?, ?, ?, NULL, ?)',
        ("Initial", title, alias, review, mapping, "prepared"),
    )
    return review


def complete_initial(raw):
    """Mark Initial applied only after the caller verifies all schema effects."""
    if not raw.in_transaction:
        raise RuntimeError("Initial completion requires an active locked transaction")

    digest = _schema_digest(raw)
    updated = raw.execute(
        f'UPDATE "{_TABLE}" SET schema_sha256=?, state=? '
        "WHERE migration_id=1 AND state='prepared'",
        (digest, "applied"),
    ).rowcount

    if updated != 1:
        raise RuntimeError("Initial intent is missing or changed")


def inspect_timeline(raw, alias, *, legacy_findings=()):
    """Inspect every linear entry and the corresponding intent journal."""
    found = raw.execute("SELECT type, sql FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()

    if found is None:
        return None

    if found[0] != "table" or found[1] != _TABLE_DDL:
        raise RuntimeError("Initial timeline table definition is unrecognized")

    columns = raw.execute(f'PRAGMA table_info("{_TABLE}")').fetchall()

    if [column[1] for column in columns] != [
        "migration_id",
        "name",
        "title",
        "database_alias",
        "review_sha256",
        "models_json",
        "schema_sha256",
        "state",
    ] or columns[0][5] != 1:
        raise RuntimeError("Initial timeline schema is not recognized")

    rows = raw.execute(f'SELECT * FROM "{_TABLE}" ORDER BY migration_id').fetchall()

    if not rows:
        raise RuntimeError("Initial timeline is empty")

    records = []

    for expected_number, row in enumerate(rows, start=1):
        number, name, title, recorded_alias, review, models_json, schema, state = row

        try:
            model_mapping = json.loads(models_json)

            if canonical_model_map(model_mapping) != models_json:
                raise ValueError("Noncanonical model identity history")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Migration model identity history is invalid") from error

        if (
            number != expected_number
            or (number == 1) != (name == "Initial")
            or not isinstance(name, str)
            or not name
            or recorded_alias != alias
            or not isinstance(title, str)
            or not title
            or len(title) > 160
            or not isinstance(review, str)
            or not _HASH.fullmatch(review)
            or state not in ("prepared", "applied", "uncertain")
            or (schema is not None and (not isinstance(schema, str) or not _HASH.fullmatch(schema)))
        ):
            raise RuntimeError(
                "Migration timeline is non-linear, invalid, or belongs to another alias"
            )

        records.append(
            {
                "number": number,
                "name": name,
                "title": title,
                "status": state,
                "review_sha256": review,
                "schema_sha256": schema,
                "model_tables": model_mapping,
            }
        )

    matches = records[-1]["status"] == "applied" and records[-1]["schema_sha256"] == _schema_digest(
        raw
    )

    # A later v2 intent cannot be treated as part of Initial's applied chain.
    # Keep it as separate evidence until a verified successor is integrated.
    intent_table = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='aurorasql_migration_intents_v2'"
    ).fetchone()
    unintegrated = False

    if intent_table:
        if raw.in_transaction:
            _require_journal(raw)
            intent_rows = raw.execute(
                "SELECT migration_id,review_sha256,before_sha256,after_sha256,state "
                'FROM "aurorasql_migration_intents_v2"'
            ).fetchall()

            for row in intent_rows:
                _validate_intent(*row[:4])

            recorded = {row[0]: (row[1], row[2], row[3], row[4]) for row in intent_rows}
        else:
            intents = list_intents(raw)
            recorded = {
                entry.migration_id: (
                    entry.review_sha256,
                    entry.before_sha256,
                    entry.after_sha256,
                    entry.state,
                )
                for entry in intents
            }

        successors = {entry["name"]: entry for entry in records[1:]}
        unintegrated = (
            set(recorded) != set(successors)
            or any(record[3] != "applied" for record in recorded.values())
            or any(
                recorded[name][0] != successor["review_sha256"]
                for name, successor in successors.items()
                if name in recorded
            )
        )

        previous_after = None

        for successor in records[1:]:
            intent = recorded.get(successor["name"])

            if intent is None:
                break

            if previous_after is not None and intent[1] != previous_after:
                unintegrated = True

            previous_after = intent[2]
    else:
        # Fresh native Initial always creates the v2 intent table. Its absence
        # is evidence of catalog damage, even before any successor exists.
        unintegrated = True

    consistent = bool(
        matches
        and not legacy_findings
        and not unintegrated
        and all(entry["status"] == "applied" for entry in records)
    )

    for entry in records:
        entry["physical_schema_matches"] = matches if entry is records[-1] else None
        entry["unintegrated_successor_intents"] = unintegrated
        entry["physical_history_consistent"] = consistent
        entry["verified_current"] = False

    return records


def inspect_initial(raw, alias, *, legacy_findings=()):
    """Compatibility view of the first record; not a full timeline report."""
    timeline = inspect_timeline(raw, alias, legacy_findings=legacy_findings)
    return timeline[0] if timeline is not None else None


def historical_table_aliases(timeline):
    """Resolve original table names through stable recorded model identities."""
    if not timeline:
        return {}

    first = timeline[0]["model_tables"]
    latest = timeline[-1]["model_tables"]
    aliases = {}

    for reference, original in first.items():
        current = latest.get(reference)

        if current and current != original:
            if reference.startswith("untrusted:"):
                raise RuntimeError("Untrusted model identity cannot reconcile table rename")

            aliases[original] = current

    if len(set(aliases.values())) != len(aliases):
        raise RuntimeError("Ambiguous table rename history")

    return aliases


def inspect_native_history(connection, alias):
    """Check the linear chain and original table evidence without rewriting it."""
    from .migration_history import list_history, review_history

    preliminary = inspect_timeline(connection._raw, alias)

    if preliminary is None:
        return None, (), ()

    entries = list_history(connection)
    aliases = historical_table_aliases(preliminary)
    findings = review_history(connection, entries=entries, table_aliases=aliases)
    return inspect_timeline(connection._raw, alias, legacy_findings=findings), entries, findings


def prepare_successor(raw, alias, name, review, models, title="Untitled"):
    """Append exactly one reviewed successor in the caller's SQLite transaction."""
    return prepare_successor_mapping(raw, alias, name, review, model_identity_map(models), title)


def prepare_successor_mapping(raw, alias, name, review, model_mapping, title="Untitled"):
    """Append a successor carrying forward an independently verified model map."""
    if not raw.in_transaction:
        raise RuntimeError("Successor migration requires a locked transaction")

    if not isinstance(name, str) or not name or name == "Initial":
        raise ValueError("Successor migration name is invalid")

    if not isinstance(review, str) or not _HASH.fullmatch(review):
        raise ValueError("Successor review fingerprint is invalid")

    chain = inspect_timeline(raw, alias)

    if not chain or not chain[-1]["physical_history_consistent"]:
        raise RuntimeError("Unverified migration history blocks successor")

    number = chain[-1]["number"] + 1
    mapping = canonical_model_map(model_mapping)
    if not isinstance(title, str) or not title or len(title) > 160:
        raise ValueError("Invalid migration title")

    raw.execute(
        f'INSERT INTO "{_TABLE}" VALUES (?, ?, ?, ?, ?, ?, NULL, ?)',
        (number, name, title, alias, review, mapping, "prepared"),
    )
    return number


def complete_successor(raw, number, name):
    """Persist the verified post-DDL schema in the same SQLite transaction."""
    if not raw.in_transaction:
        raise RuntimeError("Successor completion requires a locked transaction")

    updated = raw.execute(
        f'UPDATE "{_TABLE}" SET schema_sha256=?, state=? '
        "WHERE migration_id=? AND name=? AND state='prepared'",
        (_schema_digest(raw), "applied", number, name),
    ).rowcount

    if updated != 1:
        raise RuntimeError("Successor journal transition failed")


def verify_initial_models(raw, models, initial, legacy_entries):
    """Compare complete SQLite DDL and recorded table intents without writes."""
    from .schema import validate_init_models

    if not initial["physical_history_consistent"]:
        return False

    if initial["model_tables"] != model_identity_map(models):
        return False

    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        return False

    ordered = validate_init_models(models, dialect="sqlite")
    expected_tables = {
        model.__table_name__: sha256(create_table_sql(model, "sqlite").encode("utf-8")).hexdigest()
        for model in ordered
    }

    if (
        len(legacy_entries) != len(expected_tables)
        or {entry.table: entry.fingerprint for entry in legacy_entries} != expected_tables
        or any(entry.status != "applied" for entry in legacy_entries)
    ):
        return False

    # Build only an in-memory catalog; no model data, database attachment,
    # migration execution, or writes to the inspected physical database.
    import sqlite3

    with sqlite3.connect(":memory:") as expected:
        expected.execute("PRAGMA foreign_keys=ON")

        for model in ordered:
            expected.execute(create_table_sql(model, "sqlite"))

        return _schema_digest(expected) == _schema_digest(raw)


def verify_current_models(raw, models, timeline, legacy_entries):
    """Verify current declarations and history; refuse unproven succession."""
    from .migration_review import snapshot_fingerprint
    from .schema import validate_init_models
    from .snapshots import schema_snapshot

    if not timeline or not timeline[-1]["physical_history_consistent"]:
        return False

    if timeline[-1]["model_tables"] != model_identity_map(models):
        return False

    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        return False

    if len(timeline) == 1:
        return verify_initial_models(raw, models, timeline[0], legacy_entries)

    if not legacy_entries or any(entry.status != "applied" for entry in legacy_entries):
        return False

    ordered = validate_init_models(models, dialect="sqlite")
    aliases = historical_table_aliases(timeline)
    initial_tables = [aliases.get(entry.table, entry.table) for entry in legacy_entries]

    if len(set(initial_tables)) != len(initial_tables) or not set(initial_tables).issubset(
        {model.__table_name__ for model in ordered}
    ):
        return False

    intents = {entry.migration_id: entry for entry in list_intents(raw)}
    latest = intents.get(timeline[-1]["name"])

    from .schema_inspection import _catalog, _catalog_digest

    expected_sha = (
        _catalog_digest(_catalog(raw))
        if timeline[-1]["name"].startswith(("schema_", "edit_"))
        else snapshot_fingerprint(schema_snapshot(*ordered, dialect="sqlite"))
    )

    if latest is None or latest.after_sha256 != expected_sha:
        return False

    # ALTER TABLE SQL text is not identical to fresh CREATE TABLE SQL.
    # Compare actual columns, defaults, keys, indexes and FKs in isolation.
    import sqlite3

    from .migration_execution import _verify_result_catalog

    with sqlite3.connect(":memory:") as expected:
        expected.execute("PRAGMA foreign_keys=ON")

        for model in ordered:
            expected.execute(create_table_sql(model, "sqlite"))

        try:
            _verify_result_catalog(
                raw, expected, {model.__table_name__ for model in ordered}, native=True
            )
        except RuntimeError:
            return False

    return True


# Native MySQL/PostgreSQL read-only history inspection. Its native intent
# transitions are owned below.

_SERVER_TABLE = "aurorasql_migration_timeline"
_SERVER_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SERVER_WIDEN_TYPE = re.compile(
    r"(?:varchar\([1-9][0-9]{0,4}\)|decimal\([1-9][0-9]?,[0-9]{1,2}\))\Z"
)
_SERVER_STATES = frozenset({"prepared", "applied", "uncertain"})


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


def canonical_operation_json(operation):
    """Serialize one validated intent without developer-facing format metadata."""
    return json.dumps(operation, sort_keys=True, separators=(",", ":"), allow_nan=False)


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


def inspect_server_timeline(connection, alias):
    """Read a native chain, refusing malformed or unverified journal metadata.

    The last entry's recorded status cannot establish physical schema equality.
    Callers must not use this inspection as permission to execute another DDL.
    """
    from .schema_inspection import inspect_table

    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Native server history requires MySQL or PostgreSQL")

    if not isinstance(alias, str) or not alias:
        raise ValueError("Selected migration alias is required")

    observed = inspect_table(connection, _SERVER_TABLE)

    if not observed.exists:
        return None

    expected = {
        "migration_id",
        "name",
        "title",
        "database_alias",
        "review_sha256",
        "models_json",
        "operation_json",
        "schema_sha256",
        "state",
    }
    columns = {column.name: column for column in observed.columns}

    if (
        set(columns) != expected
        or not columns["migration_id"].primary_key
        or any(column.primary_key for name, column in columns.items() if name != "migration_id")
        or any(
            column.nullable
            for name, column in columns.items()
            if name not in ("schema_sha256", "operation_json")
        )
        or not observed.uniqueness_verified
        or "name" not in observed.unique_columns
    ):
        raise RuntimeError("Native migration journal structure cannot be trusted")

    table = _server_quote(_SERVER_TABLE, connection.dialect)
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

        if records and records[-1].status != "applied":
            raise RuntimeError("Native migration history contains a successor after unresolved DDL")

        if (
            type(number) is not int
            or number != expected_number
            or not isinstance(name, str)
            or not name
            or len(name) > 80
            or (number == 1) != (name == "Initial")
            or name in names
            or not isinstance(title, str)
            or not title.strip()
            or len(title) > 160
            or row["database_alias"] != alias
            or not isinstance(review, str)
            or not _SERVER_HASH.fullmatch(review)
            or state not in _SERVER_STATES
            or (
                schema is not None
                and (not isinstance(schema, str) or not _SERVER_HASH.fullmatch(schema))
            )
            or (state == "applied" and schema is None)
            or (state == "prepared" and schema is not None)
        ):
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

                if (
                    not isinstance(operation, dict)
                    or set(operation)
                    not in (
                        {"kind", "table"},
                        {"kind", "table", "column"},
                        {"kind", "table", "column", "from", "to"},
                    )
                    or operation.get("kind")
                    not in (
                        "create_table",
                        "add_column",
                        "widen_column",
                        "change_nullability",
                        "add_default",
                    )
                    or (operation["kind"] == "create_table" and "column" in operation)
                    or (
                        operation["kind"] == "add_column"
                        and set(operation) != {"kind", "table", "column"}
                    )
                    or (
                        operation["kind"] == "add_default"
                        and set(operation) != {"kind", "table", "column"}
                    )
                    or (
                        operation["kind"] == "widen_column"
                        and (
                            set(operation) != {"kind", "table", "column", "from", "to"}
                            or not isinstance(operation["from"], str)
                            or not isinstance(operation["to"], str)
                            or not _SERVER_WIDEN_TYPE.fullmatch(operation["from"])
                            or not _SERVER_WIDEN_TYPE.fullmatch(operation["to"])
                        )
                    )
                    or (
                        operation["kind"] == "change_nullability"
                        and (
                            set(operation) != {"kind", "table", "column", "from", "to"}
                            or type(operation["from"]) is not bool
                            or type(operation["to"]) is not bool
                            or operation["from"] is operation["to"]
                        )
                    )
                    or canonical_operation_json(operation) != operation_json
                ):
                    raise ValueError("Noncanonical successor operation")

                _server_quote(operation["table"], connection.dialect)

                if "column" in operation:
                    _server_quote(operation["column"], connection.dialect)
            except (TypeError, ValueError, KeyError) as error:
                raise RuntimeError("Native successor operation evidence is untrusted") from error

        names.add(name)
        records.append(
            ServerMigrationEntry(
                number, name, title, alias, review, parsed, schema, state, operation=operation
            )
        )

    return tuple(records)


# Native MySQL/PostgreSQL state transitions (distinct from SQLite intent history).


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
        {
            "state": "uncertain",
            "number": 1,
            "name": "Initial",
            "alias": alias,
            "before": "prepared",
        },
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

    if (
        not isinstance(alias, str)
        or not alias
        or not isinstance(schema_sha256, str)
        or not _HASH.fullmatch(schema_sha256)
    ):
        raise ValueError("Verified schema evidence and database alias are required")

    table = _quote(_TABLE, connection.dialect)
    affected = connection.execute_write(
        f"UPDATE {table} SET schema_sha256=:schema, state=:state "
        "WHERE migration_id=:number AND name=:name "
        "AND database_alias=:alias AND state=:before",
        {
            "schema": schema_sha256,
            "state": "applied",
            "number": 1,
            "name": "Initial",
            "alias": alias,
            "before": "prepared",
        },
    )

    if affected != 1:
        raise RuntimeError("Prepared server Initial intent is missing or changed")


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

    if (
        type(number) is not int
        or number < 2
        or not isinstance(name, str)
        or not name
        or not isinstance(alias, str)
        or not alias
        or not isinstance(schema_sha256, str)
        or not _HASH.fullmatch(schema_sha256)
    ):
        raise ValueError("Invalid reviewed successor identity or schema evidence")

    table = _quote(_TABLE, connection.dialect)
    affected = connection.execute_write(
        f"UPDATE {table} SET schema_sha256=:schema, state=:state "
        "WHERE migration_id=:number AND name=:name AND database_alias=:alias AND state=:before",
        {
            "schema": schema_sha256,
            "state": "applied",
            "number": number,
            "name": name,
            "alias": alias,
            "before": "prepared",
        },
    )

    if affected != 1:
        raise RuntimeError("Prepared server successor is missing or changed")


def mark_server_successor_uncertain(connection, number, name, alias):
    """Preserve MySQL uncertainty after implicit-commit successor DDL."""
    if connection.dialect != "mysql" or not getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Uncertain successor requires a held MySQL migration lock")

    if (
        type(number) is not int
        or number < 2
        or not isinstance(name, str)
        or not name
        or not isinstance(alias, str)
        or not alias
    ):
        raise ValueError("Invalid successor identity")

    table = _quote(_TABLE, "mysql")
    affected = connection.execute_write(
        f"UPDATE {table} SET state=:state "
        "WHERE migration_id=:number AND name=:name AND database_alias=:alias AND state=:before",
        {
            "state": "uncertain",
            "number": number,
            "name": name,
            "alias": alias,
            "before": "prepared",
        },
    )

    if affected != 1:
        raise RuntimeError("Prepared MySQL successor cannot be quarantined")


# Native MySQL/PostgreSQL prepared-intent transitions. Review/inspection
# dependencies resolve on invocation to avoid the timeline/review import cycle.
_SERVER_PREPARE_HASH = re.compile(r"[0-9a-f]{64}\Z")


def prepare_server_initial(connection, models, alias, review_sha256, title="Initial migration"):
    """Persist Initial intent only inside the caller's backend-specific lock.

    PostgreSQL requires a transaction so the intent rolls back with DDL.
    MySQL requires a held migration lock and stores intent durably before any
    application DDL. This function never marks an operation successful.
    """
    from .model_discovery import model_identity_map
    from .schema_inspection import inspect_table

    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Native server initialization requires MySQL or PostgreSQL")

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL native history requires an active transaction")
    elif not getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL native history requires a held migration lock")

    if (
        not isinstance(review_sha256, str)
        or not _SERVER_PREPARE_HASH.fullmatch(review_sha256)
        or not isinstance(alias, str)
        or not alias
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
    ):
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
        {
            "number": 1,
            "name": "Initial",
            "title": title,
            "alias": alias,
            "review": review_sha256,
            "models": mapping,
            "state": "prepared",
        },
    )


def prepare_server_successor(
    connection, models, alias, identity, review, approval, name, title="Untitled"
):
    """Prepare one new-table successor after independently verified Initial.

    The reviewed proposal must be reconstructed under the caller's lock. This
    primitive never runs application DDL or marks a successor applied.
    """
    import hmac

    from .migration_review import (
        review_server_model_changes,
        verify_server_initial_current,
    )
    from .model_discovery import model_identity_map

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL successor requires an active transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL successor requires a held migration lock")
    else:
        raise ValueError("Unsupported native server successor backend")

    if (
        not isinstance(name, str)
        or not name
        or name == "Initial"
        or len(name) > 80
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
    ):
        raise ValueError("Successor migration name or title is invalid")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("A complete applied native history is required")

    previous = timeline[-1].model_tables
    mapping = model_identity_map(models)

    if any(reference.startswith("untrusted:") for reference in mapping):
        raise ValueError("Server successors require stable model identities")

    before = [model for model in models if f"{model.__module__}:{model.__qualname__}" in previous]
    added = set(mapping) - set(previous)

    if (
        len(added) != 1
        or any(mapping.get(key) != value for key, value in previous.items())
        or (len(timeline) == 1 and not verify_server_initial_current(connection, before, alias))
    ):
        raise RuntimeError("Historical model mapping or Initial schema is unverified")

    new_model = next(
        model for model in models if f"{model.__module__}:{model.__qualname__}" in added
    )
    keys = [field for field in new_model.__columns__.values() if field.primary_key]

    if (
        len(keys) != 1
        or keys[0].python_type is not int
        or any(
            field.references is not None or field.server_default is not None
            for field in new_model.__columns__.values()
        )
    ):
        raise ValueError("New server table needs separately verified key/constraint semantics")

    candidate = review_server_model_changes(connection, models, alias, identity)

    if (
        len(candidate.operations) != 1
        or candidate.operations[0]["kind"] != "create_table"
        or candidate.operations[0]["table"] != mapping[next(iter(added))]
        or candidate != review
        or not isinstance(approval, str)
        or not hmac.compare_digest(candidate.review_sha256, approval)
    ):
        raise PermissionError("Exact reviewed new-table successor approval is required")

    if len(timeline) == 1:
        number = 2
    else:
        from .migration_history import verify_server_review_predecessor

        number = verify_server_review_predecessor(connection, models, alias, identity, candidate)

    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {
            "number": number,
            "name": name,
            "title": title,
            "alias": alias,
            "review": candidate.review_sha256,
            "models": canonical_model_map(mapping),
            "operation": canonical_operation_json(candidate.operations[0]),
            "state": "prepared",
        },
    )

    return number


def prepare_server_column_successor(
    connection, models, alias, identity, review, approval, name, title="Untitled"
):
    """Record one reviewed column addition only after historical proof under lock.

    This function does not execute application ALTER TABLE. Existing table
    additions must have trusted Initial model identity and physical evidence.
    """
    import hmac

    from .migration_history import verify_server_column_addition_predecessor
    from .migration_review import (
        _reviewed_server_successor_number,
        review_server_model_changes,
    )
    from .model_discovery import model_identity_map

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL column successor requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL column successor requires a held lock")
    else:
        raise ValueError("Unsupported server column successor backend")

    if (
        not isinstance(name, str)
        or not name
        or name == "Initial"
        or len(name) > 80
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
    ):
        raise ValueError("Invalid column successor name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied history is required for column addition")

    mapping = model_identity_map(models)

    if any(key.startswith("untrusted:") for key in mapping):
        raise RuntimeError("Historical model identities cannot authorize column addition")

    candidate = review_server_model_changes(connection, models, alias, identity)

    if (
        candidate != review
        or not isinstance(approval, str)
        or not hmac.compare_digest(candidate.review_sha256, approval)
        or len(candidate.operations) != 1
        or candidate.operations[0]["kind"] != "add_column"
    ):
        raise PermissionError("Exact reviewed column addition approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate, verify_server_column_addition_predecessor
    )
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {
            "number": number,
            "name": name,
            "title": title,
            "alias": alias,
            "review": candidate.review_sha256,
            "models": canonical_model_map(mapping),
            "operation": canonical_operation_json(candidate.operations[0]),
            "state": "prepared",
        },
    )

    return number


def prepare_server_widening_successor(
    connection, models, alias, identity, review, approval, name, title="Untitled"
):
    """Prepare exactly one history-verified widening under backend coordination.

    No ALTER is executed here. The proposal is reconstructed inside the held
    transaction/lock so a stale read-only review cannot authorize DDL.
    """
    import hmac

    from .migration_history import verify_server_widening_predecessor
    from .migration_review import (
        _reviewed_server_successor_number,
        review_server_column_widening,
    )
    from .model_discovery import model_identity_map

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL widening requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL widening requires a held lock")
    else:
        raise ValueError("Unsupported widening backend")

    if (
        not isinstance(name, str)
        or not name
        or name == "Initial"
        or len(name) > 80
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
    ):
        raise ValueError("Invalid widening migration name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied model history is required")

    candidate = review_server_column_widening(connection, models, alias, identity)

    if (
        candidate != review
        or not isinstance(approval, str)
        or not hmac.compare_digest(candidate.review_sha256, approval)
        or len(candidate.operations) != 1
        or candidate.operations[0]["kind"] != "widen_column"
    ):
        raise PermissionError("Exact reviewed widening approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate, verify_server_widening_predecessor
    )
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {
            "number": number,
            "name": name,
            "title": title,
            "alias": alias,
            "review": candidate.review_sha256,
            "models": canonical_model_map(model_identity_map(models)),
            "operation": canonical_operation_json(candidate.operations[0]),
            "state": "prepared",
        },
    )
    return number


def prepare_server_nullability_successor(
    connection, models, alias, identity, review, approval, name, title="Untitled"
):
    """Prepare one historically proven nullable-flag edit under a backend lock.

    The reviewer repeats its bounded NULL-row probe under the held scope.
    This operation records intent; it never executes ALTER TABLE itself.
    """
    import hmac

    from .migration_history import verify_server_nullability_predecessor
    from .migration_review import (
        _reviewed_server_successor_number,
        review_server_nullability_change,
    )
    from .model_discovery import model_identity_map

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL nullability requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL nullability requires a held migration lock")
    else:
        raise ValueError("Unsupported nullability backend")

    if (
        not isinstance(name, str)
        or not name
        or name == "Initial"
        or len(name) > 80
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
    ):
        raise ValueError("Invalid nullability migration name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied model history is required")

    candidate = review_server_nullability_change(connection, models, alias, identity)

    if (
        candidate != review
        or not isinstance(approval, str)
        or not hmac.compare_digest(candidate.review_sha256, approval)
        or len(candidate.operations) != 1
        or candidate.operations[0]["kind"] != "change_nullability"
    ):
        raise PermissionError("Exact reviewed nullability approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate, verify_server_nullability_predecessor
    )
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {
            "number": number,
            "name": name,
            "title": title,
            "alias": alias,
            "review": candidate.review_sha256,
            "models": canonical_model_map(model_identity_map(models)),
            "operation": canonical_operation_json(candidate.operations[0]),
            "state": "prepared",
        },
    )
    return number


def prepare_server_default_successor(
    connection, models, alias, identity, review, approval, name, title="Untitled"
):
    """Record one historically proven default addition under backend coordination."""
    import hmac

    from .migration_history import verify_server_default_predecessor
    from .migration_review import (
        _reviewed_server_successor_number,
        review_server_default_addition,
    )
    from .model_discovery import model_identity_map

    if connection.dialect == "postgresql":
        if not connection._transaction_active:
            raise RuntimeError("PostgreSQL default addition requires a transaction")
    elif connection.dialect == "mysql":
        if not getattr(connection, "_migration_lock_active", False):
            raise RuntimeError("MySQL default addition requires a held migration lock")
    else:
        raise ValueError("Unsupported default addition backend")

    if (
        not isinstance(name, str)
        or not name
        or name == "Initial"
        or len(name) > 80
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
    ):
        raise ValueError("Invalid default migration name or title")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied model history is required")

    candidate = review_server_default_addition(connection, models, alias, identity)

    if (
        candidate != review
        or not isinstance(approval, str)
        or not hmac.compare_digest(candidate.review_sha256, approval)
        or len(candidate.operations) != 1
        or candidate.operations[0]["kind"] != "add_default"
    ):
        raise PermissionError("Exact reviewed default approval is required")

    number = _reviewed_server_successor_number(
        connection, models, alias, identity, candidate, verify_server_default_predecessor
    )
    table = _quote(_TABLE, connection.dialect)
    connection.execute_write(
        f"INSERT INTO {table} (migration_id, name, title, database_alias, "
        "review_sha256, models_json, operation_json, schema_sha256, state) "
        "VALUES (:number, :name, :title, :alias, :review, :models, :operation, NULL, :state)",
        {
            "number": number,
            "name": name,
            "title": title,
            "alias": alias,
            "review": candidate.review_sha256,
            "models": canonical_model_map(model_identity_map(models)),
            "operation": canonical_operation_json(candidate.operations[0]),
            "state": "prepared",
        },
    )
    return number
