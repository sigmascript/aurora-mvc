"""Connection-scoped coordination for experimental recorded CREATE TABLE.

This protects participating AuroraSQL sessions only. It does not coordinate
external schema tools or make MySQL DDL transactional.
"""

from contextlib import contextmanager
from hashlib import sha256


_LOCK_NAME = "aurorasql:migration:history:v1"
# Stable signed 63-bit key, independent of Python's randomized hash().
_PG_LOCK_KEY = int.from_bytes(sha256(_LOCK_NAME.encode()).digest()[:8], "big") & ((1 << 63) - 1)


@contextmanager
def migration_lock(connection):
    """Hold a database-scoped lock across history preflight, DDL and journaling.

    SQLite uses BEGIN IMMEDIATE. MySQL GET_LOCK and PostgreSQL advisory locks
    are session-scoped and must be released on the same connection. No retries.
    """
    connection._ensure_open()
    if getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Migration coordination cannot be nested")
    if connection._transaction_active:
        raise RuntimeError("Migration coordination requires no active transaction")
    if connection.dialect == "sqlite" and connection._raw.in_transaction:
        raise RuntimeError("SQLite connection already has an active transaction")

    connection._migration_lock_active = True
    acquired = False
    error = None
    try:
        if connection.dialect == "sqlite":
            connection._raw.execute("BEGIN IMMEDIATE")
            acquired = True
        elif connection.dialect == "mysql":
            row = connection.fetch_first("SELECT GET_LOCK(:name, 0) AS acquired", {"name": _LOCK_NAME})
            if row is None or row["acquired"] != 1:
                raise RuntimeError("MySQL migration lock unavailable")
            acquired = True
        elif connection.dialect == "postgresql":
            row = connection.fetch_first(
                "SELECT pg_try_advisory_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if row is None or row["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            acquired = True
        else:
            raise ValueError("Unsupported migration lock backend")
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
    finally:
        try:
            if acquired:
                if connection.dialect == "sqlite":
                    # Commit even on a DDL error: preserve 'uncertain' journal
                    # evidence rather than rolling it back along with the DDL.
                    connection._raw.commit()
                elif connection.dialect == "mysql":
                    row = connection.fetch_first("SELECT RELEASE_LOCK(:name) AS released", {"name": _LOCK_NAME})
                    if row is None or row["released"] != 1:
                        raise RuntimeError("MySQL migration lock release could not be confirmed")
                else:
                    row = connection.fetch_first(
                        "SELECT pg_advisory_unlock(:key) AS released", {"key": _PG_LOCK_KEY}
                    )
                    if row is None or row["released"] is not True:
                        raise RuntimeError("PostgreSQL migration lock release could not be confirmed")
        except BaseException:
            # Preserve the primary migration exception when possible.
            if error is None:
                raise
        finally:
            connection._migration_lock_active = False
