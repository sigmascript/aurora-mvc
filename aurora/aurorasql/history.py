"""Experimental durable audit journal for explicitly approved create-only DDL.

A journal entry is not proof that DDL succeeded: inspect the schema to resolve
started/uncertain entries. Never auto-retry an uncertain operation.
"""

from dataclasses import dataclass
from uuid import uuid4

from .execution import execute_create
from .inspection import inspect_table
from .schema import _quote


_HISTORY_TABLE = "aurorasql_schema_history"


@dataclass(frozen=True)
class HistoryEntry:
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
    return tuple(HistoryEntry(row["operation_id"], row["table_name"], row["dialect"],
                              row["fingerprint"], row["status"]) for row in rows)


def execute_create_recorded(connection, model, approval, *, confirm=False):
    """Record intent before create-only DDL; preserve uncertainty on any failure.

    This is an experimental audit primitive, not an Alpha 4 migration replacement.
    No retry, repair, or automatic reconciliation is attempted.
    """
    if not confirm:
        raise PermissionError("Explicit confirm=True required for recorded creation")
    from .coordination import migration_lock
    with migration_lock(connection):
        if not inspect_table(connection, _HISTORY_TABLE).exists:
            raise RuntimeError("History table is missing; initialize it explicitly")
        # Validate the approval and existing schema *before* journaling intent.
        from .execution import CreateApproval
        from .schema import create_table_sql
        from hashlib import sha256
        if not isinstance(approval, CreateApproval):
            raise TypeError("Expected a CreateApproval token")
        sql = create_table_sql(model, connection.dialect)
        expected = CreateApproval(model.__table_name__, connection.dialect,
                                  sha256(sql.encode("utf-8")).hexdigest())
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
            {"id": operation_id, "name": approval.table, "dialect": approval.dialect,
             "fingerprint": approval.fingerprint, "status": "started"},
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
    return tuple(entry for entry in list_history(connection)
                 if entry.status != "applied" and (table is None or entry.table == table))


@dataclass(frozen=True)
class HistoryFinding:
    """Read-only finding; never an instruction to repair or retry DDL."""

    operation_id: str
    table: str
    kind: str
    detail: str


def review_history(connection):
    """Inspect journal consistency and current table existence without mutations.

    An existing table does not prove that its columns match the recorded SQL.
    Likewise, a missing table does not establish that a failed DDL was rolled back.
    Findings must be investigated manually before further migrations.
    """
    entries = list_history(connection)
    findings = []
    by_table = {}
    for entry in entries:
        by_table.setdefault(entry.table, []).append(entry)
        if entry.status not in ("started", "applied", "uncertain"):
            findings.append(HistoryFinding(entry.operation_id, entry.table,
                                           "invalid_status", "Unknown journal status"))
        if entry.dialect != connection.dialect:
            findings.append(HistoryFinding(entry.operation_id, entry.table,
                                           "dialect_mismatch", "Journal dialect differs from connection"))
        if entry.status in ("started", "uncertain"):
            findings.append(HistoryFinding(entry.operation_id, entry.table,
                                           "unresolved", "DDL outcome requires manual investigation"))
        # Even an applied journal record is not proof of current schema integrity.
        observed = inspect_table(connection, entry.table)
        if entry.status == "applied" and not observed.exists:
            findings.append(HistoryFinding(entry.operation_id, entry.table,
                                           "missing_applied_table", "Recorded table is absent"))
        if entry.status in ("started", "uncertain") and observed.exists:
            findings.append(HistoryFinding(entry.operation_id, entry.table,
                                           "unresolved_table_exists", "Table exists, but schema and DDL outcome are unverified"))
    for table, records in by_table.items():
        if len(records) > 1:
            for entry in records:
                findings.append(HistoryFinding(entry.operation_id, table,
                                               "multiple_operations", "Multiple records target the same table; manual review required"))
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
    entries = [entry for entry in list_history(connection)
               if entry.operation_id == operation_id]
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
        columns_match = (len(actual) == len(expected)
                         and all(name in actual
                                 and actual[name].primary_key == field.primary_key
                                 and actual[name].nullable == (field.nullable and not field.primary_key)
                                 for name, field in expected.items()))
    if not fingerprint_matches:
        disposition = "model_mismatch_manual_investigation"
    elif not observed.exists:
        disposition = "table_absent_manual_investigation"
    elif not columns_match:
        disposition = "schema_mismatch_manual_investigation"
    else:
        disposition = "partial_schema_match_manual_verification_required"
    return RecoveryAssessment(entry.operation_id, entry.table, entry.status,
                              observed.exists, fingerprint_matches,
                              columns_match, disposition)


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
    actual = {column.name: column for column in observed.columns}
    findings = []
    for name, field in model.__columns__.items():
        column = actual.get(name)
        if column is None:
            findings.append(RecoverySchemaFinding(name, "missing_column", "Declared column is absent"))
            continue
        expected = {
            int: {"sqlite": "INTEGER", "mysql": "BIGINT", "postgresql": "BIGINT"},
            float: {"sqlite": "REAL", "mysql": "DOUBLE", "postgresql": "DOUBLE PRECISION"},
            bool: {"sqlite": "INTEGER", "mysql": "TINYINT(1)", "postgresql": "BOOLEAN"},
            date: {"sqlite": "DATE", "mysql": "DATE", "postgresql": "DATE"},
            datetime: {"sqlite": "TIMESTAMP", "mysql": "DATETIME", "postgresql": "TIMESTAMP WITHOUT TIME ZONE"},
        }
        if field.python_type is str:
            expected_type = f"VARCHAR({field.length})" if field.length is not None else "TEXT"
        else:
            expected_type = expected[field.python_type][connection.dialect]
        observed_type = column.sql_type.upper().strip()
        if connection.dialect == "sqlite":
            # SQLite type affinity and generated declarations may differ;
            # report rather than inferring equivalence from affinity.
            equivalent = observed_type == expected_type
        elif connection.dialect == "mysql":
            equivalent = observed_type in ({"TINYINT(1)", "TINYINT"} if field.python_type is bool
                                           else {expected_type})
        else:
            equivalent = observed_type == expected_type
        if not equivalent:
            findings.append(RecoverySchemaFinding(name, "type_mismatch",
                                                  f"Expected {expected_type}; observed {column.sql_type}"))
        if not field.primary_key:
            if observed.uniqueness_verified:
                actual_unique = name in observed.unique_columns
                if field.unique and not actual_unique:
                    findings.append(RecoverySchemaFinding(name, "unique_missing",
                                                          "Expected single-column UNIQUE index is absent"))
                elif actual_unique and not field.unique:
                    findings.append(RecoverySchemaFinding(name, "unexpected_unique",
                                                          "Undeclared single-column uniqueness exists"))
            elif field.unique:
                findings.append(RecoverySchemaFinding(name, "unique_unverified",
                                                      "Single-column uniqueness could not be verified"))
    for name in sorted(set(actual) - set(model.__columns__)):
        findings.append(RecoverySchemaFinding(name, "extra_column", "Undeclared column exists"))
    # Defaults, check constraints, foreign keys and concurrent changes are not
    # established by this comparison, even when findings is empty.
    return tuple(findings)


@dataclass(frozen=True)
class RecoveryPreflight:
    """Coordinated read-only evidence; never permission to reconcile history."""

    operation_id: str
    assessment: RecoveryAssessment
    schema_findings: tuple
    history_findings: tuple
    reconciliation_allowed: bool = False


def preflight_recovery(connection, operation_id, model):
    """Collect a consistent recovery snapshot under AuroraSQL migration coordination.

    No journal updates, DDL retries or automatic decisions. In particular, a
    partial column match cannot establish full constraint/default equivalence.
    Operators must not treat this snapshot as an authorization to reconcile.
    """
    from .coordination import migration_lock

    with migration_lock(connection):
        assessment = assess_recovery(connection, operation_id, model)
        schema_findings = review_recovery_schema(connection, operation_id, model)
        history_findings = review_history(connection)
        # A migration's unresolved entry is expected, but every other finding
        # remains visible for manual investigation. Do not filter evidence.
        return RecoveryPreflight(operation_id, assessment, tuple(schema_findings),
                                 tuple(history_findings))


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
        total=len(entries), applied=applied, unresolved=unresolved,
        findings=findings, ready_for_new_create=not bool(findings),
    )
