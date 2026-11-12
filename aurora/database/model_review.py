"""Construct review-only additive proposals from verified SQLite model drift."""

from copy import deepcopy
from hashlib import sha256
import json

from .migration_timeline import inspect_native_history
from .model_identity import model_identity_map, table_rename_candidates
from .planning import plan_schema
from .review_artifacts import create_review
from .snapshots import schema_snapshot


class NoModelChanges(ValueError):
    """The verified SQLite schema already matches the selected models."""


def create_sqlite_model_review(connection, models, alias="default"):
    """Build an additive review; refuse uncertain history or structural drift.

    No SQL is executed and the returned review cannot authorize migration.
    The physical table DDL is retained as a baseline witness for the existing
    executor's exact catalog comparison at application time.
    """
    if connection.dialect != "sqlite" or not models:
        raise ValueError("Model-driven review requires SQLite models")

    models = tuple(models)
    current = model_identity_map(models)
    timeline, _, _ = inspect_native_history(connection, alias)

    if not timeline or not timeline[-1]["physical_history_consistent"]:
        raise RuntimeError("Verified native migration history is required")

    previous = timeline[-1]["model_tables"]

    if set(previous) - set(current) or table_rename_candidates(previous, models):
        raise ValueError("Removed or renamed models need separate explicit review")

    after = schema_snapshot(*models, dialect="sqlite")
    before = deepcopy(after)
    prior_tables = []

    for table in before["tables"]:
        target = next(model for model in models if model.__table_name__ == table["name"])
        plan = plan_schema(connection, target)
        kinds = {change.kind for change in plan.changes}

        if not plan.exists:
            if kinds != {"create_table"} or table["name"] in previous.values():
                raise RuntimeError("Missing historical table requires reconciliation")

            continue

        if kinds - {"missing_column"}:
            raise RuntimeError("Unsupported existing-column drift needs explicit review")

        missing = {change.column for change in plan.changes}

        if any(((not target.__columns__[name].nullable and
                 target.__columns__[name].default is None) or
                target.__columns__[name].unique or
                target.__columns__[name].primary_key or
                target.__columns__[name].references is not None)
               for name in missing):
            raise ValueError("Only ordinary nullable or defaulted required columns are supported")

        row = connection._raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table["name"],),
        ).fetchone()

        if row is None or not isinstance(row[0], str) or not row[0].startswith("CREATE TABLE "):
            raise RuntimeError("A canonical existing table definition is required")

        table["columns"] = [column for column in table["columns"]
                            if column["name"] not in missing]
        table["create_sql_preview"] = row[0]
        prior_tables.append(table)

    before["tables"] = prior_tables
    identity = connection._raw.execute("PRAGMA database_list").fetchall()
    main = [row[2] for row in identity if row[1] == "main"]

    if len(main) != 1 or not main[0]:
        raise RuntimeError("Model review requires a named SQLite database")

    body = json.dumps({"before": before, "after": after, "alias": alias,
                       "database": main[0]},
                      sort_keys=True, separators=(",", ":"))
    migration_id = "Model" + sha256(body.encode("utf-8")).hexdigest()[:24]
    review = create_review(before, after, migration_id=migration_id)

    if not review["plan"]["operations"]:
        raise NoModelChanges("There are no supported model changes to migrate")

    return review
