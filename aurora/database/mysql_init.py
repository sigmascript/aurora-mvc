"""Guarded, create-only MySQL initialization with durable per-table evidence.

MySQL DDL implicitly commits. Never pretend that a failed batch rolled back.
A partially applied batch is a manual recovery event, not a retry candidate.
"""

from hashlib import sha256
from uuid import uuid4

from .coordination import migration_lock
from .execution import CreateApproval, execute_create
from .history import initialize_history, review_history
from .inspection import inspect_table
from .schema import _quote, create_table_sql


def execute_mysql_init(connection, models, *, approval, expected_approval,
                       alias="default", expected_identity=None, parsed=None):
    """Create a fully approved, empty MySQL schema, preserving partial outcomes."""

    if connection.dialect != "mysql":
        raise ValueError("MySQL initialization requires a MySQL connection")

    if not isinstance(expected_approval, str) or len(expected_approval) != 64:
        raise ValueError("Expected MySQL approval fingerprint is required")

    if approval != expected_approval:
        raise PermissionError("Exact MySQL schema approval is required")

    if expected_identity is None or parsed is None:
        raise ValueError("Verified MySQL database identity is required")

    from .init_validation import validate_init_models
    ordered = validate_init_models(models, dialect="mysql")

    if list(ordered) != list(models):
        raise ValueError("Models must be in validated dependency order")

    connection._ensure_open()

    if connection._transaction_active:
        raise RuntimeError("MySQL DDL cannot execute inside a transaction")

    operation_ids = []

    with migration_lock(connection):
        from .cli_init import (_mysql_approval_digest, _mysql_database_identity,
                               _mysql_read_only_preflight, _unsafe_warnings)
        actual_identity = _mysql_database_identity(connection, parsed)

        if actual_identity != expected_identity or _mysql_approval_digest(
            ordered, alias, actual_identity
        ) != expected_approval:
            raise RuntimeError("MySQL database or proposal changed after approval")

        report = _mysql_read_only_preflight(connection, ordered, alias)

        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("MySQL schema is not empty; initialization refused")

        if inspect_table(connection, "aurorasql_schema_history").exists:
            raise RuntimeError("Existing migration journal requires manual review")

        # MySQL CREATE TABLE auto-commits; this journal must persist independently.
        initialize_history(connection, confirm=True)
        journal = _quote("aurorasql_schema_history", "mysql")

        for model in ordered:
            if review_history(connection):
                raise RuntimeError("MySQL migration history requires manual review")

            if inspect_table(connection, model.__table_name__).exists:
                raise RuntimeError("Table appeared during MySQL initialization")

            sql = create_table_sql(model, "mysql")
            token = CreateApproval(model.__table_name__, "mysql",
                                   sha256(sql.encode("utf-8")).hexdigest())
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :name, :dialect, :fingerprint, :status)",
                {"id": operation_id, "name": model.__table_name__,
                 "dialect": "mysql", "fingerprint": token.fingerprint,
                 "status": "started"},
            )

            try:
                execute_create(connection, model, token, confirm=True)

                if not inspect_table(connection, model.__table_name__).exists:
                    raise RuntimeError("MySQL table creation could not be verified")

                connection.execute_write(
                    f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                    {"status": "applied", "id": operation_id},
                )
            except BaseException:
                try:
                    connection.execute_write(
                        f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                        {"status": "uncertain", "id": operation_id},
                    )
                except BaseException:
                    # 'started' remains durable evidence; never retry automatically.
                    pass

                raise

            operation_ids.append(operation_id)

    return tuple(operation_ids)
