"""Deterministic migration review documents; never execute database changes."""
import hashlib
import json
import re

from .snapshots import compare_snapshots

_FORMAT = "aurorasql.migration.v1"
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_ORDER = {"added_table": 0, "added_column": 1, "changed_column": 2,
          "removed_column": 3, "removed_table": 4}
_RISK = {"additive", "requires_data_strategy", "potential_data_loss"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def snapshot_fingerprint(snapshot):
    """Hash validated declarations, excluding advisory SQL previews."""
    compare_snapshots(snapshot, snapshot)
    declaration = {
        "format": snapshot["format"], "dialect": snapshot["dialect"],
        "tables": [{"name": table["name"],
                    "columns": sorted(table["columns"], key=lambda c: c["name"])}
                   for table in sorted(snapshot["tables"], key=lambda t: t["name"])],
    }
    return hashlib.sha256(_canonical(declaration).encode("utf-8")).hexdigest()


def validate_migration_plan(plan):
    """Fail closed on malformed plans; validation does not approve execution."""
    keys = {"format", "id", "dialect", "before_fingerprint", "after_fingerprint",
            "operations", "execution_allowed"}
    if not isinstance(plan, dict) or set(plan) != keys or plan["format"] != _FORMAT:
        raise ValueError("Invalid migration plan format")
    if not isinstance(plan["id"], str) or not _IDENTIFIER.fullmatch(plan["id"]):
        raise ValueError("Invalid migration identifier")
    if plan["dialect"] not in ("sqlite", "mysql", "postgresql"):
        raise ValueError("Invalid migration dialect")
    for key in ("before_fingerprint", "after_fingerprint"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]):
            raise ValueError("Invalid schema fingerprint")
    if plan["execution_allowed"] is not False or not isinstance(plan["operations"], list):
        raise ValueError("Migration plans are review-only")
    for operation in plan["operations"]:
        if not isinstance(operation, dict) or set(operation) != {
            "kind", "table", "column", "risk", "requires_review"
        }:
            raise ValueError("Invalid migration operation")
        kind = operation["kind"]
        if kind not in _ORDER or operation["risk"] not in _RISK:
            raise ValueError("Unsupported migration operation")
        if not isinstance(operation["table"], str) or not _IDENTIFIER.fullmatch(operation["table"]):
            raise ValueError("Invalid table identifier")
        column = operation["column"]
        if kind in ("added_table", "removed_table"):
            if column is not None:
                raise ValueError("Table operation cannot name a column")
        elif not isinstance(column, str) or not _IDENTIFIER.fullmatch(column):
            raise ValueError("Invalid column identifier")
        if operation["requires_review"] is not True:
            raise ValueError("Every operation requires review")
    return plan


def migration_plan(before, after, *, migration_id):
    """Compare snapshots into an ordered, read-only migration proposal."""
    if not isinstance(migration_id, str) or not _IDENTIFIER.fullmatch(migration_id):
        raise ValueError("Invalid migration identifier")
    changes = compare_snapshots(before, after)
    operations = sorted(({
        "kind": item["kind"], "table": item["table"], "column": item["column"],
        "risk": item["risk"], "requires_review": True,
    } for item in changes), key=lambda op: (_ORDER[op["kind"]], op["table"], op["column"] or ""))
    return validate_migration_plan({
        "format": _FORMAT, "id": migration_id, "dialect": before["dialect"],
        "before_fingerprint": snapshot_fingerprint(before),
        "after_fingerprint": snapshot_fingerprint(after),
        "operations": operations, "execution_allowed": False,
    })


def migration_plan_json(plan):
    """Serialize a validated proposal deterministically."""
    validate_migration_plan(plan)
    return json.dumps(plan, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
