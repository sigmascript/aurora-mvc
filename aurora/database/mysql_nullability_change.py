"""Internal guarded MySQL nullability ALTER with durable uncertainty."""

from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

from .coordination import migration_lock
from .schema import _quote
from .server_initial_verification import verify_server_declared_schema
from .server_migration_timeline import (complete_server_successor,
                                        mark_server_successor_uncertain,
                                        prepare_server_nullability_successor)


def execute_mysql_nullability_change(connection, models, *, alias, identity, parsed,
                                     config_path, backup_path, login_path, review,
                                     approval, name, title="Untitled"):
    """Back up, review under lock, apply one flag change and verify its catalog."""
    from .cli_init import _mysql_database_identity
    from .cli_mysql_backup import backup_mysql_database

    if connection.dialect != "mysql":
        raise ValueError("MySQL nullability change requires MySQL")

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL nullability change cannot nest transactions or locks")

    if not alias or not identity or parsed is None or not backup_path:
        raise ValueError("Verified MySQL database identity and backup are required")

    archive = backup_mysql_database(config_path, alias, backup_path, login_path)

    if not isinstance(archive, Path) or not archive.is_file():
        raise RuntimeError("MySQL nullability backup has not been verified")

    operation_id = None
    prepared = False

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database changed after nullability review")

        try:
            # Preparation repeats the bounded NULL probe under the migration lock.
            number = prepare_server_nullability_successor(
                connection, models, alias, identity, review, approval, name, title)
            prepared = True
            operation = review.operations[0]
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :status)",
                {"id": operation_id, "table": operation["table"], "dialect": "mysql",
                 "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                 "status": "started"},
            )
            # Implicit MySQL DDL commit means even an exception can leave a
            # successful physical ALTER behind. Never retry without review.
            connection.execute_write(review.statements[0])
            changed = connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
                {"status": "applied", "id": operation_id, "before": "started"},
            )

            if changed != 1:
                raise RuntimeError("MySQL nullability journal was not completed")

            digest = verify_server_declared_schema(connection, models)
            complete_server_successor(connection, number, name, alias, digest)
        except BaseException:
            if operation_id is not None:
                try:
                    connection.execute_write(
                        f"UPDATE {_quote('aurorasql_schema_history', 'mysql')} "
                        "SET status=:status WHERE operation_id=:id "
                        "AND status IN ('started', 'applied')",
                        {"status": "uncertain", "id": operation_id},
                    )
                except BaseException:
                    pass

            if prepared:
                try:
                    mark_server_successor_uncertain(connection, 2, name, alias)
                except BaseException:
                    # A prepared record must still prevent unreviewed replay.
                    pass

            raise

    return operation_id, archive
