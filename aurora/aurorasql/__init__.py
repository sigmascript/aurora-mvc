"""Experimental AuroraSQL package, independent of the released database layer."""

from .core import Connection, connect
from .async_work import run_database_work
from .models import Column, Model, Query, column
from .schema import create_table, create_table_sql, create_tables
from .snapshots import schema_snapshot, compare_snapshots
from .migration_format import migration_plan, validate_migration_plan, migration_plan_json, snapshot_fingerprint
from .inspection import ColumnInfo, TableInfo, inspect_table
from .planning import SchemaChange, SchemaPlan, plan_schema
from .execution import CreateApproval, prepare_create, execute_create
from .history import HistoryEntry, initialize_history, list_history, execute_create_recorded, unresolved_history, HistoryFinding, review_history, RecoveryAssessment, assess_recovery, RecoverySchemaFinding, review_recovery_schema, RecoveryPreflight, preflight_recovery, MigrationStatus, migration_status

__all__ = [
    "Connection", "connect", "run_database_work", "Column", "Model", "Query", "column",
    "create_table", "create_table_sql", "create_tables", "schema_snapshot", "compare_snapshots",
    "migration_plan", "validate_migration_plan", "migration_plan_json", "snapshot_fingerprint",
    "ColumnInfo", "TableInfo", "inspect_table", "SchemaChange", "SchemaPlan", "plan_schema",
    "CreateApproval", "prepare_create", "execute_create",
    "HistoryEntry", "initialize_history", "list_history", "execute_create_recorded", "unresolved_history", "HistoryFinding", "review_history", "RecoveryAssessment", "assess_recovery", "RecoverySchemaFinding", "review_recovery_schema", "RecoveryPreflight", "preflight_recovery", "MigrationStatus", "migration_status",
]
