"""Constrained model-driven edits to ordinary SQLite columns."""

from hashlib import sha256
import hmac
import json
import re
import sqlite3

from .cli_explicit_operations import (_catalog, _catalog_digest, _fingerprint,
                                       _rehearse_sqlite)
from .core import Connection
from .inspection import inspect_table
from .migration_journal_v2 import _TABLE, _require_journal, _validate_intent
from .migration_timeline import (complete_successor, inspect_native_history,
                                 prepare_successor_mapping)
from .model_identity import model_identity_map
from .planning import plan_schema
from .schema import _default_literal, create_table_sql
from .snapshots import schema_snapshot
from .sqlite_additive_migration import _verify_result_catalog


_VARCHAR = re.compile(r"VARCHAR\((\d+)\)\Z", re.I)
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)\Z", re.I)


def _compatible_type(old, new):
    if old.upper() == new.upper():
        return True

    old_text, new_text = _VARCHAR.fullmatch(old), _VARCHAR.fullmatch(new)

    if old_text and new_text:
        return int(new_text[1]) >= int(old_text[1])

    old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)

    if old_decimal and new_decimal:
        before_p, before_s = map(int, old_decimal.groups())
        after_p, after_s = map(int, new_decimal.groups())
        return after_s >= before_s and after_p - after_s >= before_p - before_s

    return False


def sqlite_column_edit_proposal(connection, models, alias, db_file):
    """Return a single ordinary-table edit or None when no such edit exists."""
    timeline, _, _ = inspect_native_history(connection, alias)

    if not timeline or not timeline[-1]["physical_history_consistent"]:
        raise RuntimeError("Column editing requires verified native history")

    mapping = model_identity_map(models)

    if mapping != timeline[-1]["model_tables"]:
        return None

    target = None
    operations = []

    for model in models:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if target is not None or not plan.exists:
            return None

        accepted = {"type_mismatch", "nullability_mismatch", "default_mismatch"}

        if any(change.kind not in accepted for change in plan.changes):
            return None

        observed = {field.name: field for field in inspect_table(
            connection, model.__table_name__).columns}

        if set(observed) != set(model.__columns__):
            return None

        table = model.__table_name__
        quoted = '"' + table + '"'
        objects = _catalog(connection._raw)

        if any(kind in ("view", "trigger") or (kind == "index" and parent == table)
               for kind, _, parent, _ in objects):
            raise RuntimeError("Column edit with schema dependencies is unsupported")

        for other in models:
            keys = connection._raw.execute(
                f'PRAGMA foreign_key_list("{other.__table_name__}")'
            ).fetchall()

            if other.__table_name__ == table and keys or any(row[2] == table for row in keys):
                raise RuntimeError("Column edit with foreign-key dependencies is unsupported")

        for change in plan.changes:
            field = model.__columns__[change.column]
            current = observed[change.column]

            if field.primary_key or field.unique or field.references is not None or field.server_default is not None:
                raise ValueError("Key or constraint editing needs separate reviewed intent")

            if change.kind == "type_mismatch":
                from .planning import _sqlite_declared_type

                if not _compatible_type(current.sql_type, _sqlite_declared_type(field)):
                    raise ValueError("Unsupported or narrowing column type change")

            if change.kind == "nullability_mismatch" and not field.nullable:
                count = connection._raw.execute(
                    f'SELECT COUNT(*) FROM {quoted} WHERE "{change.column}" IS NULL'
                ).fetchone()[0]

                if count:
                    raise ValueError("Existing NULL values prevent required column edit")

            if change.kind == "default_mismatch":
                _default_literal(field, "sqlite")

            operations.append({"kind": change.kind, "table": table, "column": change.column})

        target = model

    if target is None:
        return None

    after = schema_snapshot(*models, dialect="sqlite")
    document = {"database": alias, "operations": operations, "after": after,
                "model_identity": mapping}
    catalog = _catalog(connection._raw)
    digest = _fingerprint(document, catalog, "sqlite", db_file)
    temporary = "aurora_rebuild_" + digest[:24]
    target_sql = create_table_sql(target, "sqlite")
    create = target_sql.replace(f'"{target.__table_name__}"', f'"{temporary}"', 1)
    columns = ", ".join(f'"{name}"' for name in target.__columns__)
    statements = [create,
                  f'INSERT INTO "{temporary}" ({columns}) SELECT {columns} FROM "{target.__table_name__}"',
                  f'DROP TABLE "{target.__table_name__}"',
                  f'ALTER TABLE "{temporary}" RENAME TO "{target.__table_name__}"']
    return document, digest, mapping, statements, target.__table_name__


def execute_sqlite_column_edit(raw, models, alias, proposal, *, approved, title):
    """Apply a reviewed edit atomically after an independent verified backup."""
    document, digest, mapping, statements, table = proposal

    if not hmac.compare_digest(approved, digest):
        raise PermissionError("Exact model-edit review approval required")

    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("BEGIN IMMEDIATE")

    try:
        connection = Connection(raw, "sqlite")
        current = sqlite_column_edit_proposal(connection, models, alias,
                                              raw.execute("PRAGMA database_list").fetchall()[0][2])

        if current is None or current[1] != digest:
            raise RuntimeError("Column-edit review is stale")

        before = _catalog(raw)
        _require_journal(raw)
        operation_id = "edit_" + digest[:40]
        before_sha = _catalog_digest(before)
        _validate_intent(operation_id, digest, before_sha, before_sha)
        number = prepare_successor_mapping(raw, alias, operation_id, digest, mapping, title)
        raw.execute(
            f'INSERT INTO "{_TABLE}" VALUES (?,?,?,?,?)',
            (operation_id, digest, before_sha, before_sha, "prepared"),
        )

        for statement in statements:
            raw.execute(statement)

        after = _catalog(raw)
        after_sha = _catalog_digest(after)
        raw.execute(f'UPDATE "{_TABLE}" SET after_sha256=? WHERE migration_id=?',
                    (after_sha, operation_id))

        with sqlite3.connect(":memory:") as expected:
            for model in models:
                expected.execute(create_table_sql(model, "sqlite"))

            _verify_result_catalog(raw, expected, {model.__table_name__ for model in models},
                                   native=True)

        if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Column edit violated foreign-key integrity")

        if raw.execute(f'UPDATE "{_TABLE}" SET state=? WHERE migration_id=? AND state=?',
                       ("applied", operation_id, "prepared")).rowcount != 1:
            raise RuntimeError("Column-edit intent transition failed")

        complete_successor(raw, number, operation_id)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise

    return operation_id
