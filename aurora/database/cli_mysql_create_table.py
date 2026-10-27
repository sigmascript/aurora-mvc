"""Two-phase MySQL CREATE TABLE CLI with exact review and explicit execution."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect, text

from aurora.database.cli_inspection import load_cli_database_config
from aurora.database.mysql_create_table_executor import (
    execute_mysql_create_table, prepare_mysql_create_table,
)
from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_preflight import MySQLSchemaSnapshot
from aurora.database.mysql_migration_history import history_table_exists


def run_mysql_create_table(*, config_path: str, alias: str, schema: str,
                           table: str, model: str, apply: bool = False,
                           approved_review: str | None = None,
                           approved_sql: str | None = None,
                           confirmation: str | None = None) -> dict:
    """Review an explicit model table or execute exactly the reviewed operation.

    Does not create the schema or history storage, and never retries DDL.
    """
    if apply:
        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            raise PermissionError("MySQL migration CLI refuses production environment")
        if os.environ.get("AURORA_MYSQL_MIGRATIONS") != "1":
            raise PermissionError("Set AURORA_MYSQL_MIGRATIONS=1 to enable execution")
        if confirmation != "APPLY":
            raise PermissionError("Explicit --confirm APPLY is required")
        if not approved_review or not approved_sql:
            raise PermissionError("Both reviewed SHA-256 digests are required")
    elif any(value is not None for value in (approved_review, approved_sql, confirmation)):
        raise ValueError("Approval arguments require --apply")

    operation = MySQLOperationContract(alias, schema, table, model)
    sql, sql_digest = prepare_mysql_create_table(operation)
    config, _ = load_cli_database_config(config_path)
    url = config.resolve(alias)
    if url.get_backend_name() != "mysql" or url.get_driver_name() != "pymysql":
        raise ValueError("This command requires mysql+pymysql")
    if url.database != schema:
        raise ValueError("Configured database does not match selected schema")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            selected = connection.execute(text("SELECT DATABASE()")).scalar_one()
            if selected != schema:
                raise RuntimeError("Connected MySQL schema differs from reviewed schema")
            existing = tuple(sorted(inspect(connection).get_table_names()))
            if table in existing:
                raise ValueError("Target table already exists; no CREATE TABLE candidate")
            snapshot = MySQLSchemaSnapshot(alias, schema, (table,), existing)
            request = MySQLApprovalRequest(operation, sql_digest, snapshot.fingerprint())
            if not apply:
                return {
                    "status": "REVIEW_ONLY", "database_alias": alias,
                    "schema": schema, "table": table, "sql": sql,
                    "sql_sha256": sql_digest, "review_sha256": request.review_sha256(),
                    "history_initialized": history_table_exists(connection),
                    "executable": False,
                }
            if not history_table_exists(connection):
                raise FileNotFoundError("CREATE TABLE history must be initialized separately")
            if approved_review != request.review_sha256() or approved_sql != sql_digest:
                raise PermissionError("Reviewed schema or SQL digest has changed; review again")
        # The executor opens a fresh connection and revalidates under its own lock.
        result = execute_mysql_create_table(
            config, request=request, reviewed=snapshot,
            approved_review_sha256=approved_review,
            approved_sql_sha256=approved_sql, confirm=True,
        )
        return {"status": result.status, "table": result.table_name, "detail": result.detail}
    finally:
        engine.dispose()
