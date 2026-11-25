"""SQLite migration intent journal and explicit uncertain-state lifecycle.

Intent records are separate from the native migration timeline. Neither
read-only inspection nor a recorded intent authorizes DDL execution.
"""

from dataclasses import dataclass
import re
import sqlite3
from uuid import UUID

_INTENT_TABLE = "aurorasql_migration_intents_v2"
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STATES = frozenset({"prepared", "uncertain", "applied"})


@dataclass(frozen=True)
class MigrationIntent:
    migration_id: str
    review_sha256: str
    before_sha256: str
    after_sha256: str
    state: str


def _check_connection(connection):
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("SQLite DB-API connection required")

    if connection.in_transaction:
        raise RuntimeError("Journal operation requires no active transaction")


def _validate_intent(migration_id, review_sha256, before_sha256, after_sha256):
    if not isinstance(migration_id, str) or not _ID.fullmatch(migration_id):
        raise ValueError("Invalid migration ID")

    for value in (review_sha256, before_sha256, after_sha256):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError("Invalid SHA-256 fingerprint")


def _require_journal(connection):
    row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name=?", (_INTENT_TABLE,)
    ).fetchone()

    if row is None or row[0] != "table":
        raise RuntimeError("Migration intent journal is missing")

    columns = connection.execute(f'PRAGMA table_info("{_INTENT_TABLE}")').fetchall()
    expected = ["migration_id", "review_sha256", "before_sha256", "after_sha256", "state"]

    if [item[1] for item in columns] != expected or columns[0][5] != 1:
        raise RuntimeError("Migration intent journal schema mismatch")


def initialize_intent_journal(connection, *, confirm=False):
    """Explicit, create-once setup. Does not modify user application tables."""

    if confirm is not True:
        raise PermissionError("Explicit confirm=True required")

    _check_connection(connection)

    if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_INTENT_TABLE,)).fetchone():
        raise RuntimeError("Journal already exists; refusing replacement")

    connection.execute(
        f'CREATE TABLE "{_INTENT_TABLE}" ('
        "migration_id TEXT PRIMARY KEY NOT NULL, "
        "review_sha256 TEXT NOT NULL, "
        "before_sha256 TEXT NOT NULL, "
        "after_sha256 TEXT NOT NULL, "
        "state TEXT NOT NULL CHECK(state IN ('prepared','uncertain','applied')))"
    )
    connection.commit()


def initialize_intent_journal_locked(connection):
    """Create v2 alongside native Initial under the caller's BEGIN IMMEDIATE.

    This is intentionally separate from the public, create-once journal API.
    """
    if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
        raise RuntimeError("Native Initial requires an active SQLite transaction")

    if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_INTENT_TABLE,)).fetchone():
        raise RuntimeError("Existing successor journal requires manual reconciliation")

    connection.execute(
        f'CREATE TABLE "{_INTENT_TABLE}" ('
        "migration_id TEXT PRIMARY KEY NOT NULL, "
        "review_sha256 TEXT NOT NULL, "
        "before_sha256 TEXT NOT NULL, "
        "after_sha256 TEXT NOT NULL, "
        "state TEXT NOT NULL CHECK(state IN ('prepared','uncertain','applied')))"
    )


def list_intents(connection):
    """Read migration intents from the existing journal without modifying it."""
    _check_connection(connection)
    _require_journal(connection)
    rows = connection.execute(
        f"SELECT migration_id, review_sha256, before_sha256, after_sha256, state "
        f'FROM "{_INTENT_TABLE}" ORDER BY migration_id'
    ).fetchall()
    result = []

    for row in rows:
        _validate_intent(*row[:4])

        if row[4] not in _STATES:
            raise RuntimeError("Unknown journal state")

        result.append(MigrationIntent(*row))

    return tuple(result)


def record_prepared_intent(
    connection, *, migration_id, review_sha256, before_sha256, after_sha256, confirm=False
):
    """Persist a non-executable intent. Never overwrites an existing ID."""

    if confirm is not True:
        raise PermissionError("Explicit confirm=True required")

    _check_connection(connection)
    _validate_intent(migration_id, review_sha256, before_sha256, after_sha256)

    # Serialize participating SQLite writers across the check-and-insert window.
    # This is journal coordination only, not a migration execution lock.
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_journal(connection)
        entries = connection.execute(
            f"SELECT migration_id, review_sha256, before_sha256, after_sha256, state "
            f'FROM "{_INTENT_TABLE}" ORDER BY migration_id'
        ).fetchall()

        for entry in entries:
            _validate_intent(*entry[:4])

            if entry[4] not in _STATES or entry[4] != "applied":
                raise RuntimeError(
                    "Unresolved or invalid migration intent; manual investigation required"
                )

        connection.execute(
            f'INSERT INTO "{_INTENT_TABLE}" VALUES (?, ?, ?, ?, ?)',
            (migration_id, review_sha256, before_sha256, after_sha256, "prepared"),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return migration_id


def assess_intent_journal(connection):
    """Read-only conservative gate: no state is treated as execution approval."""
    entries = list_intents(connection)
    blockers = [
        f"Unresolved intent: {entry.migration_id}" for entry in entries if entry.state != "applied"
    ]
    blockers.append("Execution-time schema equivalence and coordination are not certified")
    return tuple(blockers)


# Intent lifecycle: explicit quarantine and read-only status assessment.


@dataclass(frozen=True)
class LifecycleAssessment:
    migration_id: str
    state: str
    blockers: tuple[str, ...]
    execution_allowed: bool = False


def mark_intent_uncertain(connection, *, migration_id, review_sha256, confirm=False):
    """Durably quarantine a prepared intent; no automatic recovery or retry.

    The review digest is required to prevent accidentally changing another
    migration. An existing uncertain intent is not silently overwritten.
    """

    if confirm is not True:
        raise PermissionError("Explicit confirm=True required")

    _check_connection(connection)
    _validate_intent(migration_id, review_sha256, "0" * 64, "0" * 64)

    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_journal(connection)
        row = connection.execute(
            f'SELECT review_sha256, state FROM "{_INTENT_TABLE}" WHERE migration_id=?',
            (migration_id,),
        ).fetchone()

        if row is None:
            raise RuntimeError("Migration intent not found")

        if row[0] != review_sha256:
            raise PermissionError("Migration review fingerprint mismatch")

        if row[1] != "prepared":
            raise RuntimeError("Only prepared intents may become uncertain")

        cursor = connection.execute(
            f'UPDATE "{_INTENT_TABLE}" SET state=? '
            "WHERE migration_id=? AND review_sha256=? AND state=?",
            ("uncertain", migration_id, review_sha256, "prepared"),
        )

        if cursor.rowcount != 1:
            raise RuntimeError("Intent changed concurrently")

        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return migration_id


def inspect_intent_lifecycle(connection, *, migration_id):
    """Read-only status; even an applied entry is not execution permission."""
    _check_connection(connection)
    entries = list_intents(connection)
    matches = [entry for entry in entries if entry.migration_id == migration_id]

    if len(matches) != 1:
        raise RuntimeError("Migration intent missing or ambiguous")

    entry = matches[0]
    blockers = [f"Intent {entry.migration_id} is {entry.state}"]

    if entry.state == "uncertain":
        blockers.append("Manual investigation required; automatic retry forbidden")

    blockers.append("Live schema equivalence and execution coordination are unverified")
    return LifecycleAssessment(entry.migration_id, entry.state, tuple(blockers))


# Separate AuroraSQL additive column journal; not the SQLite intent journal.

_JOURNAL = "aurorasql_additive_history_v1"


@dataclass(frozen=True)
class AdditiveEntry:
    operation_id: str
    migration_id: str
    table: str
    column: str
    fingerprint: str
    status: str


def _verify_additive_journal(connection):
    """Reject a missing or structurally altered audit journal before use."""
    from .schema_inspection import _reflected_type_name, inspect_table

    observed = inspect_table(connection, _JOURNAL)
    required = {
        "operation_id",
        "migration_id",
        "table_name",
        "column_name",
        "fingerprint",
        "status",
    }
    columns = {item.name: item for item in observed.columns}

    if not observed.exists:
        raise RuntimeError("Additive journal missing; initialize explicitly")

    expected_types = {
        "operation_id": "varchar(36)",
        "migration_id": "varchar(80)",
        "table_name": "varchar(128)",
        "column_name": "varchar(128)",
        "fingerprint": "varchar(64)",
        "status": "varchar(16)",
    }

    if (
        set(columns) != required
        or not columns["operation_id"].primary_key
        or any(columns[name].primary_key for name in required - {"operation_id"})
        or any(columns[name].nullable for name in required)
        or any(columns[name].default_sql is not None for name in required)
        or any(
            _reflected_type_name(columns[name].sql_type, connection.dialect)
            != _reflected_type_name(sql_type, connection.dialect)
            for name, sql_type in expected_types.items()
        )
        or not observed.uniqueness_verified
        or "migration_id" not in observed.unique_columns
        or set(observed.unique_columns) - {"operation_id", "migration_id"}
        or len(observed.indexes) != 2
    ):
        raise RuntimeError("Additive journal structure mismatch; migration blocked")

    # Column reflection alone cannot reveal triggers or table-level CHECK/FK
    # clauses. Never trust an audit journal with executable side effects.
    if connection.dialect == "sqlite":
        ddl = connection.fetch_first(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
            {"name": _JOURNAL},
        )
        trigger = connection.fetch_first(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:name",
            {"name": _JOURNAL},
        )
        temporary_trigger = connection.fetch_first(
            "SELECT name FROM sqlite_temp_master WHERE type='trigger' AND tbl_name=:name",
            {"name": _JOURNAL},
        )
        foreign_keys = connection.fetch_all(f'PRAGMA foreign_key_list("{_JOURNAL}")')

        if (
            ddl is None
            or not isinstance(ddl["sql"], str)
            or trigger is not None
            or temporary_trigger is not None
            or foreign_keys
        ):
            raise RuntimeError("Additive journal has unsupported schema features")

        if re.search(
            r"\b(CHECK|REFERENCES|GENERATED|COLLATE|ON\s+CONFLICT|WITHOUT\s+ROWID|STRICT)\b",
            ddl["sql"],
            re.I,
        ):
            raise RuntimeError("Additive journal has unsupported schema features")


def initialize_additive_history(connection, *, confirm=False):
    """Explicitly create a separate journal; never alter legacy history."""

    if not confirm:
        raise PermissionError("Explicit confirmation required")

    from .migration_coordination import migration_lock
    from .schema import _quote
    from .schema_inspection import inspect_table

    with migration_lock(connection):
        if inspect_table(connection, _JOURNAL).exists:
            raise RuntimeError("Additive journal already exists")

        journal = _quote(_JOURNAL, connection.dialect)
        connection.execute_write(
            f"CREATE TABLE {journal} ("
            "operation_id VARCHAR(36) PRIMARY KEY, "
            "migration_id VARCHAR(80) NOT NULL UNIQUE, "
            "table_name VARCHAR(128) NOT NULL, "
            "column_name VARCHAR(128) NOT NULL, "
            "fingerprint VARCHAR(64) NOT NULL, "
            "status VARCHAR(16) NOT NULL)"
        )

        # Verify that the durable audit structure is actually observable.
        try:
            _verify_additive_journal(connection)
        except RuntimeError as exc:
            raise RuntimeError(
                "Additive journal creation could not be verified: structure mismatch"
            ) from exc


def list_additive_history(connection):
    """Read history without creating or repairing any tables."""

    from .models import _identifier
    from .schema import _quote

    _verify_additive_journal(connection)

    journal = _quote(_JOURNAL, connection.dialect)
    rows = connection.fetch_all(
        "SELECT operation_id, migration_id, table_name, column_name, "
        f"fingerprint, status FROM {journal} ORDER BY operation_id"
    )
    entries = tuple(
        AdditiveEntry(
            row["operation_id"],
            row["migration_id"],
            row["table_name"],
            row["column_name"],
            row["fingerprint"],
            row["status"],
        )
        for row in rows
    )

    # A manually modified journal row must not be treated as proof of success.
    # Preserve unresolved entries for inspection, even if their data is damaged.
    for entry in entries:
        if entry.status == "applied":
            try:
                valid_id = str(UUID(entry.operation_id)) == entry.operation_id
            except (ValueError, TypeError, AttributeError):
                valid_id = False

            fingerprint = entry.fingerprint
            valid_migration = (
                isinstance(entry.migration_id, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", entry.migration_id) is not None
            )

            try:
                _identifier(entry.table)
                _identifier(entry.column)
                valid_columns = True
            except (TypeError, ValueError):
                valid_columns = False

            if (
                not valid_id
                or not valid_migration
                or not valid_columns
                or not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
            ):
                raise RuntimeError("Applied migration journal evidence is invalid")

    return entries
