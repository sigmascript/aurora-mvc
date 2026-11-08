"""Guarded PostgreSQL initial migration with transactional journal evidence.

All application DDL and journal writes share one transaction. Backend-specific
live acceptance is still required before claiming production readiness.
"""

from hashlib import sha256
from uuid import uuid4

from .coordination import _PG_LOCK_KEY
from .execution import CreateApproval, execute_create
from .history import initialize_history
from .inspection import inspect_table
from .schema import _quote, create_table_sql
from .server_initial_verification import verify_server_initial_schema
from .server_migration_timeline import complete_server_initial, prepare_server_initial


def execute_postgres_init(connection, models, *, approval, expected_approval, alias="default",
                          expected_identity=None, parsed=None, title="Initial migration"):
    """Create an approved empty PostgreSQL schema atomically or raise.

    The caller must independently calculate expected_approval from the exact
    database identity, alias, and ordered DDL. This primitive does not grant
    approval, open a connection, or select a database.
    """

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL initialization requires a PostgreSQL connection")

    if not models or not isinstance(models, (tuple, list)):
        raise ValueError("At least one validated model is required")

    if not isinstance(expected_approval, str) or len(expected_approval) != 64:
        raise ValueError("Expected PostgreSQL approval fingerprint is required")

    if approval != expected_approval:
        raise PermissionError("Exact PostgreSQL schema approval is required")

    from .init_validation import validate_init_models
    ordered = validate_init_models(models, dialect="postgresql")

    if [model.__table_name__ for model in ordered] != [model.__table_name__ for model in models]:
        raise ValueError("Models must be supplied in validated dependency order")

    connection._ensure_open()

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL initialization cannot nest transactions")

    # Transaction-level advisory lock is automatically released on commit or
    # rollback. Unlike a session lock, it cannot leak into a reused connection.
    operation_ids = []

    with connection.transaction():
        locked = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if locked is None or locked["acquired"] is not True:
            raise RuntimeError("PostgreSQL migration lock unavailable")

        if expected_identity is not None:
            from .cli_init import _postgres_database_identity, _postgres_approval_digest

            if (parsed is None or _postgres_database_identity(connection, parsed) != expected_identity
                    or _postgres_approval_digest(ordered, alias, expected_identity) != expected_approval):
                raise RuntimeError("PostgreSQL database or proposal changed after approval")

        from .cli_init import _preflight, _unsafe_warnings
        report = _preflight(connection, ordered, alias)

        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("PostgreSQL schema is not empty; initialization refused")

        if inspect_table(connection, "aurorasql_schema_history").exists:
            raise RuntimeError("Existing migration journal requires manual review")

        initialize_history(connection, confirm=True)
        prepare_server_initial(connection, ordered, alias, expected_approval, title=title)
        journal = _quote("aurorasql_schema_history", connection.dialect)

        for model in ordered:
            sql = create_table_sql(model, "postgresql")
            token = CreateApproval(model.__table_name__, "postgresql",
                                   sha256(sql.encode("utf-8")).hexdigest())

            if inspect_table(connection, model.__table_name__).exists:
                raise RuntimeError("Table appeared during PostgreSQL initialization")

            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :name, :dialect, :fingerprint, :status)",
                {"id": operation_id, "name": model.__table_name__, "dialect": "postgresql",
                 "fingerprint": token.fingerprint, "status": "started"},
            )
            execute_create(connection, model, token, confirm=True)
            connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                {"status": "applied", "id": operation_id},
            )
            operation_ids.append(operation_id)

        schema_sha256 = verify_server_initial_schema(connection, ordered)
        complete_server_initial(connection, alias, schema_sha256)
    # A failed commit is uncertain: Connection.transaction raises, never returns
    # success or retries. PostgreSQL rollback removes transactional journal/DDL.
    return tuple(operation_ids)
