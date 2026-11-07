"""Validate explicit schema-change files and preview them without database writes.

The v1 format intentionally describes one table operation. No JSON input is
executable SQL or migration permission; execution requires a separate reviewed
backend-specific implementation.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
from contextlib import closing
import re
import sqlite3
import secrets
import sys
from urllib.parse import urlsplit

from .cli_backup import _sqlite_source, run_backup
from .cli_plan import _load_config
from .migration_journal_v2 import _TABLE, _require_journal, _validate_intent
from .migration_timeline import (complete_successor, inspect_native_history,
                                 prepare_successor_mapping)


_FORMAT = "aurorasql.operations.v1"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_MAX_BYTES = 256 * 1024
_RESERVED_PREFIX = "sqlite_"
_RESERVED_TABLES = frozenset({
    "aurorasql_migration_intents_v2",
    "aurorasql_schema_history",
    "aurorasql_additive_history_v1",
})


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid schema-operation identifier")

    if value.casefold().startswith(_RESERVED_PREFIX):
        raise ValueError("Reserved SQLite schema identifier")

    return value


def _unique_names(names):
    normalized = [name.casefold() for name in names]

    if len(set(normalized)) != len(normalized):
        raise ValueError("Duplicate or case-colliding schema identifiers")


def _unique_json_pairs(items):
    result = {}

    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key in schema operations")

        result[key] = value

    return result


def _reject_constant(value):
    raise ValueError("Non-JSON numeric constant is not supported")


def load_explicit_operations(source):
    """Load one reviewed structural operation with a strict normalized shape."""
    path = Path(source).expanduser()

    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError("Operations file must be an existing small regular file")

    try:
        raw = path.read_bytes()

        if len(raw) > _MAX_BYTES:
            raise ValueError("Operations file is too large")

        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_pairs,
                              parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot decode operations JSON") from error

    if not isinstance(document, dict) or document.get("format", _FORMAT) != _FORMAT:
        raise ValueError("Unsupported schema-operation format")

    alias = _identifier(document.get("database"))
    operations = set(document) - {"format", "database"}

    if operations == {"rename_table"}:
        mapping = document["rename_table"]

        if not isinstance(mapping, dict) or len(mapping) != 1:
            raise ValueError("rename_table requires exactly one old/new table pair")

        original, replacement = next(iter(mapping.items()))
        _identifier(original)
        _identifier(replacement)
        _unique_names((original, replacement))

        if original.casefold() in _RESERVED_TABLES or replacement.casefold() in _RESERVED_TABLES:
            raise ValueError("Migration journals cannot be renamed")

        normalized = {"kind": "rename_table", "table": original,
                      "new_table": replacement}
    elif operations == {"rename_columns"}:
        tables = document["rename_columns"]

        if not isinstance(tables, dict) or len(tables) != 1:
            raise ValueError("rename_columns requires exactly one target table")

        table, changes = next(iter(tables.items()))
        _identifier(table)

        if table.casefold() in _RESERVED_TABLES or not isinstance(changes, dict) or not 1 <= len(changes) <= 20:
            raise ValueError("Invalid rename_columns target or column count")

        for original, replacement in changes.items():
            _identifier(original)
            _identifier(replacement)
            _unique_names((original, replacement))

        _unique_names(changes.keys())
        _unique_names(changes.values())

        if {key.casefold() for key in changes} & {value.casefold() for value in changes.values()}:
            raise ValueError("Chained or cyclic column renames require separate operations")

        normalized = {"kind": "rename_columns", "table": table,
                      "columns": dict(sorted(changes.items()))}
    elif operations == {"create_index"}:
        spec = document["create_index"]

        if not isinstance(spec, dict) or set(spec) - {"table", "name", "columns", "unique"}:
            raise ValueError("create_index has unsupported fields")

        table = _identifier(spec.get("table"))
        name = _identifier(spec.get("name"))
        columns = spec.get("columns")
        unique = spec.get("unique", False)

        if (table.casefold() in _RESERVED_TABLES or
                not isinstance(columns, list) or not 1 <= len(columns) <= 16 or
                type(unique) is not bool):
            raise ValueError("Invalid create_index specification")

        for column in columns:
            _identifier(column)

        _unique_names(columns)
        normalized = {"kind": "create_index", "table": table, "name": name,
                      "columns": columns, "unique": unique}
    else:
        raise ValueError("Exactly one supported schema operation is required")

    return {"format": _FORMAT, "database": alias, "operation": normalized}


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _catalog(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def _native_history(connection, alias):
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aurorasql_migration_timeline'"
    ).fetchone() is None:
        return None

    from .core import Connection
    native = Connection(connection, "sqlite")
    timeline, _, _ = inspect_native_history(native, alias)
    return timeline


def _inspect_sqlite(connection, operation, alias="default", *, trusted_rename=False):
    """Inspect a target and refuse dependencies that this executor cannot prove."""
    catalog = _catalog(connection)
    tables = {name.casefold(): name for kind, name, _, _ in catalog if kind == "table"}
    requested = operation["table"].casefold()

    if requested not in tables:
        raise ValueError("Target table does not exist")

    table = tables[requested]
    details = connection.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
    columns = {row[1].casefold(): row for row in details}
    warnings = []

    if operation["kind"] == "rename_table":
        existing_names = {name.casefold() for _, name, _, _ in catalog}

        if operation["new_table"].casefold() in existing_names:
            raise ValueError("Destination schema name already exists")
    elif operation["kind"] == "create_index":
        index_names = {name.casefold() for _, name, _, _ in catalog}

        if operation["name"].casefold() in index_names:
            raise ValueError("Index name already exists")

        for column in operation["columns"]:
            if column.casefold() not in columns:
                raise ValueError("Indexed column does not exist")

            if columns[column.casefold()][6]:
                warnings.append("Hidden or generated columns need dedicated index review")
    else:
        for old, new in operation["columns"].items():
            if old.casefold() not in columns:
                raise ValueError("Source column does not exist")

            if new.casefold() in columns:
                raise ValueError("Destination column already exists")

            if columns[old.casefold()][5] or columns[old.casefold()][6]:
                warnings.append("Primary-key or hidden-column rename needs dedicated review")

    for kind, name, _, _ in catalog:
        if kind in ("trigger", "view"):
            warnings.append("Triggers and views require separate dependency verification")
            break

    for name in tables.values():
        keys = connection.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall()

        if any((row[2].casefold() == requested or name.casefold() == requested)
               for row in keys):
            warnings.append("Foreign-key dependencies require separate verification")
            break

    for kind, _, parent, _ in catalog:
        if (operation["kind"] != "create_index" and kind == "index" and
                parent.casefold() == requested):
            warnings.append("Indexes on the target require separate verification")
            break

    # Only ordinary tables with transparent SQL DDL are supported. Virtual,
    # generated and advanced constraints need a separate backend-specific path.
    ddl = next((sql for kind, name, _, sql in catalog
                if kind == "table" and name == table), None)

    if not isinstance(ddl, str) or not re.match(r"CREATE\s+TABLE\b", ddl, re.I):
        warnings.append("Nonordinary table DDL is unsupported")

    if ddl and re.search(r"\b(CHECK|GENERATED|COLLATE|WITHOUT\s+ROWID|STRICT)\b", ddl, re.I):
        warnings.append("Advanced table constraints require separate verification")

    # Explicit operations currently have a legacy v2 journal executor. Never
    # mutate a database with native linear history without extending that chain
    # in the same transaction; this requires model-set reconciliation first.
    if "aurorasql_migration_timeline" in tables:
        if operation["kind"] == "rename_table" and not trusted_rename:
            warnings.append("Native migration timeline needs integrated table-rename identity")
        else:
            try:
                native = _native_history(connection, alias)

                if not native or not native[-1]["physical_history_consistent"]:
                    warnings.append("Native migration history is not verified")
            except (RuntimeError, ValueError):
                warnings.append("Native migration history is not verified")

    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite database integrity check failed")

    for journal in ("aurorasql_schema_history", "aurorasql_additive_history_v1"):
        if journal.casefold() not in tables:
            continue

        recorded = connection.execute(
            f'SELECT COUNT(*) FROM {_quote(journal)} WHERE table_name=?', (table,)
        ).fetchone()[0]

        if recorded and operation["kind"] == "rename_table" and not trusted_rename:
            warnings.append("Historical table journal requires rename-aware reconciliation")

        unresolved = connection.execute(
            f'SELECT COUNT(*) FROM {_quote(journal)} WHERE status<>?', ("applied",)
        ).fetchone()[0]

        if unresolved:
            warnings.append("Unresolved legacy or additive migration history")

    try:
        _require_journal(connection)
        entries = connection.execute(
            f'SELECT migration_id,review_sha256,before_sha256,after_sha256,state FROM "{_TABLE}"'
        ).fetchall()

        for entry in entries:
            _validate_intent(*entry[:4])

            if entry[4] != "applied":
                warnings.append("Unresolved migration intent blocks execution")
    except RuntimeError:
        warnings.append("Missing or incompatible migration intent journal")

    return catalog, sorted(set(warnings))


def _fingerprint(document, catalog, dialect, database_path=None):
    body = {"document": document, "catalog": catalog, "dialect": dialect,
            "database_identity": str(database_path) if database_path else None}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _catalog_digest(catalog):
    serialized = json.dumps(catalog, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sql_statements(operation):
    table = _quote(operation["table"])

    if operation["kind"] == "rename_table":
        return [f'ALTER TABLE {table} RENAME TO {_quote(operation["new_table"])}']

    if operation["kind"] == "create_index":
        columns = ", ".join(_quote(name) for name in operation["columns"])
        prefix = "CREATE UNIQUE INDEX" if operation["unique"] else "CREATE INDEX"
        return [f'{prefix} {_quote(operation["name"])} ON {table} ({columns})']

    return [f'ALTER TABLE {table} RENAME COLUMN {_quote(old)} TO {_quote(new)}'
            for old, new in operation["columns"].items()]


def _rehearse_sqlite(db_file, statements):
    """Rehearse on a bounded in-memory same-engine copy, preserving all rows."""
    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as source:
        pages = source.execute("PRAGMA page_count").fetchone()[0]
        size = source.execute("PRAGMA page_size").fetchone()[0]

        if pages * size > 64 * 1024 * 1024:
            raise ValueError("Rehearsal database exceeds the bounded 64 MiB limit")

        clone = sqlite3.connect(":memory:")

        try:
            source.backup(clone)
            clone.execute("PRAGMA foreign_keys=ON")
            clone.execute("PRAGMA legacy_alter_table=OFF")

            for statement in statements:
                clone.execute(statement)

            if clone.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Staging migration violates foreign-key constraints")

            if clone.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Staging migration integrity check failed")

            return _catalog(clone)
        finally:
            clone.close()


def _apply_sqlite_operations(args, document, db_file, preview_digest, *, model_mapping=None):
    """Apply one reviewed, backed-up rename after a bounded SQLite rehearsal."""
    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Explicit schema migration is disabled in production")

    if (args.confirm != "APPLY" or not isinstance(args.approve, str) or
            not args.backup or not hmac.compare_digest(args.approve, preview_digest)):
        raise PermissionError("Require --confirm APPLY, exact --approve and --backup")

    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as probe:
        native = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aurorasql_migration_timeline'"
        ).fetchone() is not None

    if getattr(args, "title", None) is not None and not native:
        raise ValueError("Migration titles require native linear history")

    title = "Untitled"

    if native:
        from .migration_titles import migration_title
        title = migration_title(getattr(args, "title", None), "Untitled")

    statements = _sql_statements(document["operation"])
    expected_catalog = _rehearse_sqlite(db_file, statements)

    # Back up with the existing verified, exclusive-create SQLite implementation.
    # Retain the artifact even if DDL subsequently fails, for manual recovery.
    from types import SimpleNamespace
    backup_args = SimpleNamespace(config=args.config, database=args.database,
                                  to=args.backup)
    run_backup(backup_args)

    connection = sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True,
                                 isolation_level=None, timeout=5)

    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA legacy_alter_table=OFF")
        connection.execute("BEGIN IMMEDIATE")
        before, warnings = _inspect_sqlite(
            connection, document["operation"], args.database,
            trusted_rename=model_mapping is not None,
        )
        current_digest = _fingerprint(document, before, "sqlite", db_file)

        if warnings or not hmac.compare_digest(current_digest, preview_digest):
            raise RuntimeError("Schema changed or migration preflight is unresolved")

        _require_journal(connection)
        operation_id = "schema_" + preview_digest[:40]
        before_sha = _catalog_digest(before)
        after_sha = _catalog_digest(expected_catalog)
        _validate_intent(operation_id, preview_digest, before_sha, after_sha)
        native = None

        if document["operation"]["kind"] in ("create_index", "rename_columns") or model_mapping is not None:
            native = _native_history(connection, args.database)

        successor_number = None

        if native is not None:
            successor_number = prepare_successor_mapping(
                connection, args.database, operation_id, preview_digest,
                model_mapping if model_mapping is not None else native[-1]["model_tables"], title,
            )

        initial_count = connection.execute(
            f'SELECT COUNT(*) FROM {_quote(document["operation"]["table"])}'
        ).fetchone()[0]

        # The intent and DDL commit atomically on SQLite, never on MySQL.
        connection.execute(
            f'INSERT INTO "{_TABLE}" (migration_id,review_sha256,before_sha256,after_sha256,state) '
            'VALUES (?,?,?,?,?)',
            (operation_id, preview_digest, before_sha, after_sha, "prepared"),
        )

        for statement in statements:
            connection.execute(statement)

        actual = _catalog(connection)

        if actual != expected_catalog:
            raise RuntimeError("Executed schema differs from rehearsal")

        new_table = document["operation"].get("new_table", document["operation"]["table"])
        final_count = connection.execute(
            f'SELECT COUNT(*) FROM {_quote(new_table)}'
        ).fetchone()[0]

        if initial_count != final_count:
            raise RuntimeError("Migration unexpectedly changed row count")

        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Migration created a foreign-key violation")

        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Migration integrity verification failed")

        changed = connection.execute(
            f'UPDATE "{_TABLE}" SET state=? WHERE migration_id=? AND state=?',
            ("applied", operation_id, "prepared"),
        ).rowcount

        if changed != 1:
            raise RuntimeError("Intent journal transition failed")

        if successor_number is not None:
            complete_successor(connection, successor_number, operation_id)

            verified = _native_history(connection, args.database)

            if (verified is None or verified[-1]["number"] != successor_number or
                    not verified[-1]["physical_history_consistent"]):
                raise RuntimeError("Native history postcondition failed")

        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f"Applied reviewed SQLite {document['operation']['kind']} on {args.database}; "
          f"backup retained at {args.backup}.")
    return 0


def run_explicit_operations(args):
    """Route a validated specification to advisory planning or guarded SQLite DDL."""
    if (args.review_file or args.name or args.schema or args.table or args.column or
            args.approve_review or args.approve_sql or args.output_dir != "migrations" or
            args.model or args.explain or args.models_config != "aurora_models.json"):
        raise ValueError("Explicit operations cannot be combined with legacy or model flags")

    if args.dry_run and (args.apply or args.approve or args.confirm or args.backup):
        raise ValueError("Read-only preview cannot include execution flags")

    interactive = not args.dry_run and not args.apply

    if not args.dry_run and (args.check or args.format != "text"):
        raise PermissionError("Explicit operation execution cannot use preview options")

    if interactive and (args.approve or args.confirm or args.backup or not sys.stdin.isatty()):
        raise PermissionError("Interactive schema changes require a terminal")

    config, project_root = _load_config(args.config)
    location = Path(args.changes).expanduser()

    if not location.is_absolute():
        location = project_root / location

    document = load_explicit_operations(location)

    if document["database"] != args.database:
        raise ValueError("Operations file database alias differs from selected alias")

    url = config.resolve(args.database)
    dialect = urlsplit(url).scheme.split("+")[0]

    if dialect == "postgres":
        dialect = "postgresql"

    if dialect not in ("sqlite", "mysql", "postgresql"):
        raise ValueError("Unsupported schema-operation backend")

    warnings = []
    catalog = None
    db_file = None

    if dialect == "sqlite":
        db_file = _sqlite_source(url, project_root)

        with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            catalog, warnings = _inspect_sqlite(connection, document["operation"], args.database)
    else:
        warnings.append("Backend-specific schema-operation inspection is not implemented")

    digest = _fingerprint(document, catalog, dialect, db_file)

    if not args.dry_run:
        if dialect != "sqlite" or warnings:
            raise PermissionError("Backend or schema is not eligible for execution")

        if interactive:
            operation = document["operation"]
            print(f"AuroraSQL migration — {args.database} (SQLite)")
            print(f"  {operation['kind']}: {operation['table']}")

            if input("Apply this reviewed operation? [y/N]: ").strip().lower() != "y":
                print("Migration cancelled; no database changes made.")
                return 0

            from types import SimpleNamespace
            args = SimpleNamespace(**vars(args))
            args.confirm = "APPLY"
            args.approve = digest
            args.backup = str(Path(str(db_file) + ".before-" + secrets.token_hex(8) + ".sqlite3"))

        return _apply_sqlite_operations(args, document, db_file, digest)

    report = {
        "database_alias": args.database,
        "dialect": dialect,
        "operation": document["operation"],
        "review_fingerprint": digest,
        "status": "REFUSED" if warnings else "READY_FOR_REVIEW",
        "warnings": warnings,
        "read_only": True,
        "execution_allowed": False,
    }

    if args.format == "json":
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print(f"Database: {args.database} ({dialect}; explicit operation preview)")
        print(f"Operation: {document['operation']['kind']} on {document['operation']['table']}")
        print(f"Status: {report['status']}; review and backup required before execution")
        print(f"Review fingerprint: {digest}")

        for warning in warnings:
            print(f"  [REFUSED] {warning}")

    if args.check:
        return 3 if warnings else 2

    return 0
