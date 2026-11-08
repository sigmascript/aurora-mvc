"""Transactional PostgreSQL execution for one historically verified column add.

Internal only until live backend and backup/recovery acceptance is complete.
"""

from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

from .coordination import _PG_LOCK_KEY
from .schema import _quote
from .server_initial_verification import verify_server_declared_schema
from .server_migration_timeline import (complete_server_successor,
                                        prepare_server_column_successor)


def execute_postgres_column_addition(connection, models, *, alias, identity, parsed,
                                     config_path, backup_path, review, approval,
                                     name, title="Untitled"):
    """Back up, revalidate, apply one reviewed ADD COLUMN, then verify.

    The caller cannot turn an arbitrary statement into execution permission:
    preparation reconstructs its reviewed SQL under the transaction lock.
    """
    from .cli_init import _postgres_database_identity
    from .cli_postgres_backup import backup_postgres_database

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL column migration requires PostgreSQL")

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL column migration cannot nest transactions")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified PostgreSQL database identity is required")

    archive = backup_postgres_database(config_path, alias, backup_path)

    if not isinstance(archive, Path) or not archive.is_file():
        raise RuntimeError("PostgreSQL column backup has not been verified")

    with connection.transaction():
        lock = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if lock is None or lock["acquired"] is not True:
            raise RuntimeError("PostgreSQL column migration lock unavailable")

        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database changed after column review")

        number = prepare_server_column_successor(
            connection, models, alias, identity, review, approval, name, title)
        operation = review.operations[0]
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :status)",
            {"id": operation_id, "table": operation["table"], "dialect": "postgresql",
             "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
             "status": "started"},
        )
        connection.execute_write(review.statements[0])
        changed = connection.execute_write(
            f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
            {"status": "applied", "id": operation_id, "before": "started"},
        )

        if changed != 1:
            raise RuntimeError("PostgreSQL column operation journal was not completed")

        digest = verify_server_declared_schema(connection, models)
        complete_server_successor(connection, number, name, alias, digest)

    return operation_id, archive
