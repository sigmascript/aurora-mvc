"""Lock-scoped, read-only MySQL preflight revalidation.

This does not authorize or execute DDL. Advisory locks coordinate only clients
that voluntarily use the same lock name; they are not schema locks.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.exc import SQLAlchemyError

from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_preflight import (
    MySQLSchemaSnapshot, validate_preflight_freshness,
)


def revalidate_mysql_preflight_locked(config, *, reviewed, candidate):
    """Reinspect selected table names under a connection-scoped advisory lock.

    A successful return is informational only, not an execution approval.
    The snapshot covers table names, NOT column definitions or constraints.
    """
    if not isinstance(reviewed, MySQLSchemaSnapshot):
        raise ValueError('Trusted reviewed snapshot required')
    if not isinstance(candidate, MySQLOperationContract):
        raise ValueError('Structured MySQL candidate required')
    if candidate.database_alias != reviewed.database_alias or candidate.schema_name != reviewed.schema_name:
        raise ValueError('Candidate does not match reviewed database')

    url = config.resolve(reviewed.database_alias)
    if url.get_backend_name() != 'mysql' or url.get_driver_name() != 'pymysql':
        raise ValueError('MySQL revalidation requires mysql+pymysql')

    # MySQL lock names have a 64-character limit. Do not expose credentials.
    digest = hashlib.sha256((reviewed.schema_name + '\0' + reviewed.database_alias).encode()).hexdigest()[:47]
    lock_name = 'aurora:migration:' + digest
    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as connection:
            actual_schema = connection.execute(text('SELECT DATABASE()')).scalar_one()
            if actual_schema != reviewed.schema_name:
                raise RuntimeError('Selected MySQL schema differs from reviewed schema')
            acquired = connection.execute(
                text('SELECT GET_LOCK(:name, 0)'), {'name': lock_name}
            ).scalar_one()
            if acquired != 1:
                raise RuntimeError('Unable to acquire MySQL migration advisory lock')
            try:
                # Recheck the selected schema on the SAME connection after locking.
                current_schema = connection.execute(text('SELECT DATABASE()')).scalar_one()
                if current_schema != reviewed.schema_name:
                    raise RuntimeError('Selected MySQL schema changed during preflight')
                existing = tuple(sorted(sa_inspect(connection).get_table_names()))
                current = MySQLSchemaSnapshot(
                    database_alias=reviewed.database_alias,
                    schema_name=current_schema,
                    selected_tables=reviewed.selected_tables,
                    existing_tables=existing,
                )
                validate_preflight_freshness(
                    reviewed=reviewed, current=current, candidate=candidate,
                    advisory_lock_held=True,
                )
                return current
            finally:
                released = connection.execute(
                    text('SELECT RELEASE_LOCK(:name)'), {'name': lock_name}
                ).scalar_one()
                if released != 1:
                    raise RuntimeError('MySQL advisory lock release could not be confirmed')
    except (SQLAlchemyError, ImportError, OSError):
        raise RuntimeError('MySQL locked preflight failed; check connectivity and permissions') from None
    finally:
        if engine is not None:
            engine.dispose()
