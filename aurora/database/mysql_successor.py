"""Durable MySQL execution for one exact reviewed new-table successor.

This primitive is internal. MySQL DDL implicitly commits: failures retain
prepared or uncertain journal evidence and require manual reconciliation.
"""

from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

from .coordination import migration_lock
from .execution import CreateApproval, execute_create
from .model_identity import model_identity_map
from .schema import _quote, create_table_sql
from .server_initial_verification import verify_server_declared_schema
from .server_migration_timeline import (complete_server_successor,
                                        inspect_server_timeline,
                                        mark_server_successor_uncertain,
                                        prepare_server_successor)


def execute_mysql_new_table_successor(connection, models, *, alias, identity,
                                      parsed, config_path, backup_path, login_path,
                                      review, approval, name, title="Untitled"):
    """Back up and execute one guarded MySQL new-table migration without retry."""
    from .cli_init import _mysql_database_identity
    from .cli_mysql_backup import backup_mysql_database

    if connection.dialect != "mysql":
        raise ValueError("MySQL successor requires a MySQL connection")

    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Server migrations require separate production approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL successor cannot nest locks or transactions")

    if (not isinstance(alias, str) or not alias or not identity or parsed is None
            or not isinstance(backup_path, str) or not backup_path):
        raise ValueError("Reviewed database identity and new backup destination are required")

    archive = backup_mysql_database(config_path, alias, backup_path, login_path)

    if not isinstance(archive, Path) or not archive.is_file():
        raise RuntimeError("MySQL successor backup has not been verified")

    operation_id = None
    prepared = False

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database identity changed after review")

        try:
            sequence = prepare_server_successor(connection, models, alias, identity,
                                                review, approval, name, title)
            prepared = True
            timeline = inspect_server_timeline(connection, alias)

            if (timeline is None or len(timeline) != sequence
                    or timeline[-2].status != "applied" or timeline[-1].status != "prepared"):
                raise RuntimeError("MySQL predecessor history changed")

            previous = timeline[-2]
            mapping = model_identity_map(models)
            added = set(mapping) - set(previous.model_tables)

            if len(added) != 1:
                raise RuntimeError("Successor requires exactly one new model")

            model = next(item for item in models
                         if f"{item.__module__}:{item.__qualname__}" in added)
            ddl = create_table_sql(model, "mysql")
            approval_token = CreateApproval(model.__table_name__, "mysql",
                                            sha256(ddl.encode("utf-8")).hexdigest())
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :state)",
                {"id": operation_id, "table": model.__table_name__, "dialect": "mysql",
                 "fingerprint": approval_token.fingerprint, "state": "started"},
            )
            execute_create(connection, model, approval_token, confirm=True)
            affected = connection.execute_write(
                f"UPDATE {journal} SET status=:state WHERE operation_id=:id AND status=:before",
                {"state": "applied", "id": operation_id, "before": "started"},
            )

            if affected != 1:
                raise RuntimeError("MySQL successor journal completion was not confirmed")

            if sequence == 2:
                original_models = [item for item in models
                                   if f"{item.__module__}:{item.__qualname__}" in previous.model_tables]
                initial_digest = verify_server_declared_schema(
                    connection, models, evidence_models=original_models)

                if initial_digest != previous.schema_sha256:
                    raise RuntimeError("MySQL Initial schema changed during successor execution")

            successor_digest = verify_server_declared_schema(connection, models)
            complete_server_successor(connection, sequence, name, alias, successor_digest)

            if sequence > 2:
                from .server_history_chain import verify_server_applied_chain

                if not verify_server_applied_chain(connection, models, alias, identity):
                    raise RuntimeError("MySQL completed history chain failed independent verification")
        except BaseException:
            if operation_id is not None:
                try:
                    connection.execute_write(
                        f"UPDATE {_quote('aurorasql_schema_history', 'mysql')} "
                        "SET status=:state WHERE operation_id=:id "
                        "AND status IN ('started', 'applied')",
                        {"state": "uncertain", "id": operation_id},
                    )
                except BaseException:
                    # A started row is still evidence of an unknown outcome.
                    pass

            if prepared:
                try:
                    mark_server_successor_uncertain(connection, sequence, name, alias)
                except BaseException:
                    # Durable prepared intent blocks automatic replay.
                    pass

            raise

    return operation_id, archive
