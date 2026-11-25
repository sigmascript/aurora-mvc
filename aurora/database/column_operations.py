"""Guarded column alterations for SQLite, MySQL, and PostgreSQL.

MySQL uncertain-DDL recovery and PostgreSQL transactional checks remain
backend-specific; the shared module does not loosen migration safeguards.
"""

import os
from datetime import datetime, timezone
import hmac
import json
import sys


from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from aurora.core.project_policy import project_is_production

from .migration_coordination import _PG_LOCK_KEY, migration_lock
from .schema import _quote
from .schema import create_table_sql, validate_init_models
from .schema_inspection import verify_server_declared_schema
from .schema_inspection import inspect_table
from .migration_timeline import (
    complete_server_successor,
    mark_server_successor_uncertain,
    prepare_server_column_successor,
    prepare_server_default_successor,
    prepare_server_nullability_successor,
    prepare_server_widening_successor,
)
from .migration_history import history_table, read_history
from .initialization import _write_snapshot
from .constraints import _verify_simple_constraints
from .foreign_keys import _foreign_key_dependency
from .snapshots import schema_snapshot
from .migration_review import _add_column_clause


def execute_mysql_column_addition(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    login_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review and apply one historically proven reviewed nullable column."""
    from .cli_backup import backup_mysql_database
    from .connections import _mysql_database_identity

    if connection.dialect != "mysql":
        raise ValueError("MySQL column migration requires MySQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL column migration cannot nest transactions or locks")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified MySQL database identity is required")

    archive = None
    if backup_path is not None:
        archive = backup_mysql_database(config_path, alias, backup_path, login_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("MySQL column backup has not been verified")

    operation_id = None
    prepared = False

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database changed after column review")

        try:
            number = prepare_server_column_successor(
                connection, models, alias, identity, review, approval, name, title
            )
            prepared = True
            operation = review.operations[0]
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :status)",
                {
                    "id": operation_id,
                    "table": operation["table"],
                    "dialect": "mysql",
                    "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                    "status": "started",
                },
            )
            # MySQL ALTER TABLE implicitly commits; a raised exception cannot
            # establish whether the statement took effect.
            connection.execute_write(review.statements[0])
            changed = connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
                {"status": "applied", "id": operation_id, "before": "started"},
            )

            if changed != 1:
                raise RuntimeError("MySQL column operation journal was not completed")

            digest = verify_server_declared_schema(connection, models)
            complete_server_successor(connection, number, name, alias, digest)

            if number > 2:
                from .migration_history import verify_server_applied_chain

                if not verify_server_applied_chain(connection, models, alias, identity):
                    raise RuntimeError("MySQL column history chain is unverified")
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
                    mark_server_successor_uncertain(connection, number, name, alias)
                except BaseException:
                    # Prepared history alone must block unreviewed replay.
                    pass

            raise

    return operation_id, archive


def execute_postgres_column_addition(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review, revalidate, apply one reviewed ADD COLUMN, then verify.

    The caller cannot turn an arbitrary statement into execution permission:
    preparation reconstructs its reviewed SQL under the transaction lock.
    """
    from .cli_backup import backup_postgres_database
    from .connections import _postgres_database_identity

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL column migration requires PostgreSQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL column migration cannot nest transactions")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified PostgreSQL database identity is required")

    archive = None
    if backup_path is not None:
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
            connection, models, alias, identity, review, approval, name, title
        )
        operation = review.operations[0]
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :status)",
            {
                "id": operation_id,
                "table": operation["table"],
                "dialect": "postgresql",
                "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                "status": "started",
            },
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

        if number > 2:
            from .migration_history import verify_server_applied_chain

            if not verify_server_applied_chain(connection, models, alias, identity):
                raise RuntimeError("PostgreSQL column history chain is unverified")

    return operation_id, archive


def execute_mysql_default_addition(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    login_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review, lock, re-review a literal default, ALTER, then verify evidence."""
    from .cli_backup import backup_mysql_database
    from .connections import _mysql_database_identity

    if connection.dialect != "mysql":
        raise ValueError("MySQL SQL default migration requires MySQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL default migration cannot nest transactions or locks")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified MySQL database identity is required")

    archive = None
    if backup_path is not None:
        archive = backup_mysql_database(config_path, alias, backup_path, login_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("MySQL SQL default backup has not been verified")

    operation_id = None
    prepared = False

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database changed after default review")

        try:
            number = prepare_server_default_successor(
                connection, models, alias, identity, review, approval, name, title
            )
            prepared = True
            operation = review.operations[0]
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :status)",
                {
                    "id": operation_id,
                    "table": operation["table"],
                    "dialect": "mysql",
                    "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                    "status": "started",
                },
            )
            # MySQL ALTER can commit even when the client reports failure.
            connection.execute_write(review.statements[0])
            changed = connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
                {"status": "applied", "id": operation_id, "before": "started"},
            )

            if changed != 1:
                raise RuntimeError("MySQL SQL default journal was not completed")

            digest = verify_server_declared_schema(connection, models)
            complete_server_successor(connection, number, name, alias, digest)

            if number > 2:
                from .migration_history import verify_server_applied_chain

                if not verify_server_applied_chain(connection, models, alias, identity):
                    raise RuntimeError("MySQL default history chain is unverified")
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
                    mark_server_successor_uncertain(connection, number, name, alias)
                except BaseException:
                    # Prepared history alone must prevent unreviewed replay.
                    pass

            raise

    return operation_id, archive


def execute_postgres_default_addition(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review and apply one approved literal DEFAULT inside the transaction."""
    from .cli_backup import backup_postgres_database
    from .connections import _postgres_database_identity

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL SQL default migration requires PostgreSQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL default migration cannot nest transactions")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified PostgreSQL database identity is required")

    archive = None
    if backup_path is not None:
        archive = backup_postgres_database(config_path, alias, backup_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("PostgreSQL SQL default backup has not been verified")

    with connection.transaction():
        lock = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if lock is None or lock["acquired"] is not True:
            raise RuntimeError("PostgreSQL default migration lock unavailable")

        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database changed after default review")

        number = prepare_server_default_successor(
            connection, models, alias, identity, review, approval, name, title
        )
        operation = review.operations[0]
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :status)",
            {
                "id": operation_id,
                "table": operation["table"],
                "dialect": "postgresql",
                "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                "status": "started",
            },
        )
        connection.execute_write(review.statements[0])
        changed = connection.execute_write(
            f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
            {"status": "applied", "id": operation_id, "before": "started"},
        )

        if changed != 1:
            raise RuntimeError("PostgreSQL default journal was not completed")

        digest = verify_server_declared_schema(connection, models)
        complete_server_successor(connection, number, name, alias, digest)

        if number > 2:
            from .migration_history import verify_server_applied_chain

            if not verify_server_applied_chain(connection, models, alias, identity):
                raise RuntimeError("PostgreSQL default history chain is unverified")

    return operation_id, archive


def execute_mysql_column_widening(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    login_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Apply one exactly reviewed widening after backup and locked historical proof."""
    from .cli_backup import backup_mysql_database
    from .connections import _mysql_database_identity

    if connection.dialect != "mysql":
        raise ValueError("MySQL widening requires MySQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL widening cannot nest transactions or migration locks")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified MySQL database identity is required")

    archive = None
    if backup_path is not None:
        archive = backup_mysql_database(config_path, alias, backup_path, login_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("MySQL widening backup has not been verified")

    operation_id = None
    prepared = False

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database changed after widening review")

        try:
            number = prepare_server_widening_successor(
                connection, models, alias, identity, review, approval, name, title
            )
            prepared = True
            operation = review.operations[0]
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :status)",
                {
                    "id": operation_id,
                    "table": operation["table"],
                    "dialect": "mysql",
                    "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                    "status": "started",
                },
            )
            # MySQL ALTER can commit before an error reaches Python.
            connection.execute_write(review.statements[0])
            changed = connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
                {"status": "applied", "id": operation_id, "before": "started"},
            )

            if changed != 1:
                raise RuntimeError("MySQL widening journal was not completed")

            digest = verify_server_declared_schema(connection, models)
            complete_server_successor(connection, number, name, alias, digest)

            if number > 2:
                from .migration_history import verify_server_applied_chain

                if not verify_server_applied_chain(connection, models, alias, identity):
                    raise RuntimeError("MySQL widening history chain is unverified")
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
                    mark_server_successor_uncertain(connection, number, name, alias)
                except BaseException:
                    # The remaining prepared record must still prevent blind replay.
                    pass

            raise

    return operation_id, archive


def execute_postgres_column_widening(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Require verified backup and lock before a single reviewed ALTER."""
    from .cli_backup import backup_postgres_database
    from .connections import _postgres_database_identity

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL widening requires PostgreSQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL widening cannot nest transactions")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified PostgreSQL database identity is required")

    archive = None
    if backup_path is not None:
        archive = backup_postgres_database(config_path, alias, backup_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("PostgreSQL widening backup has not been verified")

    with connection.transaction():
        lock = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if lock is None or lock["acquired"] is not True:
            raise RuntimeError("PostgreSQL widening migration lock unavailable")

        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database changed after widening review")

        number = prepare_server_widening_successor(
            connection, models, alias, identity, review, approval, name, title
        )
        operation = review.operations[0]
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :status)",
            {
                "id": operation_id,
                "table": operation["table"],
                "dialect": "postgresql",
                "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                "status": "started",
            },
        )
        connection.execute_write(review.statements[0])
        changed = connection.execute_write(
            f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
            {"status": "applied", "id": operation_id, "before": "started"},
        )

        if changed != 1:
            raise RuntimeError("PostgreSQL widening journal was not completed")

        digest = verify_server_declared_schema(connection, models)
        complete_server_successor(connection, number, name, alias, digest)

        if number > 2:
            from .migration_history import verify_server_applied_chain

            if not verify_server_applied_chain(connection, models, alias, identity):
                raise RuntimeError("PostgreSQL widening history chain is unverified")

    return operation_id, archive


def execute_mysql_nullability_change(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    login_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review, review under lock, apply one flag change and verify its catalog."""
    from .cli_backup import backup_mysql_database
    from .connections import _mysql_database_identity

    if connection.dialect != "mysql":
        raise ValueError("MySQL nullability change requires MySQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL nullability change cannot nest transactions or locks")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified MySQL database identity is required")

    archive = None
    if backup_path is not None:
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
                connection, models, alias, identity, review, approval, name, title
            )
            prepared = True
            operation = review.operations[0]
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :status)",
                {
                    "id": operation_id,
                    "table": operation["table"],
                    "dialect": "mysql",
                    "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                    "status": "started",
                },
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

            if number > 2:
                from .migration_history import verify_server_applied_chain

                if not verify_server_applied_chain(connection, models, alias, identity):
                    raise RuntimeError("MySQL nullability history chain is unverified")
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
                    mark_server_successor_uncertain(connection, number, name, alias)
                except BaseException:
                    # A prepared record must still prevent unreviewed replay.
                    pass

            raise

    return operation_id, archive


def execute_postgres_nullability_change(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review and apply one independently verified ALTER inside a transaction."""
    from .cli_backup import backup_postgres_database
    from .connections import _postgres_database_identity

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL nullability change requires PostgreSQL")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Production migration requires separate approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL nullability change cannot nest transactions")

    if not alias or not identity or parsed is None:
        raise ValueError("Verified PostgreSQL database identity is required")

    archive = None
    if backup_path is not None:
        archive = backup_postgres_database(config_path, alias, backup_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("PostgreSQL nullability backup has not been verified")

    with connection.transaction():
        lock = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if lock is None or lock["acquired"] is not True:
            raise RuntimeError("PostgreSQL nullability migration lock unavailable")

        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database changed after nullability review")

        number = prepare_server_nullability_successor(
            connection, models, alias, identity, review, approval, name, title
        )
        operation = review.operations[0]
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :status)",
            {
                "id": operation_id,
                "table": operation["table"],
                "dialect": "postgresql",
                "fingerprint": sha256(review.statements[0].encode("utf-8")).hexdigest(),
                "status": "started",
            },
        )
        connection.execute_write(review.statements[0])
        changed = connection.execute_write(
            f"UPDATE {journal} SET status=:status WHERE operation_id=:id AND status=:before",
            {"status": "applied", "id": operation_id, "before": "started"},
        )

        if changed != 1:
            raise RuntimeError("PostgreSQL nullability journal was not completed")

        digest = verify_server_declared_schema(connection, models)
        complete_server_successor(connection, number, name, alias, digest)

        if number > 2:
            from .migration_history import verify_server_applied_chain

            if not verify_server_applied_chain(connection, models, alias, identity):
                raise RuntimeError("PostgreSQL nullability history chain is unverified")

    return operation_id, archive


# Native guarded column removal and coherent mixed-column transitions.
# This block retains the backend-specific review and acknowledgement rules.


def _strip_preview(tables):
    clone = json.loads(json.dumps(tables))
    for table in clone:
        table.pop("create_sql_preview", None)
    return clone


def single_column_removal(prior, desired):
    """Recognize only one missing existing field and no other declaration edits."""
    if not isinstance(prior, list) or not isinstance(desired, list):
        return False
    if any(not isinstance(table, dict) for table in (*prior, *desired)):
        return False
    old, new = _strip_preview(prior), _strip_preview(desired)
    if len(old) != len(new) or len(old) == 0:
        return False
    losses = 0
    for before, after in zip(old, new):
        if before.get("name") != after.get("name") or set(before) != set(after):
            return False
        if any(before[k] != after[k] for k in before if k != "columns"):
            return False
        a, b = before.get("columns"), after.get("columns")
        if not isinstance(a, list) or not isinstance(b, list):
            return False
        if a == b:
            continue
        if len(a) != len(b) + 1:
            return False
        removed = [c for c in a if c not in b]
        if len(removed) != 1 or [c for c in a if c != removed[0]] != b:
            return False
        if not isinstance(removed[0], dict) or not removed[0].get("name"):
            return False
        losses += 1
    return losses == 1


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def review_pg_column_removal(connection, models, root, alias, identity):
    """Read-only model/history/catalog/data risk review; return exact digest."""
    if connection.dialect != "postgresql" or not identity:
        raise ValueError("Existing-column removal requires a verified PostgreSQL identity")
    models = validate_init_models(models, dialect="postgresql", allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*models, dialect="postgresql", allow_v2=True)["tables"]
    if not single_column_removal(previous["models"], desired):
        raise ValueError("Only one ordinary existing-column removal is supported")
    old_tables, new_tables = _strip_preview(previous["models"]), _strip_preview(desired)
    changes = [
        (a, next(c for c in a["columns"] if c not in b["columns"]))
        for a, b in zip(old_tables, new_tables)
        if a["columns"] != b["columns"]
    ]
    table, field = changes[0]
    name, column = table["name"], field["name"]
    if (
        field.get("primary_key")
        or field.get("unique")
        or field.get("references")
        or field.get("server_default") is not None
    ):
        raise ValueError("Keyed, referenced or server-default column removal is unsupported")
    observed = inspect_table(connection, name)
    if (
        not observed.exists
        or not observed.constraints_verified
        or not observed.uniqueness_verified
        or observed.foreign_key_actions is None
        or set(observed.indexes) != {name + "_pkey"}
        or observed.unique_columns
        or observed.foreign_keys
        or column not in {item.name for item in observed.columns}
    ):
        raise ValueError("Target column has unverified physical schema or dependencies")
    if _foreign_key_dependency(connection, name):
        raise ValueError("Target table has foreign-key dependencies")
    _verify_simple_constraints(connection, name, existing_unique=False)
    # Measure actual stored values; a backup is recommended but never required.
    has_data = (
        connection.fetch_first(
            f"SELECT 1 AS present FROM {_quoted(name)} WHERE {_quoted(column)} IS NOT NULL LIMIT 1"
        )
        is not None
    )
    operation = {
        "kind": "drop_column",
        "table": name,
        "column": column,
        "has_meaningful_data": has_data,
    }
    digest = sha256(
        json.dumps(
            {
                "alias": alias,
                "identity": identity,
                "count": len(history),
                "physical": previous["physical_schema"],
                "operation": operation,
                "desired": new_tables,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return operation, digest, len(history)


def run_pg_column_removal(args, config, connection, models, root, alias, identity, parsed):
    """Explicit review + target phrase; transactionally drop and write history."""
    from .connections import _postgres_database_identity
    from .migration_review import migration_title

    if project_is_production(root):
        raise PermissionError("PostgreSQL destructive migrations are disabled in production")
    if any(getattr(args, option, None) for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("Column removal requires ordinary interactive review")
    if getattr(args, "confirm", None) is not None or not sys.stdin.isatty():
        raise PermissionError("Column removal requires independent interactive confirmations")
    operation, digest, count = review_pg_column_removal(connection, models, root, alias, identity)
    target = f"{alias}.{operation['table']}.{operation['column']}"
    print(f"AuroraSQL destructive migration — {alias} (postgresql)")
    print(f"  drop_column: {operation['table']}.{operation['column']}")
    print(
        "  Stored non-NULL values: "
        + ("PRESENT; permanent value loss" if operation["has_meaningful_data"] else "NONE")
    )
    if operation["has_meaningful_data"]:
        print("  WARNING: This operation permanently deletes existing column values.")
        print("  RECOMMENDATION: Run aurora db backup separately before proceeding.")
        print("  Aurora will not automatically create or restore a backup.")
    print("  Reviewed SHA-256: " + digest)
    if input("Apply this reviewed column removal? [y/N]: ").strip().lower() != "y":
        print("Column removal cancelled; no database changes made.")
        return 0
    if operation["has_meaningful_data"]:
        phrase = f"DELETE {target}"
        if input("Type " + phrase + " to acknowledge permanent value loss: ").strip() != phrase:
            print("Column removal cancelled; no database changes made.")
            return 0
    title = migration_title(getattr(args, "title", None), "Untitled")
    path = None
    with connection.transaction():
        # Block external writes to the target while revalidating and applying DDL.
        connection.execute_write(f"LOCK TABLE {_quoted(operation['table'])} IN SHARE MODE")
        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("Database identity changed during destructive review")
        refreshed, witness, actual_count = review_pg_column_removal(
            connection, models, root, alias, identity
        )
        if (
            refreshed != operation
            or actual_count != count
            or not hmac.compare_digest(witness, digest)
        ):
            raise RuntimeError("Destructive proposal changed after approval")
        # The migration creates no backup and never invokes the backup catalog.
        acquired = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )
        if not acquired or acquired["acquired"] is not True:
            raise RuntimeError("PostgreSQL migration lock unavailable")
        refreshed, witness, actual_count = review_pg_column_removal(
            connection, models, root, alias, identity
        )
        if (
            refreshed != operation
            or actual_count != count
            or not hmac.compare_digest(witness, digest)
        ):
            raise RuntimeError("Destructive proposal changed after approval")
        try:
            count_before = connection.fetch_first(
                f"SELECT COUNT(*) AS total FROM {_quoted(operation['table'])}"
            )
            if count_before is None:
                raise RuntimeError("Cannot verify records before column removal")
            connection.execute_write(
                f"ALTER TABLE {_quoted(operation['table'])} "
                f"DROP COLUMN {_quoted(operation['column'])}"
            )
            count_after = connection.fetch_first(
                f"SELECT COUNT(*) AS total FROM {_quoted(operation['table'])}"
            )
            if count_after is None or count_after["total"] != count_before["total"]:
                raise RuntimeError("Column removal changed the application row count")
            verify_server_declared_schema(
                connection, models, history_tables={history_table(connection)}, allow_extended=True
            )
            path = _write_snapshot(connection, root, alias, models, title)
            warnings = (
                [
                    f"Confirmed permanent data loss: removed populated column {target}; backup not created automatically"
                ]
                if operation["has_meaningful_data"]
                else None
            )
            record = {
                "id": count + 1,
                "title": title,
                "snapshot": path.name,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "warnings": json.dumps(warnings) if warnings else None,
            }
            connection.execute_write(
                "INSERT INTO " + history_table(connection) + " (id,title,snapshot,date,warnings) "
                "VALUES (:id,:title,:snapshot,:date,:warnings)",
                record,
            )
            completed, _ = read_history(connection, root, alias)
            if len(completed) != record["id"] or any(
                completed[-1][key] != value for key, value in record.items()
            ):
                raise RuntimeError("Column removal history completion failed")
        except Exception:
            # Retain any incomplete snapshot as evidence; no automatic repair.
            raise
    print(f"Applied Migration{count + 1} to {alias}; snapshot: {path.name}")
    return 0


# MySQL forward column removal retains its nontransactional DDL safeguards.


def _quote_removal(value):
    return "`" + value.replace("`", "``") + "`"


def review_mysql_column_removal(connection, models, root, alias, identity):
    """Read-only review: exact one-column model delta and actual physical risk."""
    if connection.dialect != "mysql" or not identity:
        raise ValueError("Existing-column removal requires a verified MySQL identity")
    ordered = validate_init_models(models, dialect="mysql", allow_extended=True)
    history, previous = read_history(connection, root, alias)
    declared = schema_snapshot(*ordered, dialect="mysql", allow_v2=True)["tables"]
    if not single_column_removal(previous["models"], declared):
        raise ValueError("Only one ordinary existing-column removal is supported")
    old_tables, new_tables = _strip_preview(previous["models"]), _strip_preview(declared)
    changes = [
        (old, next(col for col in old["columns"] if col not in new["columns"]))
        for old, new in zip(old_tables, new_tables)
        if old["columns"] != new["columns"]
    ]
    table, field = changes[0]
    name, column = table["name"], field["name"]
    if (
        field.get("primary_key")
        or field.get("unique")
        or field.get("references")
        or field.get("server_default") is not None
    ):
        raise ValueError("Keyed, referenced or defaulted column removal requires separate review")
    observed = inspect_table(connection, name)
    if (
        not observed.exists
        or not observed.constraints_verified
        or not observed.uniqueness_verified
        or observed.foreign_key_actions is None
        or set(observed.indexes) != {"PRIMARY"}
        or observed.unique_columns
        or observed.foreign_keys
        or column not in {item.name for item in observed.columns}
    ):
        raise ValueError("Target column has unverified physical schema or dependencies")
    if _foreign_key_dependency(connection, name):
        raise ValueError("Target table has foreign-key dependencies")
    _verify_simple_constraints(connection, name, existing_unique=False)
    has_data = (
        connection.fetch_first(
            f"SELECT 1 AS present FROM {_quote_removal(name)} "
            f"WHERE {_quote_removal(column)} IS NOT NULL LIMIT 1"
        )
        is not None
    )
    operation = {
        "kind": "drop_column",
        "table": name,
        "column": column,
        "has_meaningful_data": has_data,
    }
    digest = sha256(
        json.dumps(
            {
                "alias": alias,
                "identity": identity,
                "count": len(history),
                "physical": previous["physical_schema"],
                "operation": operation,
                "desired": new_tables,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return operation, digest, len(history)


def run_mysql_column_removal(args, config, connection, models, root, alias, identity, parsed):
    """Typed approval, reviewed MySQL DDL, forward evidence; no backup."""
    from .connections import _mysql_database_identity
    from .migration_review import migration_title

    if project_is_production(root):
        raise PermissionError("MySQL destructive migrations are disabled in production")
    if any(getattr(args, option, None) for option in ("apply", "approve", "backup", "name")):
        raise PermissionError("Column removal requires ordinary interactive review")
    if getattr(args, "confirm", None) is not None or not sys.stdin.isatty():
        raise PermissionError("Column removal requires independent interactive confirmations")
    operation, digest, count = review_mysql_column_removal(
        connection, models, root, alias, identity
    )
    target = f"{alias}.{operation['table']}.{operation['column']}"
    print(f"AuroraSQL destructive migration — {alias} (mysql)")
    print(f"  drop_column: {operation['table']}.{operation['column']}")
    print(
        "  Stored non-NULL values: "
        + ("PRESENT; permanent value loss" if operation["has_meaningful_data"] else "NONE")
    )
    if operation["has_meaningful_data"]:
        print("  WARNING: This operation permanently deletes existing column values.")
        print("  RECOMMENDATION: Run aurora db backup separately before proceeding.")
        print("  Aurora will not automatically create or restore a backup.")
    print("  Reviewed SHA-256: " + digest)
    if input("Apply this reviewed column removal? [y/N]: ").strip().lower() != "y":
        print("Column removal cancelled; no database changes made.")
        return 0
    if operation["has_meaningful_data"]:
        phrase = f"DELETE {target}"
        if input("Type " + phrase + " to acknowledge permanent value loss: ").strip() != phrase:
            print("Column removal cancelled; no database changes made.")
            return 0

    title = migration_title(getattr(args, "title", None), "Untitled")
    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database identity changed during destructive review")
        fresh, witness, current_count = review_mysql_column_removal(
            connection, models, root, alias, identity
        )
        if fresh != operation or current_count != count or not hmac.compare_digest(witness, digest):
            raise RuntimeError("Destructive proposal changed after approval; no DDL executed")
        before = connection.fetch_first(
            f"SELECT COUNT(*) AS total FROM {_quote_removal(operation['table'])}"
        )
        if before is None:
            raise RuntimeError("Unable to verify row count before removal")
        # From this point the MySQL DDL may commit. Never assume rollback.
        try:
            connection.execute_write(
                f"ALTER TABLE {_quote_removal(operation['table'])} "
                f"DROP COLUMN {_quote_removal(operation['column'])}"
            )
            after = connection.fetch_first(
                f"SELECT COUNT(*) AS total FROM {_quote_removal(operation['table'])}"
            )
            if after is None or after["total"] != before["total"]:
                raise RuntimeError("Column removal changed the application row count")
            verify_server_declared_schema(
                connection, models, history_tables={history_table(connection)}, allow_extended=True
            )
            path = _write_snapshot(connection, root, alias, models, title)
            warnings = (
                [
                    f"Confirmed permanent data loss: removed populated column {target}; backup not created automatically"
                ]
                if operation["has_meaningful_data"]
                else None
            )
            record = {
                "id": count + 1,
                "title": title,
                "snapshot": path.name,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "warnings": json.dumps(warnings) if warnings else None,
            }
            connection.execute_write(
                "INSERT INTO " + history_table(connection) + " (id,title,snapshot,date,warnings) "
                "VALUES (:id,:title,:snapshot,:date,:warnings)",
                record,
            )
            completed, _ = read_history(connection, root, alias)
            if len(completed) != record["id"] or any(
                completed[-1][key] != value for key, value in record.items()
            ):
                raise RuntimeError("Column removal history completion failed")
        except Exception as exc:
            raise RuntimeError(
                "MySQL column removal outcome uncertain after DDL began; "
                "inspect physical schema and history before retry"
            ) from exc
    print(f"Applied Migration{count + 1} to {alias}; snapshot: {path.name}")
    return 0


# A mixed add+remove is two separate changes, never an inferred rename.


def _identifier(value):
    return isinstance(value, str) and bool(value) and value.isidentifier()


def _mixed_delta(before, after):
    """Return one (table, removed, added), or None; never infer rename."""
    if (
        not isinstance(before, list)
        or not isinstance(after, list)
        or not before
        or len(before) != len(after)
        or any(not isinstance(t, dict) for t in (*before, *after))
    ):
        return None
    previous_tables, desired_tables = _strip_preview(before), _strip_preview(after)
    changes = []
    for prior, desired in zip(previous_tables, desired_tables):
        if (
            set(prior) != set(desired)
            or prior.get("name") != desired.get("name")
            or not _identifier(prior.get("name"))
            or any(prior[k] != desired[k] for k in prior if k != "columns")
        ):
            return None
        previous_columns, desired_columns = prior.get("columns"), desired.get("columns")
        if (
            not isinstance(previous_columns, list)
            or not isinstance(desired_columns, list)
            or any(
                not isinstance(c, dict) or not _identifier(c.get("name"))
                for c in (*previous_columns, *desired_columns)
            )
        ):
            return None
        if previous_columns == desired_columns:
            continue
        previous_by_name = {column["name"]: column for column in previous_columns}
        desired_by_name = {column["name"]: column for column in desired_columns}
        if len(previous_by_name) != len(previous_columns) or len(desired_by_name) != len(
            desired_columns
        ):
            return None
        removed = set(previous_by_name) - set(desired_by_name)
        added = set(desired_by_name) - set(previous_by_name)
        if (
            len(removed) != 1
            or len(added) != 1
            or any(
                previous_by_name[name] != desired_by_name[name]
                for name in set(previous_by_name) & set(desired_by_name)
            )
        ):
            return None
        changes.append(
            (prior["name"], previous_by_name[removed.pop()], desired_by_name[added.pop()])
        )
    return changes[0] if len(changes) == 1 else None


def single_mixed_column_delta(before, after):
    return _mixed_delta(before, after) is not None


def _q(name, dialect):
    mark = "`" if dialect == "mysql" else '"'
    return mark + name.replace(mark, mark * 2) + mark


def review_mixed_columns(connection, models, root, alias, identity):
    dialect = connection.dialect
    if dialect not in ("mysql", "postgresql") or not identity:
        raise ValueError("Mixed column review requires a verified server database identity")
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)["tables"]
    delta = _mixed_delta(previous["models"], desired)
    if delta is None:
        raise ValueError(
            "Only one ordinary removal plus one addition in the same table is supported"
        )
    name, dropped, added = delta
    if (
        dropped.get("primary_key")
        or dropped.get("unique")
        or dropped.get("references")
        or dropped.get("server_default") is not None
    ):
        raise ValueError("Dependent or defaulted removed column requires separate review")
    target_model = next((m for m in ordered if m.__table_name__ == name), None)
    if target_model is None or added["name"] not in target_model.__columns__:
        raise ValueError("Added field is not available in the validated model")
    add_clause = _add_column_clause(target_model, added["name"], dialect)
    observed = inspect_table(connection, name)
    expected = {"PRIMARY"} if dialect == "mysql" else {name + "_pkey"}
    names = {c.name for c in observed.columns}
    if (
        not observed.exists
        or not observed.constraints_verified
        or not observed.uniqueness_verified
        or observed.foreign_key_actions is None
        or set(observed.indexes) != expected
        or observed.unique_columns
        or observed.foreign_keys
        or dropped["name"] not in names
        or added["name"] in names
    ):
        raise ValueError("Mixed column transition needs independently verified ordinary columns")
    if _foreign_key_dependency(connection, name):
        raise ValueError("Mixed column transition has foreign-key dependencies")
    _verify_simple_constraints(connection, name, existing_unique=False)
    meaningful = (
        connection.fetch_first(
            f"SELECT 1 AS present FROM {_q(name, dialect)} "
            f"WHERE {_q(dropped['name'], dialect)} IS NOT NULL LIMIT 1"
        )
        is not None
    )
    operation = {
        "kind": "mixed_columns",
        "table": name,
        "drop_column": dropped["name"],
        "add_column": added["name"],
        "has_meaningful_data": meaningful,
    }
    statement = (
        f"ALTER TABLE {_q(name, dialect)} DROP COLUMN {_q(dropped['name'], dialect)}, {add_clause}"
    )
    digest = sha256(
        json.dumps(
            {
                "alias": alias,
                "identity": identity,
                "count": len(history),
                "physical": previous["physical_schema"],
                "operation": operation,
                "statement": statement,
                "desired": _strip_preview(desired),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return operation, statement, digest, len(history)


def run_mixed_columns(args, connection, models, root, alias, identity, parsed):
    from .connections import _mysql_database_identity, _postgres_database_identity
    from .migration_review import migration_title

    dialect = connection.dialect
    if project_is_production(root):
        raise PermissionError("Destructive server migrations are disabled in production")
    if (
        any(getattr(args, key, None) for key in ("apply", "approve", "backup", "name"))
        or getattr(args, "confirm", None) is not None
        or not sys.stdin.isatty()
    ):
        raise PermissionError("Mixed destructive columns require independent interactive approval")
    op, sql, digest, count = review_mixed_columns(connection, models, root, alias, identity)
    target = f"{alias}.{op['table']}.{op['drop_column']}"
    print(f"AuroraSQL reviewed mixed migration — {alias} ({dialect})")
    print(f"  drop_column: {op['table']}.{op['drop_column']}")
    print(f"  add_column: {op['table']}.{op['add_column']}")
    print("  These are distinct operations; Aurora does not infer a rename.")
    if op["has_meaningful_data"]:
        print("  WARNING: Removing this column permanently deletes its stored values.")
        print("  RECOMMENDATION: Run aurora db backup separately before proceeding.")
        print("  Aurora will not automatically create or restore backups.")
    print("  Reviewed SHA-256: " + digest)
    if input("Apply both reviewed column changes? [y/N]: ").strip().lower() != "y":
        print("Migration cancelled; no database changes made.")
        return 0
    if op["has_meaningful_data"]:
        phrase = f"DELETE {target}"
        if input("Type " + phrase + " to acknowledge permanent value loss: ").strip() != phrase:
            print("Migration cancelled; no database changes made.")
            return 0
    title = migration_title(getattr(args, "title", None), "Untitled")
    ddl_started = [False]
    identify = _mysql_database_identity if dialect == "mysql" else _postgres_database_identity

    def apply_reviewed():
        if identify(connection, parsed) != identity:
            raise RuntimeError("Database identity changed since approval")
        fresh, statement, witness, current_count = review_mixed_columns(
            connection, models, root, alias, identity
        )
        if (
            fresh != op
            or statement != sql
            or current_count != count
            or not hmac.compare_digest(witness, digest)
        ):
            raise RuntimeError("Mixed-column proposal changed after approval; no DDL executed")
        before = connection.fetch_first(f"SELECT COUNT(*) AS total FROM {_q(op['table'], dialect)}")
        if before is None or not isinstance(before["total"], int):
            raise RuntimeError("Cannot verify row count before mixed migration")
        ddl_started[0] = True
        connection.execute_write(sql)
        after = connection.fetch_first(f"SELECT COUNT(*) AS total FROM {_q(op['table'], dialect)}")
        if after is None or after["total"] != before["total"]:
            raise RuntimeError("Mixed migration changed application row count")
        verify_server_declared_schema(
            connection, models, history_tables={history_table(connection)}, allow_extended=True
        )
        path = _write_snapshot(connection, root, alias, models, title)
        warnings = (
            [
                f"Confirmed permanent data loss: removed populated column {target} "
                f"while adding {op['add_column']}; backup was optional"
            ]
            if op["has_meaningful_data"]
            else None
        )
        record = {
            "id": count + 1,
            "title": title,
            "snapshot": path.name,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "warnings": json.dumps(warnings) if warnings else None,
        }
        connection.execute_write(
            "INSERT INTO " + history_table(connection) + " (id,title,snapshot,date,warnings) "
            "VALUES (:id,:title,:snapshot,:date,:warnings)",
            record,
        )
        completed, _ = read_history(connection, root, alias)
        if len(completed) != record["id"] or any(completed[-1][k] != v for k, v in record.items()):
            raise RuntimeError("Mixed migration history completion failed")
        return path

    if dialect == "postgresql":
        with connection.transaction():
            connection.execute_write(f"LOCK TABLE {_q(op['table'], dialect)} IN SHARE MODE")
            lock = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not lock or lock["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            path = apply_reviewed()
    else:
        with migration_lock(connection):
            # MySQL may commit ALTER TABLE before history writing succeeds.
            if identify(connection, parsed) != identity:
                raise RuntimeError("Database identity changed since approval")
            fresh, statement, witness, current_count = review_mixed_columns(
                connection, models, root, alias, identity
            )
            if (
                fresh != op
                or statement != sql
                or current_count != count
                or not hmac.compare_digest(witness, digest)
            ):
                raise RuntimeError("Mixed-column proposal changed after approval; no DDL executed")
            try:
                path = apply_reviewed()
            except Exception as exc:
                if not ddl_started[0]:
                    raise
                raise RuntimeError(
                    "MySQL mixed migration outcome uncertain after DDL began; "
                    "inspect physical schema and history before retry"
                ) from exc
    print(f"Applied Migration{count + 1} to {alias}; snapshot: {path.name}")
    return 0


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_column_removal(raw, root, db_file, models, title, reviewed, *, acknowledged=()):
    """Drop all reviewed columns in one transaction with one history record."""
    from .constraints import matches_models_with_indexes
    from .migration_history import sqlite_new_record as _new_record
    from .migration_review import review_column_removal
    from .schema import _quote

    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        actual = review_column_removal(raw, root, db_file, models)
        if actual != reviewed:
            raise RuntimeError("Column removal review is stale; run db check again")
        dangerous = {(table, column) for table, column, count, _ in reviewed if count}
        if dangerous and not dangerous.issubset(set(acknowledged)):
            raise PermissionError("Populated column removal requires typed authorization")
        for table, column, _, _ in reviewed:
            raw.execute(
                f"ALTER TABLE {_quote(table, 'sqlite')} DROP COLUMN {_quote(column, 'sqlite')}"
            )
        matches_models_with_indexes(raw, models)
        warnings = [
            f"Approved permanent deletion of {table}.{column}: "
            f"{count} existing non-NULL value(s); backup recommended, not automatically created"
            for table, column, count, _ in reviewed
            if count
        ]
        path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path
