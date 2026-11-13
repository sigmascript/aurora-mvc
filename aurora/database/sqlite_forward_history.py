"""Small, forward-only SQLite migration history for new Aurora databases.

Existing AuroraSQL journal databases are handled by their existing read-only
verification paths until an explicit, separately verified conversion exists.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schema import create_table_sql, _quote
from .schema_records import prepare_schema_record
from .snapshots import schema_snapshot


TABLE = "migrations"
DDL = ('CREATE TABLE "migrations" ('
       '"id" INTEGER PRIMARY KEY, "title" TEXT NOT NULL, '
       '"snapshot" TEXT NOT NULL, "date" TEXT NOT NULL, "warnings" TEXT)')
_SNAPSHOT = re.compile(r"[0-9]{8}-[0-9]{6}(?:-[0-9]{1,3})?\.json\Z")
_OLD = {"aurorasql_migration_timeline", "aurorasql_schema_history",
        "aurorasql_migration_intents_v2", "aurorasql_additive_history_v1"}


def _tables(raw):
    return {row[0] for row in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def has_forward_history(raw):
    return TABLE in _tables(raw)


def _physical(raw):
    return [{"type": kind, "name": name, "table": table, "sql": sql}
            for kind, name, table, sql in raw.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
            if name not in _OLD and name != TABLE]


def read_history(raw, project_root, db_file):
    """Read and verify the latest successful schema witness; never mutate."""
    tables = _tables(raw)
    if TABLE not in tables or tables.intersection(_OLD):
        raise RuntimeError("Database does not contain a recognized single-table migration history")
    columns = raw.execute('PRAGMA table_info("migrations")').fetchall()
    if [item[1] for item in columns] != ["id", "title", "snapshot", "date", "warnings"] or columns[0][5] != 1:
        raise RuntimeError("Migration history table definition is unrecognized")
    records = raw.execute('SELECT id,title,snapshot,date,warnings FROM "migrations" ORDER BY id').fetchall()
    if not records:
        raise RuntimeError("Migration history has no initial record")
    directory, _ = prepare_schema_record(project_root, db_file, ())
    last_snapshot = None
    seen_snapshots = set()
    for expected, (ident, title, filename, date, warnings) in enumerate(records, 1):
        if (ident != expected or not isinstance(title, str) or not title or
                not isinstance(filename, str) or not _SNAPSHOT.fullmatch(filename) or
                not isinstance(date, str) or not date):
            raise RuntimeError("Migration history is incomplete or contains invalid entries")
        if filename in seen_snapshots:
            raise RuntimeError("Migration history reuses a snapshot filename")
        seen_snapshots.add(filename)
        if warnings is not None:
            try:
                parsed = json.loads(warnings)
                if not isinstance(parsed, list) or any(not isinstance(w, str) for w in parsed):
                    raise ValueError("Invalid warnings")
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Migration warnings are malformed") from exc
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Migration snapshot missing: {filename}; inspect before retrying")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Migration snapshot is unreadable: {filename}") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"Migration snapshot is invalid: {filename}")
        models = snapshot.get("models")
        physical = snapshot.get("physical_schema")
        model_names = ([table.get("name") for table in models if isinstance(table, dict)]
                       if isinstance(models, list) else [])
        physical_names = ([item.get("name") for item in physical
                           if isinstance(item, dict) and item.get("type") == "table"]
                          if isinstance(physical, list) else [])
        if (snapshot.get("database") != Path(db_file).stem or
                snapshot.get("engine") != "sqlite" or snapshot.get("title") != title or
                not isinstance(models, list) or not isinstance(physical, list) or
                any(not isinstance(table, dict) or not isinstance(table.get("name"), str)
                    for table in models) or
                any(not isinstance(item, dict) or
                    not all(isinstance(item.get(key), str) for key in ("type", "name", "table", "sql"))
                    for item in physical) or
                len(model_names) != len(set(model_names)) or
                len(physical_names) != len(set(physical_names)) or
                sorted(model_names) != sorted(physical_names)):
            raise RuntimeError(f"Migration snapshot is invalid: {filename}")
        last_snapshot = snapshot
    if last_snapshot["physical_schema"] != _physical(raw):
        raise RuntimeError("Database schema differs from the latest migration snapshot")
    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Database has foreign-key violations")
    return records, last_snapshot


def matches_models(raw, models):
    """Compare physical structure independently, without trusting a snapshot alone."""
    from .sqlite_additive_migration import _verify_result_catalog
    # Reuse the physical comparison, but it expects only application objects.
    # A temporary in-memory clone removes the internal migrations table from
    # the comparison; no application rows are read or copied.
    with sqlite3.connect(":memory:") as expected:
        expected.execute("PRAGMA foreign_keys=ON")
        for model in models:
            expected.execute(create_table_sql(model, "sqlite"))
        with sqlite3.connect(":memory:") as observed:
            for item in _physical(raw):
                if item["type"] == "table":
                    observed.execute(item["sql"])
            _verify_result_catalog(observed, expected,
                                   {model.__table_name__ for model in models})
    return True


def _new_record(raw, root, db_file, models, title, warnings=()):
    """Write snapshot from uncommitted catalog, then history in one SQLite txn.

    Callers remove the just-created file if the transaction fails. Crashes may
    leave an orphan snapshot, never a fictitious successfully applied DB row.
    """
    directory, model_snapshot = prepare_schema_record(root, db_file, models)
    if not raw.in_transaction:
        raise RuntimeError("A locked SQLite transaction is required")
    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Foreign-key verification failed")
    if raw.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("SQLite integrity verification failed")
    content = {"database": Path(db_file).stem, "engine": "sqlite", "title": title,
               "models": model_snapshot["tables"], "physical_schema": _physical(raw)}
    actual_tables = {x["name"] for x in content["physical_schema"] if x["type"] == "table"}
    if actual_tables != {model.__table_name__ for model in models}:
        raise RuntimeError("Database tables differ from selected models")
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    for attempt in range(1000):
        name = f"{timestamp}{('-' + str(attempt)) if attempt else ''}.json"
        path = directory / name
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
            break
        except FileExistsError:
            continue
        except OSError:
            path.unlink(missing_ok=True)
            raise
    else:
        raise RuntimeError("No available migration snapshot filename")
    date = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        raw.execute('INSERT INTO "migrations" (title,snapshot,date,warnings) VALUES (?,?,?,?)',
                    (title, name, date, json.dumps(list(warnings)) if warnings else None))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def initialize(raw, root, db_file, models, title):
    """Create a complete fresh schema and its first history record atomically."""
    if any(m.__table_name__ == TABLE or m.__table_name__ in _OLD for m in models):
        raise ValueError("A model table name is reserved for Aurora migration history")
    if raw.in_transaction or _tables(raw):
        raise RuntimeError("Initialization requires an empty database")
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        if _tables(raw):
            raise RuntimeError("Database changed before initialization")
        raw.execute(DDL)
        for model in models:
            raw.execute(create_table_sql(model, "sqlite"))
        matches_models(raw, models)
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path


def migrate_additions(raw, root, db_file, models, title):
    """Apply only safe new tables and nullable/defaulted columns, forward-only."""
    from .init_validation import validate_init_models
    from .planning import plan_schema
    from .core import Connection
    from .sqlite_additive_migration import _column_sql
    validate_init_models(models, dialect="sqlite")
    if any(m.__table_name__ == TABLE or m.__table_name__ in _OLD for m in models):
        raise ValueError("A model table name is reserved for Aurora migration history")
    path = None
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    conn = Connection(raw, "sqlite")
    _, old = read_history(raw, root, db_file)
    old_tables = {item["name"] for item in old["models"]}
    if old_tables - {model.__table_name__ for model in models}:
        raise ValueError("Removing tables requires a separately reviewed operation")
    operations = []
    for model in models:
        plan = plan_schema(conn, model)
        if plan.warnings and plan.exists:
            raise ValueError("Unsupported model drift; migration refused")
        for change in plan.changes:
            if change.kind == "create_table":
                operations.append(create_table_sql(model, "sqlite"))
            elif change.kind == "missing_column" and model.__table_name__ in old_tables:
                # Restrict additions to AuroraSQL's own validated SQL renderer.
                from .sqlite_additive_migration import _column_sql
                name = change.column
                operations.append(f'ALTER TABLE {_quote(model.__table_name__, "sqlite")} ADD COLUMN {_column_sql(model, name)}')
            else:
                raise ValueError("Unsupported model change; migration refused")
    if not operations:
        return None
    try:
        raw.execute("BEGIN IMMEDIATE")
        read_history(raw, root, db_file)
        for sql in operations:
            raw.execute(sql)
        matches_models(raw, models)
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path



def _data_witness(raw, table):
    """Fingerprint complete table contents, independent of row retrieval order.

    This is only review evidence: execution compares it again while holding the
    SQLite writer lock, before any DDL.
    """
    columns = len(raw.execute(f'PRAGMA table_info({_quote(table, "sqlite")})').fetchall())
    if not columns:
        raise RuntimeError("Deletion target has no verifiable columns")
    ordering = ", ".join(str(i) for i in range(1, columns + 1))
    digest = hashlib.sha256()
    for row in raw.execute(f'SELECT * FROM {_quote(table, "sqlite")} ORDER BY {ordering}'):
        for value in row:
            encoded = (type(value).__name__ + ':' + repr(value)).encode('utf-8')
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
    return digest.hexdigest()


def review_table_removal(raw, root, db_file, models):
    """Return one independently evidenced table removal, or refuse.

    A table is eligible only when no foreign keys, views, triggers or explicit
    indexes can be affected. This is inspection, not execution authorization.
    """
    _, snapshot = read_history(raw, root, db_file)
    prior = {entry["name"] for entry in snapshot["models"]}
    current = {model.__table_name__ for model in models}
    removed = prior - current
    if len(removed) != 1 or current - prior:
        raise ValueError("Only one table removal at a time is supported")
    target = removed.pop()
    catalog = _physical(raw)
    # A dependent object or foreign key may be silently destroyed or invalidated.
    # Refuse instead of trying to approximate dependency semantics.
    if any(item["type"] in ("view", "trigger") or
           (item["type"] == "index" and item["table"] == target)
           for item in catalog):
        raise ValueError("Table removal has dependencies requiring separate review")
    for item in catalog:
        if item["type"] != "table":
            continue
        for key in raw.execute(f'PRAGMA foreign_key_list({_quote(item["name"], "sqlite")})'):
            if item["name"] == target or key[2].casefold() == target.casefold():
                raise ValueError("Table removal has foreign-key dependencies")
    # Count inside the writer's IMMEDIATE transaction again before execution.
    count = raw.execute(f'SELECT COUNT(*) FROM {_quote(target, "sqlite")}').fetchone()[0]
    return target, count, _data_witness(raw, target)


def migrate_table_removal(raw, root, db_file, models, title, target, expected_count, expected_witness,
                          *, acknowledged=False):
    """Drop one reviewed table under a SQLite write lock and record its risk.

    Authorization for populated tables must be collected separately by the CLI.
    Never treat a prior empty-table observation as sufficient to permit deletion.
    """
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    snapshot_path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        actual_target, count, witness = review_table_removal(raw, root, db_file, models)
        if (actual_target, count, witness) != (target, expected_count, expected_witness):
            raise RuntimeError("Table removal review is stale; run db check again")
        if count and not acknowledged:
            raise PermissionError("Populated table removal requires explicit double confirmation")
        raw.execute(f'DROP TABLE {_quote(target, "sqlite")}')
        matches_models(raw, models)
        warnings = ([f'Approved permanent deletion of table {target}: {count} existing row(s)']
                    if count else [])
        snapshot_path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path




def review_column_removal(raw, root, db_file, models):
    """Inspect exactly one unmodeled column without authorizing its deletion."""
    from .core import Connection
    from .planning import plan_schema

    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise ValueError("SQLite column deletion requires SQLite 3.35 or newer")
    _, previous = read_history(raw, root, db_file)
    prior = {entry["name"] for entry in previous["models"]}
    current = {model.__table_name__ for model in models}
    if current != prior:
        raise ValueError("Column removal cannot be combined with table changes")
    changes = []
    connection = Connection(raw, "sqlite")
    for model in models:
        plan = plan_schema(connection, model)
        if plan.warnings:
            raise ValueError("Column removal has unsupported schema warnings")
        changes.extend((model.__table_name__, change) for change in plan.changes)
    if len(changes) != 1 or changes[0][1].kind != "extra_column":
        raise ValueError("Only one removed column is supported per migration")
    table, change = changes[0]
    column = change.column
    # Explicitly reject schema dependencies, including constraints and indexes.
    # SQLite's own DROP COLUMN validation is an additional fail-closed guard.
    if any(item["type"] in ("view", "trigger") for item in _physical(raw)):
        raise ValueError("Column removal requires separate dependency review")
    if raw.execute(f'PRAGMA index_list({_quote(table, "sqlite")})').fetchall():
        raise ValueError("Column removal has index or unique-constraint dependencies")
    for item in _physical(raw):
        if item["type"] != "table":
            continue
        if raw.execute(f'PRAGMA foreign_key_list({_quote(item["name"], "sqlite")})').fetchall():
            raise ValueError("Column removal has foreign-key dependencies")
    count = raw.execute(
        f'SELECT COUNT(*) FROM {_quote(table, "sqlite")} '
        f'WHERE {_quote(column, "sqlite")} IS NOT NULL'
    ).fetchone()[0]
    return table, column, count, _data_witness(raw, table)


def migrate_column_removal(raw, root, db_file, models, title, table, column,
                           expected_count, expected_witness, *, acknowledged=False):
    """Drop one independently reviewed SQLite column and record approved risk."""
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        actual = review_column_removal(raw, root, db_file, models)
        if actual != (table, column, expected_count, expected_witness):
            raise RuntimeError("Column removal review is stale; run db check again")
        if expected_count and not acknowledged:
            raise PermissionError("Populated column removal requires explicit double confirmation")
        raw.execute(f'ALTER TABLE {_quote(table, "sqlite")} '
                    f'DROP COLUMN {_quote(column, "sqlite")}')
        matches_models(raw, models)
        warnings = ([f'Approved permanent deletion of {table}.{column}: '
                     f'{expected_count} existing non-NULL value(s)'] if expected_count else [])
        path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path


def apply_explicit_rename(raw, root, db_file, models, title, operation):
    """Apply a model-reconciled rename and record one forward migration.

    Only renames with a complete matching resulting model schema are eligible.
    SQLite transaction rollback protects both application DDL and history rows.
    """
    from .init_validation import validate_init_models
    from .cli_explicit_operations import _rehearse_sqlite, _sql_statements, _catalog

    if operation["kind"] not in ("rename_table", "rename_columns"):
        raise ValueError("Only table and column renames are supported with forward history")
    validate_init_models(models, dialect="sqlite")
    statements = _sql_statements(operation)
    expected_catalog = _rehearse_sqlite(Path(db_file), statements)
    path = None
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("PRAGMA legacy_alter_table=OFF")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    try:
        raw.execute("BEGIN IMMEDIATE")
        read_history(raw, root, db_file)
        before_count = raw.execute(
            f'SELECT COUNT(*) FROM {_quote(operation["table"], "sqlite")}').fetchone()[0]
        for statement in statements:
            raw.execute(statement)
        if _catalog(raw) != expected_catalog:
            raise RuntimeError("Migration result differs from the reviewed schema")
        after_table = operation.get("new_table", operation["table"])
        after_count = raw.execute(
            f'SELECT COUNT(*) FROM {_quote(after_table, "sqlite")}').fetchone()[0]
        if before_count != after_count:
            raise RuntimeError("Migration unexpectedly changed the application row count")
        matches_models(raw, models)
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path


def legacy_initial_review(raw, root, db_file, models, alias):
    """Review an alpha history only when EVERY step has an exact full snapshot.

    The old chain is independently verified before matching its immutable steps
    with timestamped schema witnesses. Missing or ambiguous evidence refuses
    conversion; it is never reconstructed from current models.
    """
    from .core import Connection
    from .migration_timeline import inspect_native_history, verify_current_models
    from .snapshots import schema_snapshot

    tables = _tables(raw)
    if TABLE in tables or "aurorasql_migration_timeline" not in tables:
        return None
    timeline, entries, findings = inspect_native_history(Connection(raw, "sqlite"), alias)
    if not timeline or findings or not timeline[-1]["physical_history_consistent"]:
        return None
    if raw.in_transaction:
        from .model_identity import model_identity_map
        if timeline[-1]["model_tables"] != model_identity_map(models):
            return None
        try:
            matches_models(raw, models)
        except (ValueError, RuntimeError):
            return None
    elif not verify_current_models(raw, models, timeline, entries):
        return None

    directory, _ = prepare_schema_record(root, db_file, models)
    if not directory.is_dir() or directory.is_symlink():
        return None
    snapshots = []
    for path in sorted(directory.iterdir(), key=lambda item: (item.name[:15], int(item.stem[16:]) if item.stem[15:16] == "-" and item.stem[16:].isdigit() else -1)):
        if path.is_symlink() or not path.is_file() or not _SNAPSHOT.fullmatch(path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        snapshots.append((path, record))

    # Refuse extra, missing, duplicated, or unordered witnesses rather than
    # silently guessing which historical record belongs to which migration.
    if len(snapshots) != len(timeline):
        return None
    expected_current = schema_snapshot(*models, dialect="sqlite")["tables"]
    proposal = []
    previous_schema = None
    for item, (path, record) in zip(timeline, snapshots):
        physical = record.get("physical_schema")
        declared = record.get("models")
        if (record.get("engine") != "sqlite" or
                record.get("database") != Path(db_file).stem or
                record.get("title") != item["title"] or
                not isinstance(physical, list) or not isinstance(declared, list) or
                not physical or not declared or
                {table.get("name") for table in declared if isinstance(table, dict)} !=
                set(item["model_tables"].values()) or
                {obj.get("name") for obj in physical
                 if isinstance(obj, dict) and obj.get("type") == "table"} !=
                {table["name"] for table in declared} or
                physical == previous_schema):
            return None
        previous_schema = physical
        try:
            date = datetime.strptime(path.name[:15], "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            return None
        proposal.append((item["title"], path.name, date))
    if snapshots[-1][1]["physical_schema"] != _physical(raw) or snapshots[-1][1]["models"] != expected_current:
        return None
    return tuple(proposal)


def convert_legacy_initial(raw, root, db_file, models, alias):
    """Replace a fully evidenced alpha journal atomically, with no DDL to app tables.

    The existing snapshot is reused; no historical schema or timestamp is invented.
    Histories missing any original complete snapshot remain ineligible.
    """
    from .init_validation import validate_init_models

    models = validate_init_models(models, dialect="sqlite")
    if raw.in_transaction:
        raise RuntimeError("History conversion requires its own SQLite transaction")
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    proposal = legacy_initial_review(raw, root, db_file, models, alias)
    if proposal is None:
        raise RuntimeError("Legacy history cannot be safely converted from existing evidence")
    try:
        raw.execute("BEGIN IMMEDIATE")
        if legacy_initial_review(raw, root, db_file, models, alias) != proposal:
            raise RuntimeError("Legacy history changed before conversion")
        raw.execute(DDL)
        for ident, (title, filename, date) in enumerate(proposal, 1):
            raw.execute('INSERT INTO "migrations" (id,title,snapshot,date,warnings) VALUES (?,?,?,?,NULL)',
                        (ident, title, filename, date))
        for name in sorted(_tables(raw) & _OLD):
            raw.execute(f'DROP TABLE {_quote(name, "sqlite")}')
        read_history(raw, root, db_file)
        matches_models(raw, models)
        if raw.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Database integrity check failed during history conversion")
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    return proposal[-1][1]


def migrate_column_edit(raw, root, db_file, models, title, proposal):
    """Rebuild one ordinary table under the same forward-history transaction.

    The proposal only admits compatible widening, validated defaults and
    nullability changes without dependent indexes or foreign keys. Never
    replay a proposal against an unverified or concurrently changed schema.
    """
    from .cli_explicit_operations import _catalog, _rehearse_sqlite
    from .core import Connection
    from .sqlite_column_edit import sqlite_column_edit_proposal

    document, digest, _, statements, table = proposal
    expected = _rehearse_sqlite(Path(db_file), statements)
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    snapshot = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        read_history(raw, root, db_file)
        current = sqlite_column_edit_proposal(
            Connection(raw, "sqlite"), models, "default", db_file, forward=True,
        )
        if current is None or current[1] != digest:
            raise RuntimeError("Column-edit review is stale; run db check again")
        before = raw.execute(f'SELECT COUNT(*) FROM {_quote(table, "sqlite")}').fetchone()[0]
        for statement in statements:
            raw.execute(statement)
        if _catalog(raw) != expected:
            raise RuntimeError("Migration result differs from the reviewed schema")
        after = raw.execute(f'SELECT COUNT(*) FROM {_quote(table, "sqlite")}').fetchone()[0]
        if before != after:
            raise RuntimeError("Column edit unexpectedly changed the number of rows")
        matches_models(raw, models)
        snapshot = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        raise
    return snapshot
