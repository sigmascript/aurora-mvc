"""Review a model-identity-backed SQLite table rename without source DDL."""

import sqlite3

from .cli_explicit_operations import _catalog, _fingerprint, _inspect_sqlite
from .migration_timeline import inspect_native_history
from .model_identity import model_identity_map, table_rename_candidates
from .schema import create_table_sql


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
    catalog, warnings = _inspect_sqlite(connection._raw, operation, alias,
                                        trusted_rename=True)

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

    document = {"format": "aurorasql.operations.v1", "database": alias,
                "operation": operation, "model_identity": current}
    digest = _fingerprint(document, catalog, "sqlite", db_file)
    return document, digest, current
