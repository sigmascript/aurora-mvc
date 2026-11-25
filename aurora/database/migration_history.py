"""Experimental durable audit journal for explicitly approved create-only DDL.

A journal entry is not proof that DDL succeeded: inspect the schema to resolve
started/uncertain entries. Never auto-retry an uncertain operation.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone


from dataclasses import dataclass
from uuid import uuid4

from .schema_inspection import inspect_table
from .schema_inspection import catalog_fingerprint
from .schema import _quote
from .schema import _default_literal
from hashlib import sha256

from .model_discovery import model_identity_map
from .models import Model, column
from .schema import create_table_sql
from .schema_inspection import verify_server_initial_schema
from .migration_timeline import inspect_server_timeline


# Native five-column history discovery and read-only verification.
import json
import re
from .schema_inspection import _objects, _physical
from .snapshots import snapshot_directory
from .snapshots import prepare_schema_record, sqlite_history_name

_FILE = re.compile(r"\d{8}-\d{6}(?:-\d{1,3})?\.json\Z")
_COLUMNS = ("id", "title", "snapshot", "date", "warnings")
TABLE = "__migrations__"
LEGACY_TABLE = "migrations"


def _directory(root, alias):
    return snapshot_directory(root, alias)


def history_table(connection):
    present = set(_objects(connection)) & {TABLE, LEGACY_TABLE}
    if len(present) > 1:
        raise RuntimeError(
            "Both migrations and __migrations__ exist; manual history reconciliation required"
        )
    return next(iter(present), None)


def has_history(connection):
    return history_table(connection) is not None


def _verify_table(connection, name):
    # Compare the exact public field order, not merely the presence of the names.
    if connection.dialect == "mysql":
        rows = connection.fetch_all(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table ORDER BY ORDINAL_POSITION",
            {"table": name},
        )
    else:
        rows = connection.fetch_all(
            "SELECT a.attname AS name FROM pg_catalog.pg_attribute a "
            "WHERE a.attrelid=to_regclass(:table) AND a.attnum>0 "
            "AND NOT a.attisdropped ORDER BY a.attnum",
            {"table": name},
        )
    if tuple(row["name"] for row in rows) != _COLUMNS:
        raise RuntimeError("Server migrations history has an unexpected table definition")
    table = inspect_table(connection, name)
    if (
        not table.exists
        or tuple(observed_column.name for observed_column in table.columns) != _COLUMNS
    ):
        raise RuntimeError("Server migrations history columns are invalid")
    if (
        not table.columns[0].primary_key
        or any(
            observed_column.nullable or observed_column.primary_key
            for observed_column in table.columns[1:4]
        )
        or table.columns[0].nullable
        or not table.columns[4].nullable
        or table.columns[4].primary_key
    ):
        raise RuntimeError("Server migrations history column constraints are invalid")


def read_history(connection, root, alias):
    """Read all snapshots and compare the latest physical witness; never write."""
    objects = _objects(connection)
    selected = history_table(connection)
    if selected is None or any(name.startswith("aurorasql_") for name in objects):
        raise RuntimeError("Unrecognized or mixed server migration histories")
    _verify_table(connection, selected)
    rows = connection.fetch_all(
        f"SELECT id,title,snapshot,date,warnings FROM {selected} ORDER BY id"
    )
    if not rows:
        raise RuntimeError("Server history has no successful initial migration")
    directory = _directory(root, alias)
    latest = None
    seen_snapshots = set()

    for number, row in enumerate(rows, 1):
        filename = row["snapshot"]
        if (
            row["id"] != number
            or not row["title"]
            or not row["date"]
            or not isinstance(filename, str)
            or not _FILE.fullmatch(filename)
        ):
            raise RuntimeError("Server migration history is incomplete or malformed")
        if filename in seen_snapshots:
            raise RuntimeError("Server migration history reuses a snapshot filename")
        seen_snapshots.add(filename)

        if row["warnings"] is not None:
            try:
                warnings = json.loads(row["warnings"])
                if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
                    raise ValueError("Invalid warnings")
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Server migration warnings are malformed") from exc
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Server migration snapshot missing: {filename}")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Server migration snapshot unreadable: {filename}") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"Server migration snapshot invalid: {filename}")

        models = snapshot.get("models")
        physical = snapshot.get("physical_schema")
        model_names = (
            [table.get("name") for table in models if isinstance(table, dict)]
            if isinstance(models, list)
            else []
        )
        table_names = (
            [table.get("table") for table in physical if isinstance(table, dict)]
            if isinstance(physical, list)
            else []
        )

        if (
            snapshot.get("database") != alias
            or snapshot.get("engine") != connection.dialect
            or snapshot.get("title") != row["title"]
            or not isinstance(models, list)
            or not isinstance(physical, list)
            or any(
                not isinstance(table, dict) or not isinstance(table.get("name"), str)
                for table in models
            )
            or any(
                not isinstance(table, dict) or not isinstance(table.get("table"), str)
                for table in physical
            )
            or len(model_names) != len(set(model_names))
            or len(table_names) != len(set(table_names))
            or sorted(model_names) != sorted(table_names)
        ):
            raise RuntimeError(f"Server migration snapshot invalid: {filename}")
        latest = snapshot
    if latest["physical_schema"] != _physical(connection):
        raise RuntimeError(
            "Server physical schema differs from last migration snapshot; manual reconciliation required"
        )
    return rows, latest


from .table_operations import CreateApproval, prepare_create, execute_create
from .migration_safety import RecoveryPreflight, preflight_recovery


_HISTORY_TABLE = "aurorasql_schema_history"


@dataclass(frozen=True)
class HistoryEntry:
    """Recorded migration history entry used for read-only review."""

    operation_id: str
    table: str
    dialect: str
    fingerprint: str
    status: str


def initialize_history(connection, *, confirm=False):
    """Explicitly create an empty audit table; never replace existing history."""

    if not confirm:
        raise PermissionError("Explicit confirm=True required to initialize history")

    if inspect_table(connection, _HISTORY_TABLE).exists:
        raise RuntimeError("History table already exists; refusing to recreate it")

    table = _quote(_HISTORY_TABLE, connection.dialect)

    # Portable across SQLite, MySQL and PostgreSQL; IDs are application generated.
    connection.execute_write(
        f"CREATE TABLE {table} ("
        "operation_id VARCHAR(36) PRIMARY KEY, "
        "table_name VARCHAR(128) NOT NULL, "
        "dialect VARCHAR(16) NOT NULL, "
        "fingerprint VARCHAR(64) NOT NULL, "
        "status VARCHAR(16) NOT NULL)"
    )


def list_history(connection):
    """Read journal entries without creating or repairing history implicitly."""

    if not inspect_table(connection, _HISTORY_TABLE).exists:
        raise RuntimeError("History table is missing; initialize it explicitly")

    table = _quote(_HISTORY_TABLE, connection.dialect)
    rows = connection.fetch_all(
        f"SELECT operation_id, table_name, dialect, fingerprint, status FROM {table} "
        "ORDER BY operation_id"
    )
    return tuple(
        HistoryEntry(
            row["operation_id"],
            row["table_name"],
            row["dialect"],
            row["fingerprint"],
            row["status"],
        )
        for row in rows
    )


def execute_create_recorded(connection, model, approval, *, confirm=False):
    """Record intent before create-only DDL; preserve uncertainty on any failure.

    This is a read-only audit primitive, not a migration executor.
    No retry, repair, or automatic reconciliation is attempted.
    """

    if not confirm:
        raise PermissionError("Explicit confirm=True required for recorded creation")

    from .migration_coordination import migration_lock

    with migration_lock(connection):
        if not inspect_table(connection, _HISTORY_TABLE).exists:
            raise RuntimeError("History table is missing; initialize it explicitly")
        # Validate the approval and existing schema *before* journaling intent.
        from hashlib import sha256

        if not isinstance(approval, CreateApproval):
            raise TypeError("Expected a CreateApproval token")

        sql = create_table_sql(model, connection.dialect)
        expected = CreateApproval(
            model.__table_name__, connection.dialect, sha256(sql.encode("utf-8")).hexdigest()
        )

        if approval != expected:
            raise ValueError("Approval does not match the model and database dialect")

        if inspect_table(connection, model.__table_name__).exists:
            raise RuntimeError("Table already exists; refusing recorded creation")
        # Fail closed on *any* history inconsistency, not only on the target table.
        # This is a preflight guard, NOT a concurrency lock. Other sessions must
        # not run migrations concurrently until coordination is implemented.
        findings = review_history(connection)

        if findings:
            raise RuntimeError(
                "Unresolved migration history or inconsistent records; "
                "manual investigation required before recorded creation"
            )
        # A previous applied record is not permission to recreate a table that
        # disappeared. A history entry is permanent evidence, not a retry token.
        if any(entry.table == approval.table for entry in list_history(connection)):
            raise RuntimeError(
                "Migration history already contains this table; refusing a new create attempt"
            )

        table = _quote(_HISTORY_TABLE, connection.dialect)
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {table} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :name, :dialect, :fingerprint, :status)",
            {
                "id": operation_id,
                "name": approval.table,
                "dialect": approval.dialect,
                "fingerprint": approval.fingerprint,
                "status": "started",
            },
        )

        try:
            execute_create(connection, model, approval, confirm=True)
        except BaseException:
            # An exception from DDL does NOT prove the schema is unchanged.
            try:
                connection.execute_write(
                    f"UPDATE {table} SET status=:status WHERE operation_id=:id",
                    {"status": "uncertain", "id": operation_id},
                )
            except BaseException:
                pass  # Leave 'started' as unresolved; never hide the original error.

            raise
        # If this update fails, 'started' remains an unresolved state; do not
        # report a completed recorded migration to the caller.
        connection.execute_write(
            f"UPDATE {table} SET status=:status WHERE operation_id=:id",
            {"status": "applied", "id": operation_id},
        )
        return operation_id


def unresolved_history(connection, *, table=None):
    """Read-only report of started/uncertain operations; never reconcile automatically."""

    if table is not None:
        from .models import _identifier

        _identifier(table)

    return tuple(
        entry
        for entry in list_history(connection)
        if entry.status != "applied" and (table is None or entry.table == table)
    )


@dataclass(frozen=True)
class HistoryFinding:
    """Read-only finding; never an instruction to repair or retry DDL."""

    operation_id: str
    table: str
    kind: str
    detail: str


def review_history(connection, *, entries=None, table_aliases=None, verified_successor_table=None):
    """Inspect journal consistency and current table existence without mutations.

    An existing table does not prove that its columns match the recorded SQL.
    Likewise, a missing table does not establish that a failed DDL was rolled back.
    Findings must be investigated manually before further migrations.
    """

    if entries is None:
        entries = list_history(connection)

    aliases = table_aliases or {}

    if verified_successor_table is not None:
        from .models import _identifier

        _identifier(verified_successor_table)
    findings = []
    by_table = {}
    by_operation = {}

    for entry in entries:
        by_operation.setdefault(entry.operation_id, []).append(entry)
        by_table.setdefault(entry.table, []).append(entry)

        if entry.status not in ("started", "applied", "uncertain"):
            findings.append(
                HistoryFinding(
                    entry.operation_id, entry.table, "invalid_status", "Unknown journal status"
                )
            )

        if entry.dialect != connection.dialect:
            findings.append(
                HistoryFinding(
                    entry.operation_id,
                    entry.table,
                    "dialect_mismatch",
                    "Journal dialect differs from connection",
                )
            )

        if entry.status in ("started", "uncertain"):
            findings.append(
                HistoryFinding(
                    entry.operation_id,
                    entry.table,
                    "unresolved",
                    "DDL outcome requires manual investigation",
                )
            )
        # Even an applied journal record is not proof of current schema integrity.
        observed = inspect_table(connection, aliases.get(entry.table, entry.table))

        if entry.status == "applied" and not observed.exists:
            findings.append(
                HistoryFinding(
                    entry.operation_id,
                    entry.table,
                    "missing_applied_table",
                    "Recorded table is absent",
                )
            )

        if entry.status in ("started", "uncertain") and observed.exists:
            findings.append(
                HistoryFinding(
                    entry.operation_id,
                    entry.table,
                    "unresolved_table_exists",
                    "Table exists, but schema and DDL outcome are unverified",
                )
            )

    for operation_id, records in by_operation.items():
        if len(records) > 1:
            for entry in records:
                findings.append(
                    HistoryFinding(
                        entry.operation_id,
                        entry.table,
                        "duplicate_operation_id",
                        "Operation identifier appears more than once",
                    )
                )

    for table, records in by_table.items():
        # Native ALTER successors legitimately append one record for the same
        # table, but only their complete timeline verifier may allow it.
        if len(records) > 1 and not (table == verified_successor_table and len(records) == 2):
            for entry in records:
                findings.append(
                    HistoryFinding(
                        entry.operation_id,
                        table,
                        "multiple_operations",
                        "Multiple records target the same table; manual review required",
                    )
                )

    return tuple(findings)


@dataclass(frozen=True)
class RecoveryAssessment:
    """Read-only evidence for manual recovery; never authorizes a history edit."""

    operation_id: str
    table: str
    status: str
    table_exists: bool
    fingerprint_matches: bool
    columns_match: bool
    disposition: str


def assess_recovery(connection, operation_id, model):
    """Assess one unresolved create attempt against a supplied model.

    A matching column shape is *not* full DDL equivalence: constraints,
    indexes, defaults, and backend details may still differ. Consequently this
    function never marks a record applied or safe to retry. Operators must
    investigate and reconcile externally after establishing exclusive access.
    """
    from hashlib import sha256

    from .schema import create_table_sql

    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("A nonempty operation ID is required")

    entries = [entry for entry in list_history(connection) if entry.operation_id == operation_id]

    if len(entries) != 1:
        raise ValueError("Operation ID must identify exactly one history entry")

    entry = entries[0]

    if entry.status not in ("started", "uncertain"):
        raise ValueError("Only unresolved create attempts can be assessed")

    if entry.dialect != connection.dialect:
        raise ValueError("Journal dialect differs from connection")

    sql = create_table_sql(model, connection.dialect)

    if model.__table_name__ != entry.table:
        raise ValueError("Model table does not match history entry")

    fingerprint_matches = sha256(sql.encode("utf-8")).hexdigest() == entry.fingerprint
    observed = inspect_table(connection, entry.table)
    columns_match = False

    if observed.exists:
        expected = model.__columns__
        actual = {col.name: col for col in observed.columns}
        columns_match = len(actual) == len(expected) and all(
            name in actual
            and actual[name].primary_key == field.primary_key
            and actual[name].nullable == (field.nullable and not field.primary_key)
            for name, field in expected.items()
        )

    if not fingerprint_matches:
        disposition = "model_mismatch_manual_investigation"
    elif not observed.exists:
        disposition = "table_absent_manual_investigation"
    elif not columns_match:
        disposition = "schema_mismatch_manual_investigation"
    else:
        disposition = "partial_schema_match_manual_verification_required"

    return RecoveryAssessment(
        entry.operation_id,
        entry.table,
        entry.status,
        observed.exists,
        fingerprint_matches,
        columns_match,
        disposition,
    )


@dataclass(frozen=True)
class RecoverySchemaFinding:
    """A conservative, read-only schema comparison observation."""

    column: str
    kind: str
    detail: str


def review_recovery_schema(connection, operation_id, model):
    """Compare observable column types and uniqueness for an unresolved create.

    This is additional evidence, not proof of DDL equivalence or permission to
    reconcile history. In particular, MySQL/PostgreSQL unique constraints are
    not fully inspected by the current inspector and are reported as unverified.
    """
    from datetime import date, datetime

    assessment = assess_recovery(connection, operation_id, model)

    if not assessment.table_exists:
        return (RecoverySchemaFinding("", "table_missing", "Table does not exist"),)

    observed = inspect_table(connection, assessment.table)
    actual = {observed_column.name: observed_column for observed_column in observed.columns}
    findings = []

    for name, field in model.__columns__.items():
        observed_column = actual.get(name)

        if observed_column is None:
            findings.append(
                RecoverySchemaFinding(name, "missing_column", "Declared column is absent")
            )
            continue

        expected = {
            int: {"sqlite": "INTEGER", "mysql": "BIGINT", "postgresql": "BIGINT"},
            float: {"sqlite": "REAL", "mysql": "DOUBLE", "postgresql": "DOUBLE PRECISION"},
            bool: {"sqlite": "INTEGER", "mysql": "TINYINT(1)", "postgresql": "BOOLEAN"},
            date: {"sqlite": "DATE", "mysql": "DATE", "postgresql": "DATE"},
            datetime: {
                "sqlite": "TIMESTAMP",
                "mysql": "DATETIME",
                "postgresql": "TIMESTAMP WITHOUT TIME ZONE",
            },
        }

        if field.python_type is str:
            expected_type = f"VARCHAR({field.length})" if field.length is not None else "TEXT"
        else:
            expected_type = expected[field.python_type][connection.dialect]

        observed_type = observed_column.sql_type.upper().strip()

        if connection.dialect == "sqlite":
            # SQLite type affinity and generated declarations may differ;
            # report rather than inferring equivalence from affinity.
            equivalent = observed_type == expected_type
        elif connection.dialect == "mysql":
            equivalent = observed_type in (
                {"TINYINT(1)", "TINYINT"} if field.python_type is bool else {expected_type}
            )
        else:
            equivalent = observed_type == expected_type

        if not equivalent:
            findings.append(
                RecoverySchemaFinding(
                    name,
                    "type_mismatch",
                    f"Expected {expected_type}; observed {observed_column.sql_type}",
                )
            )

        if not field.primary_key:
            if observed.uniqueness_verified:
                actual_unique = name in observed.unique_columns

                if field.unique and not actual_unique:
                    findings.append(
                        RecoverySchemaFinding(
                            name, "unique_missing", "Expected single-column UNIQUE index is absent"
                        )
                    )
                elif actual_unique and not field.unique:
                    findings.append(
                        RecoverySchemaFinding(
                            name, "unexpected_unique", "Undeclared single-column uniqueness exists"
                        )
                    )
            elif field.unique:
                findings.append(
                    RecoverySchemaFinding(
                        name, "unique_unverified", "Single-column uniqueness could not be verified"
                    )
                )

    for name in sorted(set(actual) - set(model.__columns__)):
        findings.append(RecoverySchemaFinding(name, "extra_column", "Undeclared column exists"))
    # Defaults, check constraints, foreign keys and concurrent changes are not
    # established by this comparison, even when findings is empty.
    return tuple(findings)


@dataclass(frozen=True)
class MigrationStatus:
    """Operator-facing, read-only summary of the experimental migration journal."""

    total: int
    applied: int
    unresolved: int
    findings: tuple
    ready_for_new_create: bool


def migration_status(connection):
    """Report whether another recorded create can be attempted.

    This is advisory, not approval or a replacement for the locked preflight
    inside execute_create_recorded(). A clean report does not prove that the
    database schema is equivalent to the recorded definitions.
    """
    entries = list_history(connection)
    findings = review_history(connection)
    applied = sum(entry.status == "applied" for entry in entries)
    unresolved = sum(entry.status in ("started", "uncertain") for entry in entries)
    return MigrationStatus(
        total=len(entries),
        applied=applied,
        unresolved=unresolved,
        findings=findings,
        ready_for_new_create=not bool(findings),
    )


from .schema_planning import (
    column_addition_predecessors,
    default_addition_predecessor_models,
    nullability_predecessor_models,
    widening_predecessor_models,
)


# Historical predecessor reconstruction and verification for server column changes.


def verify_server_column_addition_predecessor(connection, models, alias, proposal):
    """Prove a single added nullable field against the original Initial state.

    Return predecessor declarations solely for transaction-local review.
    Never infer a rename, backfill, key change or destructive DDL.
    """
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server column history requires MySQL or PostgreSQL")

    if (
        len(proposal.operations) != 1
        or proposal.operations[0]["kind"] != "add_column"
        or len(proposal.statements) != 1
    ):
        raise ValueError("One reviewed ordinary column addition is required")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) != 1 or timeline[0].status != "applied":
        raise RuntimeError("A verified native Initial is required for column addition")

    if timeline[0].model_tables != model_identity_map(models):
        raise RuntimeError("Historical model-table identities differ")

    predecessor = column_addition_predecessors(models, proposal.operations[0])

    journal = list_history(connection)

    if review_history(connection, entries=journal) or len(journal) != len(predecessor):
        raise RuntimeError("Historical per-table operation journal is unresolved")

    expected = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode("utf-8")
        ).hexdigest()
        for model in predecessor
    }

    if {entry.table for entry in journal} != set(expected) or any(
        entry.status != "applied"
        or entry.dialect != connection.dialect
        or entry.fingerprint != expected[entry.table]
        for entry in journal
    ):
        raise RuntimeError("Original CREATE fingerprint differs from model predecessor")

    try:
        actual = verify_server_initial_schema(connection, predecessor)
    except (ValueError, RuntimeError) as error:
        raise RuntimeError("Physical predecessor does not match approved Initial") from error

    if actual != timeline[0].schema_sha256:
        raise RuntimeError("Historical Initial schema fingerprint differs")

    return tuple(predecessor)


def verify_server_default_predecessor(connection, models, alias, proposal):
    """Require exact original table fingerprints and physical schema equality."""
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server default migration requires MySQL or PostgreSQL")

    if len(proposal.operations) != 1 or proposal.operations[0].get("kind") != "add_default":
        raise ValueError("Exactly one reviewed default addition is required")

    timeline = inspect_server_timeline(connection, alias)

    if (
        timeline is None
        or len(timeline) != 1
        or timeline[0].status != "applied"
        or timeline[0].model_tables != model_identity_map(models)
    ):
        raise RuntimeError("Trusted Initial model mapping is required for default addition")

    previous = default_addition_predecessor_models(models, proposal.operations[0])
    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(previous):
        raise RuntimeError("Original per-table journal is unresolved")

    fingerprints = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode()
        ).hexdigest()
        for model in previous
    }

    if {entry.table for entry in history} != set(fingerprints) or any(
        entry.status != "applied"
        or entry.dialect != connection.dialect
        or entry.fingerprint != fingerprints[entry.table]
        for entry in history
    ):
        raise RuntimeError("Original CREATE fingerprints cannot prove default predecessor")

    try:
        digest = verify_server_initial_schema(connection, previous)
    except (RuntimeError, ValueError) as error:
        raise RuntimeError("Default predecessor differs from physical schema") from error

    if digest != timeline[0].schema_sha256:
        raise RuntimeError("Original Initial schema fingerprint cannot be verified")

    return previous


def verify_server_nullability_predecessor(connection, models, alias, proposal):
    """Require a verified Initial fingerprint and exact pre-ALTER physical schema."""
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server nullability requires MySQL or PostgreSQL")

    if len(proposal.operations) != 1 or proposal.operations[0].get("kind") != "change_nullability":
        raise ValueError("Exactly one reviewed nullability transition is required")

    timeline = inspect_server_timeline(connection, alias)

    if (
        timeline is None
        or len(timeline) != 1
        or timeline[0].status != "applied"
        or timeline[0].model_tables != model_identity_map(models)
    ):
        raise RuntimeError("Trusted Initial model mapping is required for nullability")

    previous = nullability_predecessor_models(models, proposal.operations[0])
    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(previous):
        raise RuntimeError("Original per-table journal is unresolved")

    fingerprints = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode()
        ).hexdigest()
        for model in previous
    }

    if {entry.table for entry in history} != set(fingerprints) or any(
        entry.status != "applied"
        or entry.dialect != connection.dialect
        or entry.fingerprint != fingerprints[entry.table]
        for entry in history
    ):
        raise RuntimeError("Original CREATE fingerprints cannot prove nullability predecessor")

    try:
        digest = verify_server_initial_schema(connection, previous)
    except (RuntimeError, ValueError) as error:
        raise RuntimeError("Nullability predecessor differs from physical schema") from error

    if digest != timeline[0].schema_sha256:
        raise RuntimeError("Original Initial schema fingerprint cannot be verified")

    return previous


def verify_server_widening_predecessor(connection, models, alias, proposal):
    """Require native Initial, CREATE fingerprints and exact pre-DDL catalog."""
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server widening requires MySQL or PostgreSQL")

    if len(proposal.operations) != 1 or proposal.operations[0]["kind"] != "widen_column":
        raise ValueError("Exactly one reviewed widening is required")

    timeline = inspect_server_timeline(connection, alias)

    if (
        timeline is None
        or len(timeline) != 1
        or timeline[0].status != "applied"
        or timeline[0].model_tables != model_identity_map(models)
    ):
        raise RuntimeError("Trusted Initial model mapping is required for widening")

    predecessor = widening_predecessor_models(models, proposal.operations[0])
    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(predecessor):
        raise RuntimeError("Original per-table journal is unresolved")

    fingerprints = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode()
        ).hexdigest()
        for model in predecessor
    }

    if {entry.table for entry in history} != set(fingerprints) or any(
        entry.status != "applied"
        or entry.dialect != connection.dialect
        or entry.fingerprint != fingerprints[entry.table]
        for entry in history
    ):
        raise RuntimeError("Original CREATE fingerprints cannot prove widening predecessor")

    try:
        digest = verify_server_initial_schema(connection, predecessor)
    except (RuntimeError, ValueError) as error:
        raise RuntimeError("Widening predecessor differs from physical schema") from error

    if digest != timeline[0].schema_sha256:
        raise RuntimeError("Original Initial schema fingerprint cannot be verified")

    return predecessor


# Verified native history-chain and history-adoption dependencies.
from .schema_planning import (
    _declared_type,
    _normalized_declaration,
    _historical_column,
    predecessor_models,
    _compatible_widening,
)


def adopt_history(connection, root, alias, *, identity, parsed):
    """Adopt fully verified legacy server history; never touch model tables or files.

    Caller must separately confirm the operation. Recheck identity and all
    historical witnesses while holding the backend migration lock.
    """
    from .connections import _mysql_database_identity, _postgres_database_identity
    from .migration_coordination import _PG_LOCK_KEY, migration_lock

    if connection.dialect not in ("mysql", "postgresql") or identity is None or parsed is None:
        raise ValueError("Verified server database identity is required")
    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("Cannot adopt history inside another migration")
    if history_table(connection) != LEGACY_TABLE:
        raise RuntimeError("There is no single legacy server history table to adopt")

    before = read_history(connection, root, alias)
    identity_fn = (
        _mysql_database_identity if connection.dialect == "mysql" else _postgres_database_identity
    )
    if identity_fn(connection, parsed) != identity:
        raise RuntimeError("Server database identity changed before history adoption")

    def rename_locked():
        if identity_fn(connection, parsed) != identity or history_table(connection) != LEGACY_TABLE:
            raise RuntimeError("Server history identity changed during adoption")
        if read_history(connection, root, alias) != before:
            raise RuntimeError("Server history changed during adoption")
        try:
            connection.execute_write("ALTER TABLE migrations RENAME TO __migrations__")
            if (
                history_table(connection) != TABLE
                or read_history(connection, root, alias) != before
            ):
                raise RuntimeError("Server history adoption verification failed")
        except Exception as exc:
            if connection.dialect == "mysql":
                raise RuntimeError(
                    "MySQL history adoption outcome uncertain after DDL began; "
                    "inspect physical history tables, records and snapshots before retry"
                ) from exc
            raise

    if connection.dialect == "postgresql":
        with connection.transaction():
            row = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not row or row["acquired"] is not True:
                raise RuntimeError("PostgreSQL history adoption lock unavailable")
            rename_locked()
    else:
        with migration_lock(connection):
            rename_locked()
    return len(before[0])


def reviewed_statement(models, operation, dialect):
    """Recreate a supported successor's original reviewed DDL statement."""
    from .migration_review import _add_column_statement

    kind = operation["kind"]
    matching = [model for model in models if model.__table_name__ == operation["table"]]

    if len(matching) != 1:
        raise ValueError("Reviewed server table is missing or ambiguous")

    model = matching[0]

    if kind == "create_table":
        keys = [field for field in model.__columns__.values() if field.primary_key]

        if (
            len(keys) != 1
            or keys[0].python_type is not int
            or any(
                field.references is not None or field.server_default is not None
                for field in model.__columns__.values()
            )
        ):
            raise ValueError("Historical server table creation has unsupported constraints")

        return create_table_sql(model, dialect)

    name = operation["column"]

    if name not in model.__columns__:
        raise ValueError("Reviewed server column is missing")

    field = model.__columns__[name]
    table_sql = _quote(operation["table"], dialect)
    column_sql = _quote(name, dialect)

    if (
        field.primary_key
        or field.unique
        or field.references is not None
        or field.server_default is not None
    ):
        raise ValueError("Reviewed server column has unsupported dependencies")

    if kind == "add_column":
        return _add_column_statement(model, name, dialect)

    if kind == "widen_column":
        if field.default is not None:
            raise ValueError("Defaulted column widening is not supported")

        if not _compatible_widening(operation["from"], operation["to"]):
            raise ValueError("Reviewed widening would lose precision or capacity")

        declared = _declared_type(field, dialect)

        if _normalized_declaration(declared, dialect) != operation["to"]:
            raise ValueError("Reviewed widening target differs from declaration")

        if dialect == "postgresql":
            return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} TYPE {declared}"

        nullability = " NULL" if field.nullable else " NOT NULL"
        return f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} {declared}{nullability}"

    if kind == "change_nullability":
        if field.default is not None:
            raise ValueError("Defaulted column nullability change is not supported")

        if field.nullable is not operation["to"]:
            raise ValueError("Reviewed nullability differs from declaration")

        if dialect == "postgresql":
            action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
            return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {action}"

        nullability = "NULL" if field.nullable else "NOT NULL"
        return (
            f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} "
            f"{_declared_type(field, dialect)} {nullability}"
        )

    if kind == "add_default":
        literal = _default_literal(field, dialect)

        if literal is None:
            raise ValueError("Reviewed SQL default is missing")

        return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} SET DEFAULT {literal}"

    raise ValueError("Unsupported server operation evidence")


def preceding_observations(observations, operation, dialect):
    """Undo one approved catalog difference in read-only observation data."""
    before = deepcopy(list(observations))
    matching = [table for table in before if table["table"] == operation["table"]]

    if len(matching) != 1:
        raise ValueError("Historical catalog target is missing or ambiguous")

    table = matching[0]
    kind = operation["kind"]

    if kind == "create_table":
        before.remove(table)
        return before

    column_name = operation["column"]
    matches = [item for item in table["columns"] if item[0] == column_name]

    if (
        len(matches) != 1
        or matches[0][3]
        or column_name in table["unique_columns"]
        or len(table["indexes"]) != 1 + len(table["unique_columns"])
    ):
        raise ValueError("Historical catalog column has dependencies")

    observed = matches[0]

    if kind == "add_column":
        if not observed[2]:
            raise ValueError("Historical nullable column addition is incompatible")

        table["columns"] = [item for item in table["columns"] if item[0] != column_name]
    elif kind == "widen_column":
        if not _compatible_widening(operation["from"], operation["to"]):
            raise ValueError("Historical column widening is incompatible")

        current = _normalized_declaration(observed[1], dialect)

        if current != operation["to"]:
            raise ValueError("Historical column type differs from recorded successor")

        previous_type = operation["from"]

        if dialect == "postgresql":
            if previous_type.startswith("varchar("):
                previous_type = "character varying(" + previous_type[len("varchar(") :]
            elif previous_type.startswith("decimal("):
                previous_type = "numeric(" + previous_type[len("decimal(") :]

        table["columns"] = [
            (item[0], previous_type, *item[2:]) if item[0] == column_name else item
            for item in table["columns"]
        ]
    elif kind == "change_nullability":
        if observed[2] is not operation["to"] or operation["from"] is operation["to"]:
            raise ValueError("Historical column nullability differs")

        table["columns"] = [
            (item[0], item[1], operation["from"], *item[3:]) if item[0] == column_name else item
            for item in table["columns"]
        ]
    elif kind == "add_default":
        if observed[4] is None:
            raise ValueError("Historical column default is missing")

        table["columns"] = [
            (*item[:4], None) if item[0] == column_name else item for item in table["columns"]
        ]
    else:
        raise ValueError("Unsupported catalog history operation")

    return before


def expected_server_journal(
    models, timeline, dialect, identity, observations, *, current_model_mapping=None
):
    """Validate all recorded successor digests and reconstruct journal hashes.

    The caller must first verify current physical catalog equivalence and the
    timeline's strict structural rules. This function performs no SQL or I/O.
    """
    if not timeline or any(item.status != "applied" for item in timeline):
        raise RuntimeError("An unresolved server history cannot be reconstructed")

    remaining_models = tuple(models)
    remaining_catalog = deepcopy(list(observations))
    hashes = []
    model_mapping = dict(timeline[-1].model_tables)
    selected_mapping = (
        model_identity_map(remaining_models)
        if current_model_mapping is None
        else current_model_mapping
    )

    if (
        catalog_fingerprint(dialect, remaining_catalog) != timeline[-1].schema_sha256
        or selected_mapping != model_mapping
        or any(item.database_alias != timeline[0].database_alias for item in timeline)
    ):
        raise RuntimeError("Current server catalog or model mapping differs from history")

    for index in range(len(timeline) - 1, 0, -1):
        entry = timeline[index]
        statement = reviewed_statement(remaining_models, entry.operation, dialect)
        payload = json.dumps(
            {
                "dialect": dialect,
                "alias": entry.database_alias,
                "identity": identity,
                "operations": (entry.operation,),
                "statements": (statement,),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        if sha256(payload.encode("utf-8")).hexdigest() != entry.review_sha256:
            raise RuntimeError("Historical reviewed server statement differs")

        hashes.append((entry.operation["table"], sha256(statement.encode("utf-8")).hexdigest()))
        remaining_models = predecessor_models(remaining_models, entry.operation, dialect)
        remaining_catalog = preceding_observations(remaining_catalog, entry.operation, dialect)

        if entry.operation["kind"] == "create_table":
            added = [
                key for key, value in model_mapping.items() if value == entry.operation["table"]
            ]

            if len(added) != 1:
                raise RuntimeError("Historical server model creation is ambiguous")

            del model_mapping[added[0]]

        if model_mapping != timeline[index - 1].model_tables:
            raise RuntimeError("Historical server model identity chain differs")

        if catalog_fingerprint(dialect, remaining_catalog) != timeline[index - 1].schema_sha256:
            raise RuntimeError("Historical server catalog fingerprint differs")

    if {model.__table_name__ for model in remaining_models} != set(
        timeline[0].model_tables.values()
    ):
        raise RuntimeError("Historical Initial model tables differ")

    hashes.extend(
        (model.__table_name__, sha256(create_table_sql(model, dialect).encode("utf-8")).hexdigest())
        for model in remaining_models
    )
    return Counter(hashes)


def verify_server_applied_chain(connection, models, alias, identity, *, current_model_mapping=None):
    """Prove a repeated native chain against the current catalog and journals.

    No timeline status, recorded hash or model declaration alone is enough.
    All three independent evidence families must agree without mutation.
    The Initial-only and single-successor legacy verifiers stay separate until
    compatibility acceptance establishes their unchanged behavior.
    """
    from .migration_history import list_history
    from .migration_timeline import inspect_server_timeline
    from .schema_inspection import inspect_table, verify_server_declared_schema

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) < 2 or any(item.status != "applied" for item in timeline):
        return False

    try:
        journal_table = inspect_table(connection, "aurorasql_schema_history")
        journal_columns = {
            observed_column.name: observed_column for observed_column in journal_table.columns
        }
        required = {"operation_id", "table_name", "dialect", "fingerprint", "status"}

        if (
            not journal_table.exists
            or set(journal_columns) != required
            or not journal_columns["operation_id"].primary_key
            or any(observed_column.nullable for observed_column in journal_columns.values())
            or any(
                observed_column.primary_key
                for name, observed_column in journal_columns.items()
                if name != "operation_id"
            )
            or not journal_table.uniqueness_verified
            or journal_table.unique_columns
            or len(journal_table.indexes) != 1
        ):
            return False

        if connection.dialect == "mysql":
            triggers = connection.fetch_all(
                "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND EVENT_OBJECT_TABLE IN ('aurorasql_schema_history', 'aurorasql_migration_timeline')"
            )
        elif connection.dialect == "postgresql":
            triggers = connection.fetch_all(
                "SELECT t.tgname AS name FROM pg_catalog.pg_trigger t "
                "WHERE t.tgrelid IN (to_regclass('aurorasql_schema_history'), "
                "to_regclass('aurorasql_migration_timeline')) AND NOT t.tgisinternal"
            )
        else:
            return False

        if triggers:
            return False

        digest, observations = verify_server_declared_schema(
            connection, models, include_observations=True
        )

        if digest != timeline[-1].schema_sha256:
            return False

        expected = expected_server_journal(
            models,
            timeline,
            connection.dialect,
            identity,
            observations,
            current_model_mapping=current_model_mapping,
        )
        journal = list_history(connection)

        if (
            len(journal) != sum(expected.values())
            or len({entry.operation_id for entry in journal}) != len(journal)
            or any(
                entry.status != "applied" or entry.dialect != connection.dialect
                for entry in journal
            )
        ):
            return False

        actual = Counter((entry.table, entry.fingerprint) for entry in journal)
        return actual == expected
    except (ValueError, TypeError, RuntimeError, KeyError, IndexError):
        return False


def verify_server_review_predecessor(connection, models, alias, identity, review):
    """Prove the entire physical state before a proposed later successor.

    Derive only one supported previous model state from the proposal. The
    caller rechecks the exact proposal and database identity under its held
    backend lock before using this as one prerequisite for a prepared intent.
    """
    from .migration_timeline import inspect_server_timeline

    if len(review.operations) != 1:
        raise ValueError("One reviewed server operation is required")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) < 2:
        raise RuntimeError("A previously applied successor is required")

    if any(item.status != "applied" for item in timeline):
        raise RuntimeError("Unresolved server history cannot authorize a successor")

    operation = review.operations[0]
    earlier = predecessor_models(models, operation, connection.dialect)
    mapping = dict(model_identity_map(models))

    if operation["kind"] == "create_table":
        added = [key for key, value in mapping.items() if value == operation["table"]]

        if len(added) != 1:
            raise ValueError("New model identity is ambiguous")

        del mapping[added[0]]

    if mapping != timeline[-1].model_tables:
        raise RuntimeError("Proposed model identities differ from the applied predecessor")

    if not verify_server_applied_chain(
        connection, earlier, alias, identity, current_model_mapping=mapping
    ):
        raise RuntimeError("Complete prior server history or physical schema is unverified")

    return timeline[-1].number + 1


# SQLite forward-history uses the same five-column contract, but distinct
# catalog and transaction semantics from native MySQL/PostgreSQL history.

SQLITE_TABLE = "__migrations__"
SQLITE_LEGACY_TABLE = "migrations"
SQLITE_BACKUP_TABLE = "__backups__"
_SQLITE_SNAPSHOT = re.compile(r"[0-9]{8}-[0-9]{6}(?:-[0-9]{1,3})?\.json\Z")
_SQLITE_OLD = {
    "aurorasql_migration_timeline",
    "aurorasql_schema_history",
    "aurorasql_migration_intents_v2",
    "aurorasql_additive_history_v1",
}


def _sqlite_tables(raw):
    return {
        row[0]
        for row in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def sqlite_history_table(raw):
    """Select one history table; never guess between conflicting histories."""
    found = _sqlite_tables(raw) & {SQLITE_TABLE, SQLITE_LEGACY_TABLE}
    if len(found) > 1:
        raise RuntimeError(
            "Both migrations and __migrations__ exist; manual history reconciliation required"
        )
    return next(iter(found), None)


def sqlite_has_forward_history(raw):
    return sqlite_history_table(raw) is not None


def _sqlite_physical(raw):
    return [
        {"type": kind, "name": name, "table": table, "sql": sql}
        for kind, name, table, sql in raw.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
        if name not in _SQLITE_OLD
        and name not in (SQLITE_TABLE, SQLITE_LEGACY_TABLE, SQLITE_BACKUP_TABLE)
    ]


def sqlite_read_history(raw, project_root, db_file):
    """Read and verify the latest successful schema witness; never mutate."""
    tables = _sqlite_tables(raw)
    selected = sqlite_history_table(raw)
    if selected is None or tables.intersection(_SQLITE_OLD):
        raise RuntimeError("Database does not contain a recognized single-table migration history")
    columns = raw.execute(f'PRAGMA table_info("{selected}")').fetchall()
    if [item[1] for item in columns] != ["id", "title", "snapshot", "date", "warnings"] or columns[
        0
    ][5] != 1:
        raise RuntimeError("Migration history table definition is unrecognized")
    records = raw.execute(
        f'SELECT id,title,snapshot,date,warnings FROM "{selected}" ORDER BY id'
    ).fetchall()
    if not records:
        raise RuntimeError("Migration history has no initial record")
    directory, _ = prepare_schema_record(project_root, db_file, ())
    last_snapshot = None
    seen_snapshots = set()
    for expected, (ident, title, filename, date, warnings) in enumerate(records, 1):
        if (
            ident != expected
            or not isinstance(title, str)
            or not title
            or not isinstance(filename, str)
            or not _SQLITE_SNAPSHOT.fullmatch(filename)
            or not isinstance(date, str)
            or not date
        ):
            raise RuntimeError("Migration history is incomplete or contains invalid entries")
        if filename in seen_snapshots:
            raise RuntimeError("Migration history reuses a snapshot filename")
        seen_snapshots.add(filename)
        if warnings is not None:
            try:
                parsed = json.loads(warnings)
                if not isinstance(parsed, list) or any(not isinstance(w, str) for w in parsed):
                    raise ValueError("Invalid warnings")
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Migration warnings are malformed") from exc
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Migration snapshot missing: {filename}; inspect before retrying")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Migration snapshot is unreadable: {filename}") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"Migration snapshot is invalid: {filename}")
        models = snapshot.get("models")
        physical = snapshot.get("physical_schema")
        model_names = (
            [table.get("name") for table in models if isinstance(table, dict)]
            if isinstance(models, list)
            else []
        )
        physical_names = (
            [
                item.get("name")
                for item in physical
                if isinstance(item, dict) and item.get("type") == "table"
            ]
            if isinstance(physical, list)
            else []
        )
        if (
            snapshot.get("database") != sqlite_history_name(project_root, db_file)
            or snapshot.get("engine") != "sqlite"
            or snapshot.get("title") != title
            or not isinstance(models, list)
            or not isinstance(physical, list)
            or any(
                not isinstance(table, dict) or not isinstance(table.get("name"), str)
                for table in models
            )
            or any(
                not isinstance(item, dict)
                or not all(
                    isinstance(item.get(key), str) for key in ("type", "name", "table", "sql")
                )
                for item in physical
            )
            or len(model_names) != len(set(model_names))
            or len(physical_names) != len(set(physical_names))
            or sorted(model_names) != sorted(physical_names)
        ):
            raise RuntimeError(f"Migration snapshot is invalid: {filename}")
        last_snapshot = snapshot
    if last_snapshot["physical_schema"] != _sqlite_physical(raw):
        raise RuntimeError("Database schema differs from the latest migration snapshot")
    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Database has foreign-key violations")
    return records, last_snapshot


# SQLite history adoption and record creation preserve their distinct transaction
# boundary. The SQLite execution owner calls the record writer only in a held txn.


def sqlite_adopt_history(raw, root, db_file):
    """Explicitly adopt verified five-column SQLite history without rewriting records.

    This is an internal-table rename, not a new application schema migration.
    Existing JSON witnesses remain intact. Never reset or invent history.
    """
    if raw.in_transaction:
        raise RuntimeError("History adoption requires its own transaction")
    if sqlite_history_table(raw) != SQLITE_LEGACY_TABLE:
        raise RuntimeError("There is no legacy history table to adopt")
    before, snapshot = sqlite_read_history(raw, root, db_file)
    raw.execute("PRAGMA foreign_keys=ON")
    try:
        raw.execute("BEGIN IMMEDIATE")
        if sqlite_history_table(raw) != SQLITE_LEGACY_TABLE or sqlite_read_history(
            raw, root, db_file
        ) != (before, snapshot):
            raise RuntimeError("History changed during adoption; retry only after review")
        raw.execute(f'ALTER TABLE "{SQLITE_LEGACY_TABLE}" RENAME TO "{SQLITE_TABLE}"')
        after, verified = sqlite_read_history(raw, root, db_file)
        if after != before or verified != snapshot:
            raise RuntimeError("History adoption verification failed")
        if raw.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite integrity verification failed during history adoption")
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    return len(before)


def sqlite_new_record(raw, root, db_file, models, title, warnings=()):
    """Write snapshot from uncommitted catalog, then history in one SQLite txn.

    Callers remove the just-created file if the transaction fails. Crashes may
    leave an orphan snapshot, never a fictitious successfully applied DB row.
    """
    directory, model_snapshot = prepare_schema_record(root, db_file, models)
    if not raw.in_transaction:
        raise RuntimeError("A locked SQLite transaction is required")
    if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Foreign-key verification failed")
    if raw.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("SQLite integrity verification failed")
    content = {
        "database": sqlite_history_name(root, db_file),
        "engine": "sqlite",
        "title": title,
        "models": model_snapshot["tables"],
        "physical_schema": _sqlite_physical(raw),
    }
    actual_tables = {x["name"] for x in content["physical_schema"] if x["type"] == "table"}
    if actual_tables != {model.__table_name__ for model in models}:
        raise RuntimeError("Database tables differ from selected models")
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    for attempt in range(1000):
        name = f"{timestamp}{('-' + str(attempt)) if attempt else ''}.json"
        path = directory / name
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
            break
        except FileExistsError:
            continue
        except OSError:
            path.unlink(missing_ok=True)
            raise
    else:
        raise RuntimeError("No available migration snapshot filename")
    date = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        raw.execute(
            f'INSERT INTO "{sqlite_history_table(raw)}" (title,snapshot,date,warnings) VALUES (?,?,?,?)',
            (title, name, date, json.dumps(list(warnings)) if warnings else None),
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


# The SQLite legacy conversion is transactional and only follows a complete
# independently verified historical snapshot chain.
SQLITE_DDL = (
    'CREATE TABLE "__migrations__" ('
    '"id" INTEGER PRIMARY KEY, "title" TEXT NOT NULL, '
    '"snapshot" TEXT NOT NULL, "date" TEXT NOT NULL, "warnings" TEXT)'
)


def sqlite_convert_legacy_initial(raw, root, db_file, models, alias):
    """Replace a fully evidenced alpha journal atomically, with no DDL to app tables.

    The existing snapshot is reused; no historical schema or timestamp is invented.
    Histories missing any original complete snapshot remain ineligible.
    """
    from .migration_review import sqlite_legacy_initial_review
    from .migration_review import sqlite_matches_models as _sqlite_matches_models
    from .schema import validate_init_models

    models = validate_init_models(models, dialect="sqlite")
    if raw.in_transaction:
        raise RuntimeError("History conversion requires its own SQLite transaction")
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    proposal = sqlite_legacy_initial_review(raw, root, db_file, models, alias)
    if proposal is None:
        raise RuntimeError("Legacy history cannot be safely converted from existing evidence")
    try:
        raw.execute("BEGIN IMMEDIATE")
        if sqlite_legacy_initial_review(raw, root, db_file, models, alias) != proposal:
            raise RuntimeError("Legacy history changed before conversion")
        raw.execute(SQLITE_DDL)
        for ident, (title, filename, date) in enumerate(proposal, 1):
            raw.execute(
                f'INSERT INTO "{SQLITE_TABLE}" (id,title,snapshot,date,warnings) VALUES (?,?,?,?,NULL)',
                (ident, title, filename, date),
            )
        for name in sorted(_sqlite_tables(raw) & _SQLITE_OLD):
            raw.execute(f"DROP TABLE {_quote(name, 'sqlite')}")
        sqlite_read_history(raw, root, db_file)
        _sqlite_matches_models(raw, models)
        if raw.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Database integrity check failed during history conversion")
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    return proposal[-1][1]
