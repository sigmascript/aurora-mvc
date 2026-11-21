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
from .snapshot_paths import sqlite_history_name
from .snapshots import schema_snapshot


TABLE = "__migrations__"
BACKUP_TABLE = "__backups__"
LEGACY_TABLE = "migrations"
DDL = ('CREATE TABLE "__migrations__" ('
       '"id" INTEGER PRIMARY KEY, "title" TEXT NOT NULL, '
       '"snapshot" TEXT NOT NULL, "date" TEXT NOT NULL, "warnings" TEXT)')
_SNAPSHOT = re.compile(r"[0-9]{8}-[0-9]{6}(?:-[0-9]{1,3})?\.json\Z")
_OLD = {"aurorasql_migration_timeline", "aurorasql_schema_history",
        "aurorasql_migration_intents_v2", "aurorasql_additive_history_v1"}


def _tables(raw):
    return {row[0] for row in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def history_table(raw):
    """Select one history table; never guess between conflicting histories."""
    found = _tables(raw) & {TABLE, LEGACY_TABLE}
    if len(found) > 1:
        raise RuntimeError("Both migrations and __migrations__ exist; manual history reconciliation required")
    return next(iter(found), None)


def has_forward_history(raw):
    return history_table(raw) is not None


def _physical(raw):
    return [{"type": kind, "name": name, "table": table, "sql": sql}
            for kind, name, table, sql in raw.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
            if name not in _OLD and name not in (TABLE, LEGACY_TABLE, BACKUP_TABLE)]


def read_history(raw, project_root, db_file):
    """Read and verify the latest successful schema witness; never mutate."""
    tables = _tables(raw)
    selected = history_table(raw)
    if selected is None or tables.intersection(_OLD):
        raise RuntimeError("Database does not contain a recognized single-table migration history")
    columns = raw.execute(f'PRAGMA table_info("{selected}")').fetchall()
    if [item[1] for item in columns] != ["id", "title", "snapshot", "date", "warnings"] or columns[0][5] != 1:
        raise RuntimeError("Migration history table definition is unrecognized")
    records = raw.execute(f'SELECT id,title,snapshot,date,warnings FROM "{selected}" ORDER BY id').fetchall()
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
        if (snapshot.get("database") != sqlite_history_name(project_root, db_file) or
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


def _ordinary_index_sql(raw):
    """Only nonunique, column-based, application indexes are model-neutral."""
    indexes = raw.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master "
        "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    names = {m[1] for m in raw.execute(
        "SELECT type,name,tbl_name FROM sqlite_master WHERE type='table'").fetchall()}
    for name, table, sql in indexes:
        if table not in names or not isinstance(sql, str):
            raise ValueError("Index catalog is not independently verifiable")
        info = raw.execute(f'PRAGMA index_list({_quote(table, "sqlite")})').fetchall()
        entry = next((row for row in info if row[1] == name), None)
        if (entry is None or entry[2] or entry[3] != 'c' or entry[4] or
                not re.fullmatch(r'CREATE\s+INDEX\s+"?[A-Za-z_][A-Za-z_0-9]*"?\s+ON\s+"?[A-Za-z_][A-Za-z_0-9]*"?\s*\(\s*"?[A-Za-z_][A-Za-z_0-9]*"?(?:\s*,\s*"?[A-Za-z_][A-Za-z_0-9]*"?)*\s*\)', sql, re.I)):
            raise ValueError("Advanced or unique indexes require dedicated schema review")
        columns = raw.execute(f'PRAGMA index_xinfo({_quote(name, "sqlite")})').fetchall()
        if any(row[1] < 0 or row[5] != 1 or row[3] for row in columns if row[5]):
            raise ValueError("Expression or collated indexes require dedicated review")
    return indexes


def matches_models_with_indexes(raw, models):
    """Check model columns/constraints while excluding independently validated ordinary indexes."""
    _ordinary_index_sql(raw)
    with sqlite3.connect(':memory:') as clone:
        clone.execute('PRAGMA foreign_keys=ON')
        for kind, name, _, sql in raw.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"):
            if kind == 'table' and sql and name not in (TABLE, LEGACY_TABLE, BACKUP_TABLE):
                clone.execute(sql)
        return matches_models(clone, models)


def validate_index_operation(raw, models, operation):
    """Validate an index-only change against exact model and history evidence."""
    if operation["kind"] not in ("create_index", "drop_index"):
        raise ValueError("Expected an index operation")
    table, name = operation['table'], operation['name']
    if table not in {m.__table_name__ for m in models} or table in (TABLE, LEGACY_TABLE, BACKUP_TABLE):
        raise ValueError("Index target is not a registered application table")
    indexes = _ordinary_index_sql(raw)
    existing = {index_name.casefold(): (index_name, target) for index_name, target, _ in indexes}
    if operation['kind'] == 'create_index':
        if operation['unique']:
            raise ValueError("Unique indexes require the dedicated UNIQUE migration path")
        if any(row[1].casefold() == name.casefold() for row in raw.execute(
                "SELECT type,name FROM sqlite_master")):
            raise ValueError("Index name is already in use")
        fields = {field.casefold(): field for field in next(
            m for m in models if m.__table_name__ == table).__columns__}
        if any(column.casefold() not in fields for column in operation['columns']):
            raise ValueError("Index column is not declared by the selected model")
    elif name.casefold() not in existing or existing[name.casefold()][1] != table:
        raise ValueError("Only a recorded ordinary index on the selected table may be dropped")
    matches_models_with_indexes(raw, models)


def apply_explicit_index(raw, root, db_file, models, title, operation):
    """Rehearse and apply one guarded ordinary index change with forward history."""
    from .cli_explicit_operations import _rehearse_sqlite, _sql_statements, _catalog
    from .init_validation import validate_init_models
    validate_init_models(models, dialect='sqlite', allow_extended=True)
    expected = _rehearse_sqlite(Path(db_file), _sql_statements(operation))
    raw.execute('PRAGMA foreign_keys=ON')
    if raw.in_transaction or raw.execute('PRAGMA foreign_keys').fetchone() != (1,):
        raise RuntimeError("Index migration requires an unlocked FK-enforced SQLite connection")
    path = None
    try:
        raw.execute('BEGIN IMMEDIATE')
        read_history(raw, root, db_file)
        validate_index_operation(raw, models, operation)
        before = raw.execute(f'SELECT count(*) FROM {_quote(operation["table"], "sqlite")}').fetchone()[0]
        for sql in _sql_statements(operation):
            raw.execute(sql)
        if _catalog(raw) != expected:
            raise RuntimeError("Index schema differs from reviewed rehearsal")
        matches_models_with_indexes(raw, models)
        after = raw.execute(f'SELECT count(*) FROM {_quote(operation["table"], "sqlite")}').fetchone()[0]
        if before != after:
            raise RuntimeError("Index migration changed application row count")
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path

def adopt_history(raw, root, db_file):
    """Explicitly adopt verified five-column SQLite history without rewriting records.

    This is an internal-table rename, not a new application schema migration.
    Existing JSON witnesses remain intact. Never reset or invent history.
    """
    if raw.in_transaction:
        raise RuntimeError("History adoption requires its own transaction")
    if history_table(raw) != LEGACY_TABLE:
        raise RuntimeError("There is no legacy history table to adopt")
    before, snapshot = read_history(raw, root, db_file)
    raw.execute("PRAGMA foreign_keys=ON")
    try:
        raw.execute("BEGIN IMMEDIATE")
        if history_table(raw) != LEGACY_TABLE or read_history(raw, root, db_file) != (before, snapshot):
            raise RuntimeError("History changed during adoption; retry only after review")
        raw.execute(f'ALTER TABLE "{LEGACY_TABLE}" RENAME TO "{TABLE}"')
        after, verified = read_history(raw, root, db_file)
        if after != before or verified != snapshot:
            raise RuntimeError("History adoption verification failed")
        if raw.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite integrity verification failed during history adoption")
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    return len(before)


def matches_models(raw, models):
    """Compare physical structure independently, without trusting a snapshot alone."""
    from .sqlite_additive_migration import _verify_result_catalog
    # Reuse the physical comparison, but it expects only application objects.
    # A temporary in-memory clone removes the internal migrations table from
    # the comparison; no application rows are read or copied.
    with sqlite3.connect(":memory:") as expected:
        expected.execute("PRAGMA foreign_keys=ON")
        for model in models:
            expected.execute(create_table_sql(model, "sqlite", models=models))
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
    content = {"database": sqlite_history_name(root, db_file), "engine": "sqlite", "title": title,
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
        raw.execute(f'INSERT INTO "{history_table(raw)}" (title,snapshot,date,warnings) VALUES (?,?,?,?)',
                    (title, name, date, json.dumps(list(warnings)) if warnings else None))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def initialize(raw, root, db_file, models, title):
    """Create a complete fresh schema and its first history record atomically."""
    if any(m.__table_name__ in (TABLE, LEGACY_TABLE, BACKUP_TABLE) or m.__table_name__ in _OLD for m in models):
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
            raw.execute(create_table_sql(model, "sqlite", models=models))
        matches_models_with_indexes(raw, models)
        if raw.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError("Rename would leave invalid foreign-key references")
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
    ordered_models = validate_init_models(models, dialect="sqlite", allow_extended=True)
    if any(m.__table_name__ in (TABLE, LEGACY_TABLE, BACKUP_TABLE) or m.__table_name__ in _OLD for m in models):
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
    for model in ordered_models:
        trusted = {item["name"] for item in old["physical_schema"]
                   if item["type"] == "index" and item["sql"] is not None}
        plan = plan_schema(conn, model, models=models, trusted_indexes=trusted)
        if plan.warnings and plan.exists:
            raise ValueError("Unsupported model drift; migration refused")
        for change in plan.changes:
            if change.kind == "create_table":
                operations.append(create_table_sql(model, "sqlite", models=models))
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
        matches_models_with_indexes(raw, models)
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


def review_table_removal(raw, root, db_file, models, *, allow_new_tables=False):
    """Return one independently evidenced table removal, or refuse.

    A table is eligible when no surviving table references it and no views or
    triggers can be affected. Its own outgoing foreign keys disappear with it.
    This is inspection, not execution authorization.
    """
    _, snapshot = read_history(raw, root, db_file)
    prior = {entry["name"] for entry in snapshot["models"]}
    current = {model.__table_name__ for model in models}
    removed = prior - current
    if len(removed) != 1 or (current - prior and not allow_new_tables):
        raise UnsafeTableTransition(
            "Table removal requires exactly one undeclared table; "
            "added tables require a mixed review"
        )
    target = removed.pop()
    catalog = _physical(raw)
    # Ordinary indexes owned by the removed table go with that table.
    # Keep the validated index catalog for unrelated application tables.
    try:
        _ordinary_index_sql(raw)
    except ValueError as exc:
        raise UnsafeTableTransition("Table removal has unverified ordinary-index dependencies") from exc
    if any(item["type"] in ("view", "trigger") for item in catalog):
        raise UnsafeTableTransition(f"Cannot remove {target}: views or triggers require separate review")
    for item in catalog:
        if item["type"] != "table":
            continue
        for key in raw.execute(f'PRAGMA foreign_key_list({_quote(item["name"], "sqlite")})'):
            if item["name"] != target and key[2].casefold() == target.casefold():
                raise UnsafeTableTransition(
                    f"Cannot remove {target}: surviving table {item['name']} references it"
                )
    # Count inside the writer's IMMEDIATE transaction again before execution.
    count = raw.execute(f'SELECT COUNT(*) FROM {_quote(target, "sqlite")}').fetchone()[0]
    return target, count, _data_witness(raw, target)


def migrate_mixed_table_transition(raw, root, db_file, models, title, target,
                                   expected_count, expected_witness, *, acknowledged=False):
    """Drop one reviewed table and create new ones in one forward transaction."""
    from .init_validation import validate_init_models

    ordered_models = validate_init_models(models, dialect="sqlite", allow_extended=True)
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("SQLite foreign keys must be enabled")

    snapshot_path = None

    try:
        raw.execute("BEGIN IMMEDIATE")
        _, previous = read_history(raw, root, db_file)
        old_tables = {item["name"] for item in previous["models"]}
        new_models = [model for model in ordered_models if model.__table_name__ not in old_tables]

        if not new_models:
            raise UnsafeTableTransition("Mixed table migration requires a new model table")

        actual_target, count, witness = review_table_removal(
            raw, root, db_file, models, allow_new_tables=True
        )
        if (actual_target, count, witness) != (target, expected_count, expected_witness):
            raise RuntimeError("Mixed table review is stale; run db check again")
        if count and not acknowledged:
            raise PermissionError("Populated table removal requires typed authorization")

        raw.execute(f'DROP TABLE {_quote(target, "sqlite")}')

        for model in new_models:
            raw.execute(create_table_sql(model, "sqlite", models=ordered_models))

        matches_models_with_indexes(raw, models)
        warnings = (
            [f'Approved permanent deletion of table {target}: {count} existing row(s); '
             'backup recommended; not automatically created'] if count else []
        )
        snapshot_path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()

    except BaseException:
        raw.rollback()
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise

    return snapshot_path


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
            raise PermissionError("Populated table removal requires typed authorization")
        raw.execute(f'DROP TABLE {_quote(target, "sqlite")}')
        matches_models_with_indexes(raw, models)
        warnings = ([f'Approved permanent deletion of table {target}: {count} existing row(s); '
                     'backup recommended; not automatically created'] if count else [])
        snapshot_path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path




def review_column_removal(raw, root, db_file, models, *, allow_mixed=False):
    """Review an entire set of removed columns against real dependencies and data."""
    from .core import Connection
    from .planning import plan_schema

    if not allow_mixed and sqlite3.sqlite_version_info < (3, 35, 0):
        raise ValueError("SQLite column deletion requires SQLite 3.35 or newer")
    _, previous = read_history(raw, root, db_file)
    prior = {entry["name"] for entry in previous["models"]}
    current = {model.__table_name__ for model in models}
    if current != prior:
        raise ValueError("Column removal cannot be combined with table changes")
    changes = []
    connection = Connection(raw, "sqlite")
    trusted = {item["name"] for item in previous["physical_schema"]
               if item["type"] == "index" and item["sql"] is not None}
    for model in models:
        plan = plan_schema(connection, model, models=models,
                           trusted_indexes=trusted)
        if plan.warnings:
            raise ValueError("Column removal has unsupported schema warnings")
        changes.extend((model.__table_name__, change) for change in plan.changes)
    if not changes or not any(change.kind == "extra_column" for _, change in changes):
        raise ValueError("Column removal needs a removed column")
    if allow_mixed:
        allowed = {"extra_column", "type_mismatch", "default_mismatch",
                   "nullability_mismatch", "unique_mismatch", "foreign_key_mismatch",
                   "foreign_key_action_mismatch", "missing_column"}
        if (any(change.kind not in allowed for _, change in changes)
                or len({table for table, _ in changes}) != 1):
            raise ValueError("Mixed column removal requires one supported table transition")
    elif any(change.kind != "extra_column" for _, change in changes):
        raise ValueError("Column removal must contain only removed columns")
    try:
        indexes = _ordinary_index_sql(raw)
    except ValueError as exc:
        raise UnsafeTableTransition("Column removal has unverified index dependencies") from exc
    if any(item["type"] in ("view", "trigger") for item in _physical(raw)):
        raise UnsafeTableTransition("Column removal has view or trigger dependencies")
    # Recheck every target before changing a single column. Reject any
    # dependency that refers to a removed column, not unrelated FK/indexes.
    reviewed = []
    for table, change in changes:
        if change.kind != "extra_column":
            continue
        column = change.column
        for index_name, index_table, _ in indexes:
            if index_table != table:
                continue
            keys = [entry[2] for entry in raw.execute(
                f'PRAGMA index_xinfo({_quote(index_name, "sqlite")})') if entry[5]]
            if column in keys:
                raise UnsafeTableTransition(
                    f"Cannot remove {table}.{column}: ordinary index {index_name} uses this column")
        for item in _physical(raw):
            if item["type"] != "table":
                continue
            for fk in raw.execute(f'PRAGMA foreign_key_list({_quote(item["name"], "sqlite")})'):
                if ((item["name"] == table and fk[3] == column) or
                        (fk[2].casefold() == table.casefold() and fk[4] == column)):
                    raise UnsafeTableTransition(
                        f"Cannot remove {table}.{column}: foreign-key dependency in {item['name']}")
        count = raw.execute(
            f'SELECT COUNT(*) FROM {_quote(table, "sqlite")} '
            f'WHERE {_quote(column, "sqlite")} IS NOT NULL'
        ).fetchone()[0]
        reviewed.append((table, column, count, _data_witness(raw, table)))
    return tuple(reviewed)


def migrate_column_removal(raw, root, db_file, models, title, reviewed,
                           *, acknowledged=()):
    """Drop all reviewed columns in one transaction with one history record."""
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        actual = review_column_removal(raw, root, db_file, models)
        if actual != reviewed:
            raise RuntimeError("Column removal review is stale; run db check again")
        dangerous = {(table, column) for table, column, count, _ in reviewed if count}
        if dangerous and not dangerous.issubset(set(acknowledged)):
            raise PermissionError("Populated column removal requires typed authorization")
        for table, column, _, _ in reviewed:
            raw.execute(f'ALTER TABLE {_quote(table, "sqlite")} '
                        f'DROP COLUMN {_quote(column, "sqlite")}')
        matches_models_with_indexes(raw, models)
        warnings = [f'Approved permanent deletion of {table}.{column}: '
                    f'{count} existing non-NULL value(s); backup recommended, not automatically created'
                    for table, column, count, _ in reviewed if count]
        path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path


def apply_explicit_rename(raw, root, db_file, models, title, operation, *, source_edits=None):
    """Apply a model-reconciled rename and record one forward migration.

    Only renames with a complete matching resulting model schema are eligible.
    SQLite transaction rollback protects both application DDL and history rows.
    """
    from .init_validation import validate_init_models
    from .cli_explicit_operations import _rehearse_sqlite, _sql_statements, _catalog

    if operation["kind"] not in ("rename_table", "rename_columns"):
        raise ValueError("Only table and column renames are supported with forward history")
    validate_init_models(models, dialect="sqlite", allow_extended=True)
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
        applied = []
        if source_edits:
            from .explicit_model_rename import apply_source_changes
            applied = apply_source_changes(source_edits)
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        if locals().get('applied'):
            from .explicit_model_rename import restore_source_changes
            restore_source_changes(applied)
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
    if tables & {TABLE, LEGACY_TABLE} or "aurorasql_migration_timeline" not in tables:
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
            raw.execute(f'INSERT INTO "{TABLE}" (id,title,snapshot,date,warnings) VALUES (?,?,?,?,NULL)',
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



class UnsafeTableTransition(ValueError):
    """An internally verified, developer-safe table-transition refusal."""


def _incoming_foreign_keys(raw, parent_table):
    """Return columns on surviving tables that reference the selected parent."""
    references = []

    for item in _physical(raw):
        if item["type"] != "table" or item["name"] == parent_table:
            continue

        child = item["name"]
        keys = raw.execute(
            f'PRAGMA foreign_key_list({_quote(child, "sqlite")})'
        ).fetchall()

        for key in keys:
            if key[2].casefold() == parent_table.casefold():
                references.append((child, key[4]))

    return references


def _review_ordinary_rebuild(raw, root, db_file, models, kind):
    """Review an ordinary SQLite table transition and its real data risks.

    One table may have several type/default changes. The same review runs
    read-only before confirmation and again inside the write transaction.
    """
    from .core import Connection
    from .planning import plan_schema, _sqlite_declared_type
    from .schema import _default_literal

    _, prior = read_history(raw, root, db_file)
    trusted = {item["name"] for item in prior["physical_schema"]
               if item["type"] == "index" and item["sql"] is not None}
    changes = []
    for model in models:
        plan = plan_schema(Connection(raw, "sqlite"), model, models=models,
                           trusted_indexes=trusted)
        if plan.warnings or not plan.exists:
            return None
        changes.extend((model, change) for change in plan.changes)
    allowed = ({"type_mismatch", "default_mismatch", "nullability_mismatch", "unique_mismatch", "foreign_key_mismatch", "foreign_key_action_mismatch", "missing_column", "extra_column"} if kind == "table_transition"
               else {kind})
    if not changes or any(change.kind not in allowed for _, change in changes):
        return None
    tables = {model.__table_name__ for model, _ in changes}
    if len(tables) != 1:
        return None
    table = next(iter(tables))
    model = next(model for model in models if model.__table_name__ == table)
    names = tuple(sorted({change.column for _, change in changes}))
    actual_columns = {row[1]: row[2] for row in raw.execute(
        f'PRAGMA table_info({_quote(table, "sqlite")})')}
    if any(change.kind == "extra_column" for _, change in changes):
        # Reuse the destructive reviewer for actual dependencies and row risks.
        review_column_removal(raw, root, db_file, models, allow_mixed=True)
    for _, change in changes:
        name = change.column
        if change.kind == "extra_column":
            continue
        field = model.__columns__[name]
        if change.kind == "missing_column":
            from .sqlite_additive_migration import _column_sql
            # A rebuild can introduce an undefaulted required column only
            # when no existing row needs a value for that column.
            required_without_default = not field.nullable and field.default is None

            if required_without_default and raw.execute(
                    f'SELECT 1 FROM {_quote(table, "sqlite")} LIMIT 1').fetchone():
                raise UnsafeTableTransition(
                    f"Cannot add {table}.{name}: populated rows require a value "
                    "before NOT NULL can be applied"
                )

            _column_sql(model, name, allow_empty_required=required_without_default)
            continue
        if (field.primary_key or field.server_default is not None or
                (field.references is not None and change.kind not in (
                    "foreign_key_mismatch", "foreign_key_action_mismatch"))):
            raise UnsafeTableTransition(
                f"Cannot change {table}.{name}: key or constrained columns require separate review")
        if change.kind in ("foreign_key_mismatch", "foreign_key_action_mismatch"):
            # SQLite permits a nullable FK and its removal; existing rows must
            # satisfy the target before a new or redirected FK is installed.
            # The model resolver independently establishes unique/PK target and
            # matching source/target types; the catalog/history are verified.
            if field.references is not None:
                from .schema import _resolved_foreign_keys
                target_table, target_column, _, _ = _resolved_foreign_keys(model, models)[name]
                missing = raw.execute(
                    f'SELECT COUNT(*) FROM {_quote(table, "sqlite")} AS source '
                    f'WHERE source.{_quote(name, "sqlite")} IS NOT NULL '
                    f'AND NOT EXISTS (SELECT 1 FROM {_quote(target_table, "sqlite")} AS target '
                    f'WHERE target.{_quote(target_column, "sqlite")} = '
                    f'source.{_quote(name, "sqlite")})'
                ).fetchone()[0]
                if missing:
                    raise UnsafeTableTransition(
                        f"Cannot reference {target_table}.{target_column} from {table}.{name}: "
                        f"{missing} existing value(s) have no referenced row. "
                        "No database changes were made")
            continue
        if change.kind == "unique_mismatch":
            if field.unique:
                # A single-column UNIQUE is safe only when existing non-NULL
                # values are actually unique. SQLite permits multiple NULLs.
                duplicated = raw.execute(
                    f'SELECT 1 FROM {_quote(table, "sqlite")} '
                    f'WHERE {_quote(name, "sqlite")} IS NOT NULL '
                    f'GROUP BY {_quote(name, "sqlite")} HAVING COUNT(*) > 1 LIMIT 1'
                ).fetchone()
                if duplicated is not None:
                    raise UnsafeTableTransition(
                        f"Cannot make {table}.{name} UNIQUE: existing non-NULL values "
                        "contain duplicates. No database changes were made")
            continue
        if change.kind == "default_mismatch":
            _default_literal(field, "sqlite")
            continue
        if change.kind == "nullability_mismatch":
            if not field.nullable:
                missing = raw.execute(
                    f'SELECT COUNT(*) FROM {_quote(table, "sqlite")} '
                    f'WHERE {_quote(name, "sqlite")} IS NULL').fetchone()[0]
                if missing:
                    raise UnsafeTableTransition(
                        f"Cannot require {table}.{name}: {missing} stored NULL value(s) "
                        "need a value before NOT NULL can be applied. "
                        "No database changes were made")
            continue
        existing = actual_columns.get(name, "").upper().replace(" ", "")
        desired = _sqlite_declared_type(field).upper().replace(" ", "")
        old_varchar = re.fullmatch(r"VARCHAR\((\d+)\)", existing)
        new_varchar = re.fullmatch(r"VARCHAR\((\d+)\)", desired)
        if old_varchar and new_varchar:
            if int(new_varchar[1]) < int(old_varchar[1]):
                # SQLite length() stops at NUL, so inspect decoded values.
                for (value,) in raw.execute(
                        f'SELECT {_quote(name, "sqlite")} FROM {_quote(table, "sqlite")} '
                        f'WHERE {_quote(name, "sqlite")} IS NOT NULL'):
                    if not isinstance(value, str):
                        raise UnsafeTableTransition(
                            f"Cannot narrow {table}.{name}: stored value is not text")
                    if len(value) > int(new_varchar[1]):
                        raise UnsafeTableTransition(
                            f"Cannot narrow {table}.{name} to VARCHAR({new_varchar[1]}): "
                            f"an existing value has {len(value)} characters; "
                            "truncation would occur. No database changes were made")
        elif existing == "INTEGER" and new_varchar:
            limit = int(new_varchar[1])
            for (value,) in raw.execute(
                    f'SELECT {_quote(name, "sqlite")} FROM {_quote(table, "sqlite")} '
                    f'WHERE {_quote(name, "sqlite")} IS NOT NULL'):
                if type(value) is not int or len(str(value)) > limit:
                    raise UnsafeTableTransition(
                        f"Cannot convert {table}.{name} from INTEGER to VARCHAR({limit}): "
                        "at least one stored value is incompatible or too long. "
                        "No database changes were made")
        elif old_varchar and desired == "INTEGER":
            for (value,) in raw.execute(
                    f'SELECT {_quote(name, "sqlite")} FROM {_quote(table, "sqlite")} '
                    f'WHERE {_quote(name, "sqlite")} IS NOT NULL'):
                if (not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value)
                        or len(value) > 20):
                    raise UnsafeTableTransition(
                        f"Cannot convert {table}.{name} from VARCHAR to INTEGER: "
                        "stored text is not a canonical integer. No database changes were made")
                number = int(value)
                if not -(2**63) <= number < 2**63:
                    raise UnsafeTableTransition(
                        f"Cannot convert {table}.{name} to INTEGER: stored value "
                        "exceeds the SQLite integer range. No database changes were made")
        else:
            raise UnsafeTableTransition(
                f"Cannot change {table}.{name} from {existing} to {desired}: "
                "this conversion has not been verified for lossless execution")
    indexes = [(idx_name, sql) for idx_name, idx_table, sql in _ordinary_index_sql(raw)
               if idx_table == table]
    if any(item["type"] in ("trigger", "view") for item in _physical(raw)):
        raise UnsafeTableTransition("Table transition has view or trigger dependencies")
    incoming = _incoming_foreign_keys(raw, table)

    if incoming:
        # A UNIQUE-only rebuild may leave existing child references intact,
        # but it must never modify a column referenced by those children.
        if kind != "unique_mismatch" or any(
                not referenced_column or referenced_column.casefold() == names[0].casefold()
                for _, referenced_column in incoming):
            raise UnsafeTableTransition(
                "Table transition has inbound foreign-key dependencies"
            )

    if any(key[2].casefold() == table.casefold() for key in raw.execute(
            f'PRAGMA foreign_key_list({_quote(table, "sqlite")})')):
        raise UnsafeTableTransition("Self-referencing foreign keys need separate review")
    target = names if kind == "table_transition" else names[0]
    return table, target, tuple(indexes), tuple(
        (item["type"], item["name"], item["sql"]) for item in prior["physical_schema"])


def review_default_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "default_mismatch")


def review_nullability_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "nullability_mismatch")


def review_unique_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "unique_mismatch")


def review_length_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "type_mismatch")


def migrate_default_change(raw, root, db_file, models, title, proposal, *, kind="default_mismatch",
                           reviewed_removals=(), acknowledged=()):
    """Rebuild one reviewed ordinary column change and retain existing rows."""
    from .schema import create_table_sql
    from .core import Connection

    table, name, indexes, _ = proposal
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.in_transaction or raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("Default migration requires an unlocked FK-enforced SQLite connection")

    # SQLite cannot drop a referenced parent while FK enforcement is enabled.
    # For an approved UNIQUE-only edit, temporarily disable enforcement on
    # this private writer and verify all relationships before committing.
    incoming_unique = (
        kind == "unique_mismatch" and bool(_incoming_foreign_keys(raw, table))
    )
    if incoming_unique:
        raw.execute("PRAGMA foreign_keys=OFF")
        if raw.execute("PRAGMA foreign_keys").fetchone() != (0,):
            raise RuntimeError("Cannot prepare the reviewed parent-table rebuild")

    path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        if _review_ordinary_rebuild(raw, root, db_file, models, kind) != proposal:
            raise RuntimeError("Default-change review is stale; rerun db check")
        if reviewed_removals:
            actual = review_column_removal(raw, root, db_file, models, allow_mixed=True)
            if actual != reviewed_removals:
                raise RuntimeError("Column-removal review is stale; rerun db check")
            dangerous = {(table_name, col) for table_name, col, count, _ in actual if count}
            if dangerous and not dangerous.issubset(set(acknowledged)):
                raise PermissionError("Populated column removal requires typed authorization")
        target = next(m for m in models if m.__table_name__ == table)
        temporary = "aurora_rebuild_" + hashlib.sha256(repr(proposal).encode()).hexdigest()[:24]
        target_sql = create_table_sql(target, "sqlite", models=models)
        temporary_sql = target_sql.replace(_quote(table, "sqlite"),
                                            _quote(temporary, "sqlite"), 1)
        if temporary_sql == target_sql:
            raise RuntimeError("Cannot construct reviewed SQLite rebuild")
        # Copy only columns that already exist. The target SQLite schema
        # populates eligible new fields from its DEFAULT or with NULL.
        old_columns = {row[1] for row in raw.execute(
            f'PRAGMA table_info({_quote(table, "sqlite")})')}
        copied = [name for name in target.__columns__ if name in old_columns]
        names = ", ".join(_quote(name, "sqlite") for name in copied)
        # Explicit conversion for changed storage types. Unchanged columns
        # retain their original values. SQLite enforces target affinity.
        current_types = {row[1]: row[2].upper().replace(" ", "") for row in raw.execute(
            f'PRAGMA table_info({_quote(table, "sqlite")})')}
        from .planning import _sqlite_declared_type
        expressions = []
        for field_name in copied:
            field = target.__columns__[field_name]
            source = _quote(field_name, "sqlite")
            old = current_types[field_name]
            desired = _sqlite_declared_type(field).upper().replace(" ", "")
            if old == "INTEGER" and re.fullmatch(r"VARCHAR\(\d+\)", desired):
                source = f"CAST({source} AS TEXT)"
            elif re.fullmatch(r"VARCHAR\(\d+\)", old) and desired == "INTEGER":
                source = f"CAST({source} AS INTEGER)"
            expressions.append(source)
        before = raw.execute(f'SELECT COUNT(*) FROM {_quote(table, "sqlite")}').fetchone()[0]
        raw.execute(temporary_sql)
        raw.execute(f'INSERT INTO {_quote(temporary, "sqlite")} ({names}) '
                    f'SELECT {", ".join(expressions)} FROM {_quote(table, "sqlite")}')
        raw.execute(f'DROP TABLE {_quote(table, "sqlite")}')
        raw.execute(f'ALTER TABLE {_quote(temporary, "sqlite")} RENAME TO {_quote(table, "sqlite")}')
        for _, sql in indexes:
            raw.execute(sql)
        after = raw.execute(f'SELECT COUNT(*) FROM {_quote(table, "sqlite")}').fetchone()[0]
        if before != after:
            raise RuntimeError("Default change altered the number of application rows")
        matches_models_with_indexes(raw, models)
        warnings = [f'Approved permanent deletion of {t}.{col}: {count} existing non-NULL '
                    f'value(s); backup recommended, not automatically created'
                    for t, col, count, _ in reviewed_removals if count]
        path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        if incoming_unique:
            raw.execute("PRAGMA foreign_keys=ON")
            if raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise RuntimeError("SQLite foreign keys could not be restored")

    return path


def migrate_column_edit(raw, root, db_file, models, title, proposal):
    """Rebuild one ordinary table under the same forward-history transaction.

    The proposal admits compatible widening, validated defaults, nullability
    and isolated single-column uniqueness changes without dependent objects.
    Never replay a proposal against unverified or concurrently changed schema.
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


def migrate_length_change(raw, root, db_file, models, title, proposal):
    """Change VARCHAR length; recheck stored values under lock when narrowing."""
    return migrate_default_change(raw, root, db_file, models, title, proposal,
                                  kind="type_mismatch")


def review_table_transition(raw, root, db_file, models):
    """Inspect a single coherent SQLite table transition before approval."""
    return _review_ordinary_rebuild(raw, root, db_file, models, "table_transition")


def migrate_table_transition(raw, root, db_file, models, title, proposal, *,
                             reviewed_removals=(), acknowledged=()):
    """Execute one reviewed table transition and record one forward snapshot."""
    return migrate_default_change(raw, root, db_file, models, title, proposal,
                                  kind="table_transition", reviewed_removals=reviewed_removals,
                                  acknowledged=acknowledged)
