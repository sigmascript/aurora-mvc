"""Recovery preflight is coordinated and strictly read-only."""

import pytest

from aurora.aurorasql import (
    Model, column, connect, initialize_history, prepare_create,
    preflight_recovery, list_history, inspect_table,
)


class RecoveryProbe(Model):
    id = column(int, primary_key=True)
    title = column(str, length=64)


def _unresolved(db, *, status="uncertain"):
    approval = prepare_create(db, RecoveryProbe)
    db.execute_write(
        'INSERT INTO "aurorasql_schema_history" '
        '(operation_id, table_name, dialect, fingerprint, status) '
        'VALUES (:id, :name, :dialect, :fingerprint, :status)',
        {"id": "recovery-op", "name": approval.table, "dialect": "sqlite",
         "fingerprint": approval.fingerprint, "status": status},
    )


def test_preflight_reports_missing_table_without_mutating_history():
    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        _unresolved(db)
        before = list_history(db)
        result = preflight_recovery(db, "recovery-op", RecoveryProbe)
        assert result.operation_id == "recovery-op"
        assert result.assessment.disposition == "table_absent_manual_investigation"
        assert any(item.kind == "table_missing" for item in result.schema_findings)
        assert any(item.kind == "unresolved" for item in result.history_findings)
        assert result.reconciliation_allowed is False
        assert list_history(db) == before
        assert not inspect_table(db, "recovery_probe").exists


def test_preflight_does_not_approve_even_when_table_exists():
    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        _unresolved(db, status="started")
        db.execute_write('CREATE TABLE "recovery_probe" '
                         '("id" INTEGER PRIMARY KEY, "title" VARCHAR(64) NOT NULL)')
        before = list_history(db)
        result = preflight_recovery(db, "recovery-op", RecoveryProbe)
        assert result.assessment.table_exists
        assert result.reconciliation_allowed is False
        assert list_history(db) == before


def test_preflight_refuses_missing_or_applied_operations():
    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        with pytest.raises(ValueError):
            preflight_recovery(db, "not-found", RecoveryProbe)
        _unresolved(db, status="applied")
        with pytest.raises(ValueError, match="unresolved"):
            preflight_recovery(db, "recovery-op", RecoveryProbe)


def test_preflight_refuses_nested_coordination():
    from aurora.aurorasql.coordination import migration_lock

    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        _unresolved(db)
        with migration_lock(db):
            with pytest.raises(RuntimeError, match="nested"):
                preflight_recovery(db, "recovery-op", RecoveryProbe)
