"""Read-only eligibility check for the isolated Alpha 3 MySQL test schema.

Never creates schemas, history tables, or application tables. This helper is
not a migration approval or a substitute for a backup or live DDL test.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


APPROVED_SCHEMA = "aurora_alpha3_migration_test"


@dataclass(frozen=True)
class MySQLLiveTestPreflight:
    schema: str
    server_version: str
    existing_tables: tuple[str, ...]
    create_history_present: bool
    add_column_history_present: bool
    status: str = "READ_ONLY_INSPECTION"


def inspect_disposable_mysql_schema(database_url: str, *, schema: str = APPROVED_SCHEMA) -> MySQLLiveTestPreflight:
    """Inspect only the expressly approved schema; no DDL or DML.

    Requires a URL whose selected database is the dedicated disposable schema.
    An unconfigured schema is refused rather than silently created.
    """
    if schema != APPROVED_SCHEMA:
        raise PermissionError("Only the approved Alpha 3 disposable schema may be inspected")
    url = make_url(database_url)
    if url.get_backend_name() != "mysql" or url.get_driver_name() != "pymysql":
        raise ValueError("Expected a mysql+pymysql connection URL")
    if url.database != APPROVED_SCHEMA:
        raise PermissionError("Database URL must select the approved disposable schema")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            selected = connection.execute(text("SELECT DATABASE()")).scalar_one()
            if selected != APPROVED_SCHEMA:
                raise RuntimeError("Connected schema differs from the approved schema")
            version = str(connection.execute(text("SELECT VERSION()")).scalar_one())
            tables = tuple(sorted(inspect(connection).get_table_names()))
            return MySQLLiveTestPreflight(
                schema=selected,
                server_version=version,
                existing_tables=tables,
                create_history_present="aurora_mysql_migration_history" in tables,
                add_column_history_present="aurora_mysql_add_column_history" in tables,
            )
    finally:
        engine.dispose()
