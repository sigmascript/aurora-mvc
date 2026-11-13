"""Apply one reviewed forward migration to a verified server history.

PostgreSQL commits schema and history together. MySQL cannot undo DDL; any
uncertain result is left intact for explicit reconciliation, never retried.
"""

import hmac
import os
from pathlib import Path
from datetime import datetime, timezone

from .coordination import _PG_LOCK_KEY, migration_lock
from .server_forward_history import read_history
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .server_model_review import review_server_model_changes
from .server_column_widening import review_server_column_widening


def execute_server_forward_migration(connection, models, root, alias, *,
                                     identity, parsed, review, approval, title,
                                     backup_path, config_path, login_path=None):
    """Verify predecessor and exact review under lock before one DDL."""
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .cli_mysql_backup import backup_mysql_database
    from .cli_postgres_backup import backup_postgres_database

    dialect = connection.dialect
    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Server forward migration requires MySQL or PostgreSQL")
    if os.environ.get("AURORA_ENV", "development").lower() == "production":
        raise PermissionError("Server migration execution is disabled in production")
    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Cannot nest server migration execution")
    if not identity or parsed is None or not isinstance(title, str) or not title.strip():
        raise ValueError("Verified database identity and migration title are required")
    if (len(review.operations) != 1 or len(review.statements) != 1
            or review.operations[0]["kind"] not in ("create_table", "add_column", "widen_column")
            or not hmac.compare_digest(str(approval), review.review_sha256)):
        raise PermissionError("One exact approved safe server operation is required")

    # Backups are optional for five-column forward migrations. If explicitly
    # requested, the archive must be verified before any schema change.
    archive = None
    if dialect == "mysql":
        identity_fn = _mysql_database_identity
        if backup_path is not None:
            archive = backup_mysql_database(config_path, alias, backup_path, login_path)
    else:
        identity_fn = _postgres_database_identity
        if backup_path is not None:
            archive = backup_postgres_database(config_path, alias, backup_path)

    if backup_path is not None and (not isinstance(archive, Path) or not archive.is_file()):
        raise RuntimeError("Requested server backup could not be verified")

    def apply_locked():
        if identity_fn(connection, parsed) != identity:
            raise RuntimeError("Server database identity changed after review")

        rows, _ = read_history(connection, root, alias)
        if review.operations[0]["kind"] == "widen_column":
            fresh = review_server_column_widening(connection, models, alias, identity)
        else:
            fresh = review_server_model_changes(connection, models, alias, identity)

        if (fresh.operations != review.operations or fresh.statements != review.statements
                or not hmac.compare_digest(fresh.review_sha256, review.review_sha256)):
            raise RuntimeError("Server schema proposal changed after review")

        # MySQL DDL can commit even when execution or later evidence writing
        # fails. Once execution starts, preserve evidence and require manual
        # reconciliation rather than suggesting a safe automatic retry.
        try:
            connection.execute_write(fresh.statements[0])
            verify_server_declared_schema(connection, models, history_tables={"migrations"})
            snapshot = _write_snapshot(connection, root, alias, models, title)
            record = {
                "id": len(rows) + 1,
                "title": title,
                "snapshot": snapshot.name,
                "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "warnings": None,
            }
            connection.execute_write(
                "INSERT INTO migrations (id,title,snapshot,date,warnings) "
                "VALUES (:id,:title,:snapshot,:date,:warnings)",
                record,
            )
            verified_rows, _ = read_history(connection, root, alias)
            if (len(verified_rows) != record["id"]
                    or any(verified_rows[-1][key] != value
                           for key, value in record.items())):
                raise RuntimeError("Server migration history completion was not verified")
        except Exception as exc:
            if dialect == "mysql":
                raise RuntimeError(
                    "MySQL migration outcome uncertain after DDL execution began; "
                    "inspect physical schema, migration history, and snapshots "
                    "before any retry"
                ) from exc
            raise

        return snapshot

    if dialect == "postgresql":
        with connection.transaction():
            locked = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired",
                {"key": _PG_LOCK_KEY},
            )
            if not locked or locked["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            path = apply_locked()
    else:
        with migration_lock(connection):
            path = apply_locked()

    # Retain durable file evidence on errors and uncertain commits.
    return path, archive
