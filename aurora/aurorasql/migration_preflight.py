"""Fail-closed, read-only migration preflight for declarative review plans.

This module grants no execution authority and does not issue DDL.
"""

from dataclasses import dataclass

from .inspection import inspect_table
from .migration_format import (
    migration_plan, snapshot_fingerprint, validate_migration_plan,
)
from .snapshots import compare_snapshots


@dataclass(frozen=True)
class MigrationPreflight:
    migration_id: str
    dialect: str
    ready_for_execution: bool
    findings: tuple[str, ...]


def preflight_migration(connection, before, after, plan):
    """Verify plan provenance and observable table/column presence.

    A successful review is not an authorization to execute: live schema type,
    constraints, defaults, drift, concurrent DDL and audit state need separate
    verified execution contracts. No DDL is performed by this function.
    """
    validate_migration_plan(plan)
    if connection.dialect != plan["dialect"]:
        raise ValueError("Connection and migration dialect differ")
    if before.get("dialect") != plan["dialect"] or after.get("dialect") != plan["dialect"]:
        raise ValueError("Snapshot and migration dialect differ")
    if snapshot_fingerprint(before) != plan["before_fingerprint"]:
        raise ValueError("Source snapshot fingerprint mismatch")
    if snapshot_fingerprint(after) != plan["after_fingerprint"]:
        raise ValueError("Target snapshot fingerprint mismatch")
    expected = migration_plan(before, after, migration_id=plan["id"])
    if expected != plan:
        raise ValueError("Migration operations do not match the snapshots")

    old_tables = {item["name"]: item for item in before["tables"]}
    touched = sorted({op["table"] for op in plan["operations"]})
    findings = []
    for name in touched:
        live = inspect_table(connection, name)
        old = old_tables.get(name)
        if old is None:
            if live.exists:
                findings.append(f"{name}: expected absent table, but it exists")
            continue
        if not live.exists:
            findings.append(f"{name}: expected existing table is absent")
            continue
        live_columns = {column.name for column in live.columns}
        declared_columns = {column["name"] for column in old["columns"]}
        if live_columns != declared_columns:
            findings.append(f"{name}: live column names differ from source snapshot")
    for op in plan["operations"]:
        if op["kind"] not in ("added_table", "added_column") or op["risk"] != "additive":
            findings.append(f"{op['table']}: operation {op['kind']} requires a separate data strategy")
        elif op["kind"] == "added_column":
            target = next(t for t in after["tables"] if t["name"] == op["table"])
            field = next(c for c in target["columns"] if c["name"] == op["column"])
            if not field["nullable"] or field["unique"] or field["primary_key"] or field["references"] is not None or field["server_default"] is not None:
                findings.append(f"{op['table']}.{op['column']}: advanced column semantics require separate review")
    findings.append("Read-only preflight: no migration execution is authorized")
    return MigrationPreflight(plan["id"], plan["dialect"], False, tuple(findings))
