"""Guarded SQLite Initial record and read-only physical-schema verification.

This supplements, rather than replaces, the historical per-table journal.
Legacy databases must not be adopted or rewritten merely by reading history.
"""

from hashlib import sha256
import json
import re

from .schema import create_table_sql
from .model_identity import canonical_model_map, model_identity_map


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
_JOURNALS = frozenset({
    _TABLE,
    "aurorasql_schema_history",
    "aurorasql_migration_intents_v2",
    "aurorasql_additive_history_v1",
})
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

    payload = json.dumps({
        "database": main[0],
        "alias": alias,
        "ddl": [create_table_sql(model, "sqlite") for model in models],
    }, sort_keys=True, separators=(",", ":"))
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
        "migration_id", "name", "title", "database_alias", "review_sha256", "models_json",
        "schema_sha256", "state"
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

        if (number != expected_number or (number == 1) != (name == "Initial")
                or not isinstance(name, str) or not name or recorded_alias != alias
                or not isinstance(title, str) or not title or len(title) > 160
                or not isinstance(review, str) or not _HASH.fullmatch(review)
                or state not in ("prepared", "applied", "uncertain")
                or (schema is not None and (not isinstance(schema, str) or not _HASH.fullmatch(schema)))):
            raise RuntimeError("Migration timeline is non-linear, invalid, or belongs to another alias")

        records.append({"number": number, "name": name, "title": title, "status": state,
                        "review_sha256": review, "schema_sha256": schema,
                        "model_tables": model_mapping})

    matches = records[-1]["status"] == "applied" and records[-1]["schema_sha256"] == _schema_digest(raw)

    # A later v2 intent cannot be treated as part of Initial's applied chain.
    # Keep it as separate evidence until a verified successor is integrated.
    intent_table = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='aurorasql_migration_intents_v2'"
    ).fetchone()
    unintegrated = False

    if intent_table:
        from .migration_journal_v2 import _require_journal, _validate_intent, list_intents

        if raw.in_transaction:
            _require_journal(raw)
            intent_rows = raw.execute(
                'SELECT migration_id,review_sha256,before_sha256,after_sha256,state '
                'FROM "aurorasql_migration_intents_v2"'
            ).fetchall()

            for row in intent_rows:
                _validate_intent(*row[:4])

            recorded = {row[0]: (row[1], row[2], row[3], row[4]) for row in intent_rows}
        else:
            intents = list_intents(raw)
            recorded = {entry.migration_id: (entry.review_sha256, entry.before_sha256,
                                            entry.after_sha256, entry.state)
                        for entry in intents}

        successors = {entry["name"]: entry for entry in records[1:]}
        unintegrated = (set(recorded) != set(successors)
                        or any(record[3] != "applied" for record in recorded.values())
                        or any(recorded[name][0] != successor["review_sha256"]
                               for name, successor in successors.items() if name in recorded))

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

    consistent = bool(matches and not legacy_findings and not unintegrated
                      and all(entry["status"] == "applied" for entry in records))

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
    from .history import list_history, review_history

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
    from .init_validation import validate_init_models

    if not initial["physical_history_consistent"]:
        return False

    if initial["model_tables"] != model_identity_map(models):
        return False

    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        return False

    ordered = validate_init_models(models, dialect="sqlite")
    expected_tables = {model.__table_name__: sha256(
        create_table_sql(model, "sqlite").encode("utf-8")
    ).hexdigest() for model in ordered}

    if (len(legacy_entries) != len(expected_tables)
            or {entry.table: entry.fingerprint for entry in legacy_entries} != expected_tables
            or any(entry.status != "applied" for entry in legacy_entries)):
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
    from .init_validation import validate_init_models
    from .migration_format import snapshot_fingerprint
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
            {model.__table_name__ for model in ordered}):
        return False

    from .migration_journal_v2 import list_intents

    intents = {entry.migration_id: entry for entry in list_intents(raw)}
    latest = intents.get(timeline[-1]["name"])

    from .cli_explicit_operations import _catalog, _catalog_digest

    expected_sha = (_catalog_digest(_catalog(raw)) if timeline[-1]["name"].startswith(("schema_", "edit_"))
                    else snapshot_fingerprint(schema_snapshot(*ordered, dialect="sqlite")))

    if latest is None or latest.after_sha256 != expected_sha:
        return False

    # ALTER TABLE SQL text is not identical to fresh CREATE TABLE SQL.
    # Compare actual columns, defaults, keys, indexes and FKs in isolation.
    import sqlite3

    from .sqlite_additive_migration import _verify_result_catalog

    with sqlite3.connect(":memory:") as expected:
        expected.execute("PRAGMA foreign_keys=ON")

        for model in ordered:
            expected.execute(create_table_sql(model, "sqlite"))

        try:
            _verify_result_catalog(raw, expected, {model.__table_name__ for model in ordered},
                                   native=True)
        except RuntimeError:
            return False

    return True
