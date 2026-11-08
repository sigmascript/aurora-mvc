"""Transactional PostgreSQL execution for one verified new-table successor.

This internal primitive is not a public migration route. Never enable it for
ordinary CLI execution before backup/recovery and live database acceptance.
"""

from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

from .coordination import _PG_LOCK_KEY
from .execution import CreateApproval, execute_create
from .model_identity import model_identity_map
from .schema import _quote, create_table_sql
from .server_initial_verification import verify_server_declared_schema
from .server_migration_timeline import (complete_server_successor,
                                        inspect_server_timeline,
                                        prepare_server_successor)


def execute_postgres_new_table_successor(connection, models, *, alias, identity,
                                         parsed, config_path, backup_path, review,
                                         approval, name, title="Untitled"):
    """Back up, then apply one exact approved table and its journals atomically.

    PostgreSQL rollback protects the application DDL and journal on ordinary
    errors. A commit failure is never retried; the operator must reconcile.
    """
    from .cli_init import _postgres_database_identity
    from .cli_postgres_backup import backup_postgres_database

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL successor requires a PostgreSQL connection")

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Server migrations require separate production approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL successor cannot nest transactions")

    if not isinstance(alias, str) or not alias or not identity or parsed is None:
        raise ValueError("A verified database alias and identity are required")

    # Backup is attempted before any transaction or schema write. Its client
    # validates the configured database and user and publishes a new archive.
    archived = backup_postgres_database(config_path, alias, backup_path)

    if not isinstance(archived, Path) or not archived.is_file():
        raise RuntimeError("PostgreSQL successor backup has not been verified")

    with connection.transaction():
        locked = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if locked is None or locked["acquired"] is not True:
            raise RuntimeError("PostgreSQL successor migration lock unavailable")

        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database identity changed after review")

        sequence = prepare_server_successor(connection, models, alias, identity,
                                            review, approval, name, title)
        timeline = inspect_server_timeline(connection, alias)

        if (timeline is None or len(timeline) != sequence
                or timeline[-2].status != "applied" or timeline[-1].status != "prepared"):
            raise RuntimeError("PostgreSQL predecessor history changed")

        previous = timeline[-2]
        mapping = model_identity_map(models)
        new_references = set(mapping) - set(previous.model_tables)

        if len(new_references) != 1:
            raise RuntimeError("Successor requires exactly one new model")

        new_reference = next(iter(new_references))
        model = next(item for item in models
                     if f"{item.__module__}:{item.__qualname__}" == new_reference)
        ddl = create_table_sql(model, "postgresql")
        token = CreateApproval(model.__table_name__, "postgresql",
                               sha256(ddl.encode("utf-8")).hexdigest())
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :state)",
            {"id": operation_id, "table": model.__table_name__, "dialect": "postgresql",
             "fingerprint": token.fingerprint, "state": "started"},
        )
        execute_create(connection, model, token, confirm=True)
        affected = connection.execute_write(
            f"UPDATE {journal} SET status=:state WHERE operation_id=:id AND status=:before",
            {"state": "applied", "id": operation_id, "before": "started"},
        )

        if affected != 1:
            raise RuntimeError("PostgreSQL successor journal completion was not confirmed")

        if sequence == 2:
            original_models = [item for item in models
                               if f"{item.__module__}:{item.__qualname__}" in previous.model_tables]
            initial_digest = verify_server_declared_schema(
                connection, models, evidence_models=original_models)

            if initial_digest != previous.schema_sha256:
                raise RuntimeError("PostgreSQL Initial schema changed during successor execution")

        successor_digest = verify_server_declared_schema(connection, models)
        complete_server_successor(connection, sequence, name, alias, successor_digest)

        if sequence > 2:
            from .server_history_chain import verify_server_applied_chain

            if not verify_server_applied_chain(connection, models, alias, identity):
                raise RuntimeError("PostgreSQL completed history chain failed independent verification")

    return operation_id, archived
