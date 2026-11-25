"""Canonical AuroraSQL database API for Aurora MVC."""

from .configuration import DatabaseConfiguration, configure_databases
from .connections import Connection, connect
from .async_work import run_database_work
from .models import Column, Model, Query, column
from .schema import create_table, create_table_sql, create_tables
from .snapshots import schema_snapshot, compare_snapshots
from .migration_review import (
    migration_plan,
    validate_migration_plan,
    migration_plan_json,
    snapshot_fingerprint,
)
from .migration_safety import MigrationPreflight, preflight_migration
from .migration_review import AddColumnReview, review_add_column
from .migration_execution import (
    AdditiveEntry,
    initialize_additive_history,
    list_additive_history,
    execute_add_column_recorded,
)
from .schema_inspection import ColumnInfo, TableInfo, inspect_table
from .schema_planning import SchemaChange, SchemaPlan, plan_schema
from .migration_history import CreateApproval, prepare_create, execute_create
from .migration_history import (
    HistoryEntry,
    initialize_history,
    list_history,
    execute_create_recorded,
    unresolved_history,
    HistoryFinding,
    review_history,
    RecoveryAssessment,
    assess_recovery,
    RecoverySchemaFinding,
    review_recovery_schema,
    RecoveryPreflight,
    preflight_recovery,
    MigrationStatus,
    migration_status,
)

__all__ = [
    "Connection",
    "connect",
    "DatabaseConfiguration",
    "configure_databases",
    "run_database_work",
    "Column",
    "Model",
    "Query",
    "column",
    "create_table",
    "create_table_sql",
    "create_tables",
    "schema_snapshot",
    "compare_snapshots",
    "migration_plan",
    "validate_migration_plan",
    "migration_plan_json",
    "snapshot_fingerprint",
    "MigrationPreflight",
    "preflight_migration",
    "AddColumnReview",
    "review_add_column",
    "AdditiveEntry",
    "initialize_additive_history",
    "list_additive_history",
    "execute_add_column_recorded",
    "ColumnInfo",
    "TableInfo",
    "inspect_table",
    "SchemaChange",
    "SchemaPlan",
    "plan_schema",
    "CreateApproval",
    "prepare_create",
    "execute_create",
    "HistoryEntry",
    "initialize_history",
    "list_history",
    "execute_create_recorded",
    "unresolved_history",
    "HistoryFinding",
    "review_history",
    "RecoveryAssessment",
    "assess_recovery",
    "RecoverySchemaFinding",
    "review_recovery_schema",
    "RecoveryPreflight",
    "preflight_recovery",
    "MigrationStatus",
    "migration_status",
]
