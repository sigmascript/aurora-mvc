"""Read-only, structured MySQL CREATE TABLE preflight.

The preflight does not generate DDL, grant approvals, or execute migrations.
Only explicitly supplied, trusted Aurora model tables can become candidates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from aurora.database.mysql_migration_contract import MySQLOperationContract


@dataclass(frozen=True)
class MySQLSchemaSnapshot:
    database_alias: str
    schema_name: str
    selected_tables: tuple[str, ...]
    existing_tables: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.selected_tables, tuple) or not self.selected_tables:
            raise ValueError("Explicit selected model tables are required")
        if not isinstance(self.existing_tables, tuple):
            raise ValueError("Existing table names must be a tuple")
        for field in ("selected_tables", "existing_tables"):
            values = getattr(self, field)
            if any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
                raise ValueError(f"Invalid {field}")
        if not isinstance(self.database_alias, str) or not self.database_alias:
            raise ValueError("Database alias is required")
        if not isinstance(self.schema_name, str) or not self.schema_name:
            raise ValueError("Schema name is required")

    def fingerprint(self) -> str:
        payload = dict(database_alias=self.database_alias, schema_name=self.schema_name,
                       selected_tables=sorted(self.selected_tables),
                       existing_tables=sorted(self.existing_tables))
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def derive_create_table_candidates(snapshot: MySQLSchemaSnapshot, model_tables: dict[str, str]) -> tuple[MySQLOperationContract, ...]:
    """Derive candidate identities only; never parse drift messages.

    model_tables maps explicit MODULE:CLASS references to their validated SQLAlchemy
    table names. The caller must establish this mapping from trusted model classes.
    """
    if not isinstance(snapshot, MySQLSchemaSnapshot):
        raise ValueError("Trusted schema snapshot required")
    if not isinstance(model_tables, dict) or not model_tables:
        raise ValueError("Explicit model-to-table mapping required")
    if set(model_tables.values()) != set(snapshot.selected_tables) or len(set(model_tables.values())) != len(model_tables):
        raise ValueError("Model selection does not match inspected tables")
    present = set(snapshot.existing_tables)
    candidates = []
    for reference, table in sorted(model_tables.items()):
        if table not in present:
            candidates.append(MySQLOperationContract(
                database_alias=snapshot.database_alias,
                schema_name=snapshot.schema_name,
                table_name=table,
                model_reference=reference,
            ))
    return tuple(candidates)


def validate_preflight_freshness(*, reviewed: MySQLSchemaSnapshot,
                                 current: MySQLSchemaSnapshot,
                                 candidate: MySQLOperationContract,
                                 advisory_lock_held: bool) -> None:
    """Reject stale or unlocked preflights; success is NOT execution approval."""
    if not isinstance(reviewed, MySQLSchemaSnapshot) or not isinstance(current, MySQLSchemaSnapshot):
        raise ValueError("Both snapshots must be trusted structured snapshots")
    if not isinstance(candidate, MySQLOperationContract):
        raise ValueError("Structured MySQL candidate required")
    if advisory_lock_held is not True:
        raise RuntimeError("MySQL advisory lock must be held during revalidation")
    if reviewed != current:
        raise RuntimeError("MySQL schema changed since review; replan required")
    if candidate.database_alias != current.database_alias or candidate.schema_name != current.schema_name:
        raise RuntimeError("MySQL candidate targets a different database")
    if candidate.table_name not in current.selected_tables or candidate.table_name in current.existing_tables:
        raise RuntimeError("MySQL candidate is not a missing selected table")


def evaluate_mysql_outcome(*, ddl_attempted: bool, table_exists_after: bool | None,
                           history_recorded: bool | None) -> str:
    """Conservative recovery classification; unknown is not rolled back."""
    if not isinstance(ddl_attempted, bool):
        raise ValueError("ddl_attempted must be bool")
    if table_exists_after is not None and not isinstance(table_exists_after, bool):
        raise ValueError("table_exists_after must be bool or None")
    if history_recorded is not None and not isinstance(history_recorded, bool):
        raise ValueError("history_recorded must be bool or None")
    if not ddl_attempted:
        if table_exists_after or history_recorded:
            raise ValueError("Inconsistent pre-execution outcome")
        return "NOT_STARTED"
    if table_exists_after is None:
        return "OUTCOME_UNKNOWN"
    if table_exists_after is False:
        return "REQUIRES_MANUAL_INSPECTION" if history_recorded else "DDL_NOT_PRESENT"
    if history_recorded is True:
        return "SCHEMA_AND_HISTORY_PRESENT"  # Not proof of full schema correctness.
    return "DDL_PRESENT_HISTORY_UNCONFIRMED"
