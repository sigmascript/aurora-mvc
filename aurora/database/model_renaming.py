"""Stage exact Python model edits for expressly requested JSON schema renames.

Model-only column additions/removals are never inferred as renames. An explicit
rename changes the source only after its exact resulting schema is reviewed.
"""

import ast
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch

import sqlite3

from .model_discovery import model_identity_map, table_rename_candidates
from .schema import create_table_sql
from .migration_timeline import inspect_native_history
from .schema_inspection import _inspect_sqlite
from .migration_review import _fingerprint

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
from urllib.parse import urlsplit
from aurora.core.project_policy import project_is_production
from .migration_coordination import _PG_LOCK_KEY, migration_lock
from .schema import validate_init_models
from .schema_inspection import inspect_table
from .migration_history import history_table, read_history
from .initialization import _write_snapshot
from .schema_inspection import verify_server_declared_schema
from .snapshots import schema_snapshot


class ModelSourceRenameError(ValueError):
    """Known, safe refusal of an explicit source rename."""


def _source_model(root, reference):
    module, cls = reference.split(":", 1)
    path = (Path(root) / Path(*module.split("."))).with_suffix(".py")
    project = Path(root).resolve()
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(project):
        raise ModelSourceRenameError(f"Model source is not a regular project file: {reference}")
    source = path.read_bytes()
    try:
        value = source.decode("utf-8")
        tree = ast.parse(value, filename=str(path))
    except (UnicodeError, SyntaxError) as error:
        raise ModelSourceRenameError(f"Cannot safely edit model source: {reference}") from error
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls]
    if len(classes) != 1:
        raise ModelSourceRenameError(f"Model class is missing or ambiguous: {reference}")
    return path, source, value, classes[0]


def _span(lines, node):
    # AST offsets are UTF-8 byte positions, not Python character positions.
    return (
        sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset,
        sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset,
    )


def _edit_one(root, reference, operation, target_table, *, is_target, references):
    path, original, text, cls = _source_model(root, reference)
    lines = original.splitlines(keepends=True)
    replacements = []
    if is_target and operation["kind"] == "rename_columns":
        found = set()
        for stmt in cls.body:
            if not (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)
                and (
                    isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == "column"
                    or isinstance(stmt.value.func, ast.Attribute)
                    and stmt.value.func.attr == "column"
                )
            ):
                continue
            name = stmt.targets[0].id
            if name in operation["columns"]:
                start, end = _span(lines, stmt.targets[0])
                replacements.append((start, end, operation["columns"][name].encode()))
                found.add(name)
            if name in operation["columns"].values():
                raise ModelSourceRenameError(
                    f"Rename destination {name} already exists in Python model"
                )
        if found != set(operation["columns"]):
            raise ModelSourceRenameError(
                f"Rename source column(s) not found as simple model declarations: {set(operation['columns']) - found}"
            )
    elif is_target:
        # Keep the Python model class and path stable; only its physical table identity changes.
        mappings = [
            stmt
            for stmt in cls.body
            if isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "__table__"
        ]
        if len(mappings) > 1:
            raise ModelSourceRenameError("Ambiguous __table__ declarations")
        if mappings:
            stmt = mappings[0]
            if not isinstance(stmt.value, ast.Constant) or stmt.value.value != target_table:
                raise ModelSourceRenameError("Existing __table__ does not match the rename source")
            start, end = _span(lines, stmt.value)
            replacements.append((start, end, repr(operation["new_table"]).encode()))
        else:
            if not cls.body:
                raise ModelSourceRenameError("Cannot locate model class body")
            body = cls.body[0]
            # Keep an existing class docstring as the first statement.
            if (
                isinstance(body, ast.Expr)
                and isinstance(body.value, ast.Constant)
                and isinstance(body.value.value, str)
            ):
                if len(cls.body) < 2:
                    raise ModelSourceRenameError(
                        "Cannot safely insert table mapping after a standalone model docstring"
                    )
                body = cls.body[1]
            leading = lines[body.lineno - 1][: body.col_offset]
            if not leading or any(char not in b" \t" for char in leading):
                raise ModelSourceRenameError("Cannot safely insert explicit model table mapping")
            pos = sum(map(len, lines[: body.lineno - 1]))
            newline = b"\r\n" if b"\r\n" in original else b"\n"
            replacements.append(
                (
                    pos,
                    pos,
                    leading + b"__table__ = " + repr(operation["new_table"]).encode() + newline,
                )
            )
    for field_name, (old_reference, new_reference) in references.items():
        matches = [
            stmt
            for stmt in cls.body
            if isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == field_name
            and isinstance(stmt.value, ast.Call)
        ]
        if len(matches) != 1:
            raise ModelSourceRenameError(
                f"Cannot identify dependent foreign-key field {field_name}"
            )
        keywords = [kw for kw in matches[0].value.keywords if kw.arg in ("reference", "references")]
        if (
            len(keywords) != 1
            or not isinstance(keywords[0].value, ast.Constant)
            or keywords[0].value.value != old_reference
        ):
            raise ModelSourceRenameError(
                f"Cannot safely rewrite foreign-key reference for {field_name}"
            )
        start, end = _span(lines, keywords[0].value)
        replacements.append((start, end, repr(new_reference).encode()))
    changed = original
    for start, end, value in sorted(replacements, reverse=True):
        if not (0 <= start <= end <= len(changed)):
            raise ModelSourceRenameError("Invalid Python source position")
        changed = changed[:start] + value + changed[end:]
    if changed == original:
        raise ModelSourceRenameError("Explicit rename made no model-source change")
    try:
        ast.parse(changed.decode("utf-8"))
    except (SyntaxError, UnicodeError) as error:
        raise ModelSourceRenameError("Generated Python model source is invalid") from error
    return path, (original, changed)


def source_changes(root, refs, models, operation):
    target = [
        model
        for model in models
        if model.__table_name__.casefold() == operation["table"].casefold()
    ]
    if len(target) != 1:
        raise ModelSourceRenameError(
            "Rename target must correspond to exactly one registered Python model"
        )
    target = target[0]
    by_name = {model.__name__: model for model in models}
    by_table = {model.__table_name__: model for model in models}
    changes = {}
    for model in models:
        identity = f"{model.__module__}:{model.__name__}"
        if identity not in refs:
            raise ModelSourceRenameError("Cannot identify the registered Python model file")
        references = {}
        for field_name, field in model.__columns__.items():
            if field.references is None:
                continue
            prefix, column = field.references.split(".")
            linked = by_name.get(prefix) if field.model_reference else by_table.get(prefix)
            if linked is target:
                if operation["kind"] == "rename_table" and not field.model_reference:
                    updated = f"{operation['new_table']}.{column}"
                elif operation["kind"] == "rename_columns" and column in operation["columns"]:
                    updated = f"{prefix}.{operation['columns'][column]}"
                else:
                    continue
                references[field_name] = (field.references, updated)
        if model is target or references:
            path, pair = _edit_one(
                root,
                identity,
                operation,
                model.__table_name__,
                is_target=model is target,
                references=references,
            )
            if path.resolve() in changes:
                raise ModelSourceRenameError("Several model identities target the same source file")
            changes[path.resolve()] = pair
    return changes


@contextmanager
def staged_model_source(changes):
    """Present proposed bytes to fresh project model loader without touching disk."""
    from .cli_check import _FreshModelLoader

    original_get_data = _FreshModelLoader.get_data
    replacements = {str(path): new for path, (_, new) in changes.items()}

    def get_data(loader, path):
        return replacements.get(str(Path(path).resolve()), original_get_data(loader, path))

    with patch.object(_FreshModelLoader, "get_data", get_data):
        yield


def _replace(path, content, mode):
    # Atomic per-file replacement, preserving existing permission bits.
    descriptor, temporary = tempfile.mkstemp(prefix=".aurora-rename-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_source_changes(changes):
    """Apply pre-reviewed source edits; return a reversible source-change witness."""
    applied = []
    try:
        for path, (before, after) in changes.items():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != before:
                raise ModelSourceRenameError(
                    "Model source was changed after rename review; retry with the current source"
                )
            mode = stat.S_IMODE(path.stat().st_mode)
            _replace(path, after, mode)
            applied.append((path, before, after, mode))
    except BaseException:
        restore_source_changes(applied)
        raise
    return applied


def restore_source_changes(applied):
    for path, before, after, mode in reversed(applied):
        if path.is_symlink() or not path.is_file() or path.read_bytes() != after:
            raise RuntimeError(f"Cannot restore model source modified concurrently: {path}")
        _replace(path, before, mode)


def sqlite_model_rename_proposal(connection, models, alias, db_file):
    """Return one narrow rename proposal or None when no rename was requested."""
    timeline, _, _ = inspect_native_history(connection, alias)

    if not timeline or not timeline[-1]["physical_history_consistent"]:
        raise RuntimeError("Model rename requires verified native history")

    previous = timeline[-1]["model_tables"]
    current = model_identity_map(models)
    candidates = table_rename_candidates(previous, models)

    if not candidates:
        return None

    if len(candidates) != 1 or set(previous) != set(current):
        raise ValueError("Only one unchanged-model-identity table rename is supported")

    _, old, new = candidates[0]
    operation = {"kind": "rename_table", "table": old, "new_table": new}
    catalog, warnings = _inspect_sqlite(connection._raw, operation, alias, trusted_rename=True)

    if warnings:
        raise RuntimeError("Table rename dependencies require separate review")

    # Compare physical columns to the target model declarations. No model may
    # introduce a different column/constraint while requesting a table rename.
    with sqlite3.connect(":memory:") as expected:
        for model in models:
            expected.execute(create_table_sql(model, "sqlite"))

        for pragma in ("table_info", "index_list", "foreign_key_list"):
            old_rows = connection._raw.execute(f'PRAGMA {pragma}("{old}")').fetchall()
            new_rows = expected.execute(f'PRAGMA {pragma}("{new}")').fetchall()

            if old_rows != new_rows:
                raise RuntimeError("Table rename also changes model schema")

    document = {
        "format": "aurorasql.operations.v1",
        "database": alias,
        "operation": operation,
        "model_identity": current,
    }
    digest = _fingerprint(document, catalog, "sqlite", db_file)
    return document, digest, current


def _without_preview(tables):
    copies = json.loads(json.dumps(tables))
    for table in copies:
        table.pop("create_sql_preview", None)
    return copies


def _review(connection, before_models, after_models, root, alias, identity, operation):
    """Read-only review of one source field, with no unconstrained schema drift."""
    if connection.dialect != "postgresql" or operation.get("kind") != "rename_columns":
        raise ValueError("Only an explicit PostgreSQL column rename is supported")
    if len(operation["columns"]) != 1:
        raise ValueError("This server rename supports exactly one column")
    old, new = next(iter(operation["columns"].items()))
    if old == new:
        raise ValueError("Column rename must change its name")
    original = validate_init_models(before_models, dialect="postgresql", allow_extended=True)
    renamed = validate_init_models(after_models, dialect="postgresql", allow_extended=True)
    history, last = read_history(connection, root, alias)
    previous = _without_preview(last["models"])
    current = _without_preview(
        schema_snapshot(*original, dialect="postgresql", allow_v2=True)["tables"]
    )
    expected = _without_preview(
        schema_snapshot(*renamed, dialect="postgresql", allow_v2=True)["tables"]
    )
    if current != previous:
        raise ValueError("Existing model differs from verified predecessor snapshot")
    # History snapshots do not attest to trigger/partition/RLS features.
    # Require the complete supported catalog contract before modifying DDL.
    verify_server_declared_schema(
        connection, original, history_tables={history_table(connection)}, allow_extended=True
    )
    selected = [table for table in previous if table["name"] == operation["table"]]
    if len(selected) != 1:
        raise ValueError("Rename target is not one verified model table")
    fields = selected[0]["columns"]
    source = [field for field in fields if field["name"] == old]
    if len(source) != 1 or any(field["name"] == new for field in fields):
        raise ValueError("Old column must exist and new column must not exist")
    model = next(model for model in original if model.__table_name__ == operation["table"])
    field = model.__columns__[old]
    if field.primary_key or field.unique or field.references or field.default is not None:
        raise ValueError("Dependent, keyed or defaulted column rename requires separate review")
    projected = json.loads(json.dumps(previous))
    for table in projected:
        if table["name"] == operation["table"]:
            for column in table["columns"]:
                if column["name"] == old:
                    column["name"] = new
            table["columns"].sort(key=lambda column: column["name"])
    if projected != expected:
        raise ValueError("Rename must be the only model-schema change")
    observed = inspect_table(connection, operation["table"])
    if (
        not observed.exists
        or not observed.constraints_verified
        or observed.foreign_key_actions is None
        or set(observed.indexes) != {operation["table"] + "_pkey"}
        or observed.unique_columns
        or observed.foreign_keys
    ):
        raise ValueError("Rename target has unverified or dependent constraints or indexes")
    if old not in {column.name for column in observed.columns} or new in {
        column.name for column in observed.columns
    }:
        raise ValueError("Physical source/destination column identity differs")
    # No other table may contain an FK pointing at the target. The physical
    # witness validates all tables, but this dependency rule is intentionally strict.
    inbound = connection.fetch_first(
        "SELECT 1 AS present FROM pg_catalog.pg_constraint c "
        "JOIN pg_catalog.pg_class t ON t.oid=c.confrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
        "WHERE c.contype='f' AND n.nspname=current_schema() AND t.relname=:table LIMIT 1",
        {"table": operation["table"]},
    )
    if inbound:
        raise ValueError("Referenced table has foreign-key dependencies; rename refused")
    proposed = {"table": operation["table"], "old": old, "new": new}
    digest = sha256(
        json.dumps(
            {
                "alias": alias,
                "identity": identity,
                "history_count": len(history),
                "predecessor": last["physical_schema"],
                "operation": proposed,
                "new_models": expected,
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return proposed, digest, len(history)


def run_server_rename_operation(args, document, config, root, *, simple_confirm, interactive):
    """Require an explicitly reviewed JSON rename and safely edit one model."""
    from .cli_check import _select_models
    from .connections import _postgres_database_identity
    from .migration_review import migration_title

    if args.apply or args.approve or args.backup:
        raise PermissionError("Server renames require ordinary confirmation, not advanced flags")
    if not args.dry_run and project_is_production(root):
        raise PermissionError("Explicit server rename disabled in production")
    operation = document["operation"]
    if operation["kind"] != "rename_columns" or len(operation["columns"]) != 1:
        raise ValueError("Only a single explicit column rename is supported")
    refs, originals = _select_models(args, root)
    edits = source_changes(root, refs, originals, operation)
    if len(edits) != 1:
        raise ValueError("Rename involving multiple Python model files requires separate review")
    with staged_model_source(edits):
        _, successors = _select_models(args, root)
    title = migration_title(getattr(args, "title", None) or "Untitled", "Untitled")
    parsed = urlsplit(config.resolve(args.database))
    with config.connect(args.database) as connection:
        if connection.dialect != "postgresql":
            raise ValueError("MySQL explicit rename awaits a separate guarded executor")
        identity = _postgres_database_identity(connection, parsed)
        selected, digest, _ = _review(
            connection, originals, successors, root, args.database, identity, operation
        )
        if args.dry_run:
            if args.format == "json":
                print(
                    json.dumps(
                        {
                            "database_alias": args.database,
                            "dialect": "postgresql",
                            "operation": operation,
                            "status": "READY_FOR_REVIEW",
                            "review_fingerprint": digest,
                            "read_only": True,
                            "execution_allowed": False,
                        },
                        sort_keys=True,
                        indent=2,
                    )
                )
            else:
                print(f"Database: {args.database} (PostgreSQL; read-only rename review)")
                print(
                    f"  rename_column: {selected['table']}.{selected['old']} -> {selected['new']}"
                )
                print("Status: READY_FOR_REVIEW; no changes made")
            return 2 if args.check else 0
        if not (simple_confirm or interactive):
            raise PermissionError("PostgreSQL rename requires explicit confirmation")
        print(f"AuroraSQL migration — {args.database} (postgresql)")
        print(f"  rename_column: {selected['table']}.{selected['old']} -> {selected['new']}")
        print("  Python source: " + ", ".join(str(path.relative_to(root)) for path in edits))
        print("  Reviewed SHA-256: " + digest)
        if (
            interactive
            and input("Apply this rename and model-source change? [y/N]: ").strip().lower() != "y"
        ):
            print("Migration cancelled; no database changes made.")
            return 0
        applied = []
        with connection.transaction():
            acquired = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not acquired or acquired["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            if _postgres_database_identity(connection, parsed) != identity:
                raise RuntimeError("PostgreSQL database identity changed after review")
            fresh, witness, count = _review(
                connection, originals, successors, root, args.database, identity, operation
            )
            if fresh != selected or not hmac.compare_digest(witness, digest):
                raise RuntimeError("Rename proposal changed after review")

            # All identifiers originate from the validated explicit JSON file.
            def q(name):
                return '"' + name.replace('"', '""') + '"'

            try:
                connection.execute_write(
                    f"ALTER TABLE {q(selected['table'])} RENAME COLUMN "
                    f"{q(selected['old'])} TO {q(selected['new'])}"
                )
                verify_server_declared_schema(
                    connection,
                    successors,
                    history_tables={history_table(connection)},
                    allow_extended=True,
                )
                applied = apply_source_changes(edits)
                path = _write_snapshot(connection, root, args.database, successors, title)
                row = {
                    "id": count + 1,
                    "title": title,
                    "snapshot": path.name,
                    "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "warnings": None,
                }
                connection.execute_write(
                    "INSERT INTO "
                    + history_table(connection)
                    + " (id,title,snapshot,date,warnings) "
                    "VALUES (:id,:title,:snapshot,:date,:warnings)",
                    row,
                )
                recorded, _ = read_history(connection, root, args.database)
                if len(recorded) != row["id"] or any(
                    recorded[-1][key] != value for key, value in row.items()
                ):
                    raise RuntimeError("Renamed schema history completion failed")
            except BaseException:
                if applied:
                    restore_source_changes(applied)
                raise
        print(f"Applied PostgreSQL explicit rename on {args.database}.")
        print("Schema snapshot: " + str(path))
        return 0


# MySQL explicit rename preserves nontransactional DDL and uncertain outcomes.


def _tables(models):
    tables = json.loads(json.dumps(models))
    for table in tables:
        table.pop("create_sql_preview", None)
    return tables


def _dependencies(connection, table):
    """Refuse inbound FK dependencies even when not included in selected models."""
    inbound = connection.fetch_first(
        "SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME=:table "
        "LIMIT 1",
        {"table": table},
    )
    if inbound:
        raise ValueError("Referenced table has foreign-key dependencies; rename refused")
    triggers = connection.fetch_first(
        "SELECT 1 AS present FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
        {"table": table},
    )
    if triggers:
        raise ValueError("Table triggers require separate rename review")


def review_mysql_rename(
    connection, original_models, renamed_models, root, alias, identity, operation
):
    if connection.dialect != "mysql" or operation.get("kind") != "rename_columns":
        raise ValueError("Explicit MySQL rename reviewer requires MySQL")
    if len(operation["columns"]) != 1:
        raise ValueError("Exactly one column rename is supported")
    old, new = next(iter(operation["columns"].items()))
    if old == new:
        raise ValueError("Rename must change the name")
    before = validate_init_models(original_models, dialect="mysql", allow_extended=True)
    after = validate_init_models(renamed_models, dialect="mysql", allow_extended=True)
    history, latest = read_history(connection, root, alias)
    previous = _tables(latest["models"])
    source = _tables(schema_snapshot(*before, dialect="mysql", allow_v2=True)["tables"])
    target = _tables(schema_snapshot(*after, dialect="mysql", allow_v2=True)["tables"])
    if previous != source:
        raise ValueError("Model differs from verified predecessor snapshot")
    verify_server_declared_schema(
        connection, before, history_tables={history_table(connection)}, allow_extended=True
    )
    matches = [t for t in previous if t["name"] == operation["table"]]
    if len(matches) != 1:
        raise ValueError("Target is not one verified model table")
    fields = matches[0]["columns"]
    if len([f for f in fields if f["name"] == old]) != 1 or any(f["name"] == new for f in fields):
        raise ValueError("Expected old column absent or destination already exists")
    model = next(m for m in before if m.__table_name__ == operation["table"])
    field = model.__columns__[old]
    if field.primary_key or field.unique or field.references or field.default is not None:
        raise ValueError("Dependent, keyed or defaulted column requires separate review")
    projected = json.loads(json.dumps(previous))
    for t in projected:
        if t["name"] == operation["table"]:
            for col in t["columns"]:
                if col["name"] == old:
                    col["name"] = new
            t["columns"].sort(key=lambda item: item["name"])
    if projected != target:
        raise ValueError("Rename must be the only model-schema change")
    observed = inspect_table(connection, operation["table"])
    if (
        not observed.exists
        or not observed.constraints_verified
        or observed.foreign_key_actions is None
        or set(observed.indexes) != {"PRIMARY"}
        or observed.unique_columns
        or observed.foreign_keys
    ):
        raise ValueError("Unverified or dependent indexes or constraints prevent rename")
    if old not in {c.name for c in observed.columns} or new in {c.name for c in observed.columns}:
        raise ValueError("Physical column identity differs from requested rename")
    _dependencies(connection, operation["table"])
    selected = {"table": operation["table"], "old": old, "new": new}
    digest = sha256(
        json.dumps(
            {
                "alias": alias,
                "identity": identity,
                "count": len(history),
                "predecessor": latest["physical_schema"],
                "operation": selected,
                "new_models": target,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return selected, digest, len(history)


def run_mysql_rename_operation(args, document, config, root, *, simple_confirm, interactive):
    from .cli_check import _select_models
    from .connections import _mysql_database_identity
    from .migration_review import migration_title

    if args.apply or args.approve or args.backup:
        raise PermissionError("MySQL rename requires ordinary interactive confirmation")
    if not args.dry_run and project_is_production(root):
        raise PermissionError("MySQL explicit rename disabled in production")
    operation = document["operation"]
    if operation["kind"] != "rename_columns" or len(operation["columns"]) != 1:
        raise ValueError("Only one explicit column rename is supported")
    refs, original = _select_models(args, root)
    edits = source_changes(root, refs, original, operation)
    if len(edits) != 1:
        raise ValueError("Rename affecting multiple model files requires separate review")
    with staged_model_source(edits):
        _, renamed = _select_models(args, root)
    title = migration_title(getattr(args, "title", None) or "Untitled", "Untitled")
    parsed = urlsplit(config.resolve(args.database))
    with config.connect(args.database) as connection:
        if connection.dialect != "mysql":
            raise ValueError("MySQL rename requires MySQL")
        identity = _mysql_database_identity(connection, parsed)
        selected, digest, _ = review_mysql_rename(
            connection, original, renamed, root, args.database, identity, operation
        )
        if args.dry_run:
            if args.format == "json":
                print(
                    json.dumps(
                        {
                            "database_alias": args.database,
                            "dialect": "mysql",
                            "operation": operation,
                            "status": "READY_FOR_REVIEW",
                            "review_fingerprint": digest,
                            "read_only": True,
                            "execution_allowed": False,
                        },
                        sort_keys=True,
                        indent=2,
                    )
                )
            else:
                print(f"Database: {args.database} (MySQL; read-only rename review)")
                print(
                    f"  rename_column: {selected['table']}.{selected['old']} -> {selected['new']}"
                )
                print("Status: READY_FOR_REVIEW; no changes made")
            return 2 if args.check else 0
        if not (simple_confirm or interactive):
            raise PermissionError("MySQL rename requires explicit confirmation")
        print(f"AuroraSQL migration — {args.database} (mysql)")
        print(f"  rename_column: {selected['table']}.{selected['old']} -> {selected['new']}")
        print("  Python source: " + ", ".join(str(p.relative_to(root)) for p in edits))
        print("  Reviewed SHA-256: " + digest)
        if (
            interactive
            and input("Apply this rename and model-source change? [y/N]: ").strip().lower() != "y"
        ):
            print("Migration cancelled; no database changes made.")
            return 0

        def quote(identifier):
            return "`" + identifier.replace("`", "``") + "`"

        with migration_lock(connection):
            if _mysql_database_identity(connection, parsed) != identity:
                raise RuntimeError("MySQL identity changed after rename review")
            fresh, witness, count = review_mysql_rename(
                connection, original, renamed, root, args.database, identity, operation
            )
            if fresh != selected or not hmac.compare_digest(witness, digest):
                raise RuntimeError("MySQL rename proposal changed after review")
            # No implicit rollback is possible after the following DDL begins.
            try:
                connection.execute_write(
                    f"ALTER TABLE {quote(selected['table'])} RENAME COLUMN "
                    f"{quote(selected['old'])} TO {quote(selected['new'])}"
                )
                verify_server_declared_schema(
                    connection,
                    renamed,
                    history_tables={history_table(connection)},
                    allow_extended=True,
                )
                # Keep successfully synchronized source if journaling subsequently fails.
                # The physical DB is already changed, so manual reconciliation is mandatory.
                apply_source_changes(edits)
                path = _write_snapshot(connection, root, args.database, renamed, title)
                row = {
                    "id": count + 1,
                    "title": title,
                    "snapshot": path.name,
                    "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "warnings": None,
                }
                connection.execute_write(
                    "INSERT INTO "
                    + history_table(connection)
                    + " (id,title,snapshot,date,warnings) "
                    "VALUES (:id,:title,:snapshot,:date,:warnings)",
                    row,
                )
                recorded, _ = read_history(connection, root, args.database)
                if len(recorded) != row["id"] or any(recorded[-1][k] != v for k, v in row.items()):
                    raise RuntimeError("MySQL rename history completion failed")
            except Exception as exc:
                raise RuntimeError(
                    "MySQL rename outcome uncertain after DDL began; "
                    "inspect physical schema, Python source and history before retry"
                ) from exc
        print(f"Applied MySQL explicit rename on {args.database}.")
        print("Schema snapshot: " + str(path))
        return 0


# SQLite forward-history ownership (R4.15).
def sqlite_apply_explicit_rename(
    raw, root, db_file, models, title, operation, *, source_edits=None
):
    """Apply a model-reconciled rename and record one forward migration.

    Only renames with a complete matching resulting model schema are eligible.
    SQLite transaction rollback protects both application DDL and history rows.
    """
    from pathlib import Path

    from .migration_execution import _sql_statements
    from .migration_history import (
        sqlite_new_record as _new_record,
    )
    from .migration_history import (
        sqlite_read_history as read_history,
    )
    from .migration_review import _rehearse_sqlite
    from .migration_review import sqlite_matches_models as matches_models
    from .schema import _quote, validate_init_models
    from .schema_inspection import _catalog

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
            f"SELECT COUNT(*) FROM {_quote(operation['table'], 'sqlite')}"
        ).fetchone()[0]
        for statement in statements:
            raw.execute(statement)
        if _catalog(raw) != expected_catalog:
            raise RuntimeError("Migration result differs from the reviewed schema")
        after_table = operation.get("new_table", operation["table"])
        after_count = raw.execute(
            f"SELECT COUNT(*) FROM {_quote(after_table, 'sqlite')}"
        ).fetchone()[0]
        if before_count != after_count:
            raise RuntimeError("Migration unexpectedly changed the application row count")
        matches_models(raw, models)
        applied = []
        if source_edits:
            from .model_renaming import apply_source_changes

            applied = apply_source_changes(source_edits)
        path = _new_record(raw, root, db_file, models, title)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        if locals().get("applied"):
            from .model_renaming import restore_source_changes

            restore_source_changes(applied)
        raise
    return path
