"""Two-phase, explicitly approved MySQL nullable-column migration CLI.

Never creates schemas or history tables. DDL is never retried automatically.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from aurora.database.cli_inspection import load_cli_database_config
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal
from aurora.database.mysql_add_column_preflight import inspect_mysql_nullable_add_column
from aurora.database.mysql_add_column_history import (
    prepare_mysql_add_column_history_review,
    mysql_add_column_history_exists,
)
from aurora.database.mysql_add_column_executor import execute_mysql_nullable_add_column


def run_mysql_add_column(*, config_path: str, alias: str, schema: str,
                         table: str, model: str, column: str, apply: bool = False,
                         approved_review: str | None = None,
                         approved_sql: str | None = None,
                         confirmation: str | None = None) -> dict:
    """Review a single nullable column, or apply an exact approved review.

    The review identity includes the pre-existing column names and SQL digest.
    An apply command must be independently invoked with both digests.
    """
    if apply:
        if os.environ.get('AURORA_ENV', 'development').lower() == 'production':
            raise PermissionError('MySQL migration CLI refuses production environment')
        if os.environ.get('AURORA_MYSQL_MIGRATIONS') != '1':
            raise PermissionError('Set AURORA_MYSQL_MIGRATIONS=1 to enable explicit execution')
        if confirmation != 'APPLY':
            raise PermissionError('Explicit --confirm APPLY is required')
        if not approved_review or not approved_sql:
            raise PermissionError('Both reviewed SHA-256 digests are required')
    elif approved_review is not None or approved_sql is not None or confirmation is not None:
        raise ValueError('Approval arguments require --apply')

    proposal = MySQLAddColumnProposal(alias, schema, table, model, column)
    config, _ = load_cli_database_config(config_path)
    url = config.resolve(alias)
    if url.get_backend_name() != 'mysql' or url.get_driver_name() != 'pymysql':
        raise ValueError('This command requires an explicit mysql+pymysql database alias')
    if url.database != schema:
        raise ValueError('Configured database does not match the selected schema')
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            selected = connection.execute(text('SELECT DATABASE()')).scalar_one()
            if selected != schema:
                raise RuntimeError('Connected MySQL schema differs from reviewed schema')
            snapshot = inspect_mysql_nullable_add_column(connection, proposal=proposal)
            history = prepare_mysql_add_column_history_review(
                proposal, existing_columns=snapshot.existing_columns,
            )
            if not apply:
                return {
                    'status': 'REVIEW_ONLY', 'database_alias': alias,
                    'schema': schema, 'table': table, 'column': column,
                    'sql': snapshot.sql, 'sql_sha256': snapshot.sql_sha256,
                    'review_sha256': history.review_sha256,
                    'history_initialized': mysql_add_column_history_exists(connection),
                    'executable': False,
                }
            if not mysql_add_column_history_exists(connection):
                raise FileNotFoundError('ADD COLUMN history must be initialized separately')
            if history.review_sha256 != approved_review or snapshot.sql_sha256 != approved_sql:
                raise PermissionError('Reviewed schema or SQL digest has changed; review again')
            result = execute_mysql_nullable_add_column(
                connection, proposal=proposal, reviewed=snapshot,
                approved_sql_sha256=approved_sql, confirm=True,
                history_review=history, approved_history_sha256=approved_review,
            )
            return {'status': result.status, 'table': result.table_name,
                    'column': result.column_name, 'detail': result.detail}
    finally:
        engine.dispose()
