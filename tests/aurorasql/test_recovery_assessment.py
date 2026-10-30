"""Read-only recovery evidence must not mutate uncertain journal entries."""

import pytest

from aurora.aurorasql import (Model, assess_recovery, column, connect,
                              create_table, initialize_history, list_history,
                              prepare_create)


class RecoveryNote(Model):
    id = column(int, primary_key=True)
    title = column(str, length=60)


class ChangedRecoveryNote(Model):
    __table__ = "recovery_note"
    id = column(int, primary_key=True)
    different = column(str, length=60)


def seed(db, status="uncertain"):
    initialize_history(db, confirm=True)
    approval = prepare_create(db, RecoveryNote)
    db.execute_write(
        'INSERT INTO "aurorasql_schema_history" '
        '(operation_id, table_name, dialect, fingerprint, status) '
        'VALUES (:id, :name, :dialect, :fingerprint, :status)',
        {"id": "recovery-probe", "name": "recovery_note", "dialect": "sqlite",
         "fingerprint": approval.fingerprint, "status": status},
    )


def test_absent_table_requires_manual_investigation():
    with connect("sqlite:///:memory:") as db:
        seed(db)
        result = assess_recovery(db, "recovery-probe", RecoveryNote)
        assert not result.table_exists
        assert result.fingerprint_matches
        assert result.disposition == "table_absent_manual_investigation"
        assert list_history(db)[0].status == "uncertain"


def test_existing_table_is_not_automatically_reconciled():
    with connect("sqlite:///:memory:") as db:
        seed(db, "started")
        create_table(db, RecoveryNote)
        result = assess_recovery(db, "recovery-probe", RecoveryNote)
        assert result.columns_match
        assert result.disposition == "partial_schema_match_manual_verification_required"
        assert list_history(db)[0].status == "started"


def test_mismatched_model_and_resolved_entry_are_rejected():
    with connect("sqlite:///:memory:") as db:
        seed(db)
        result = assess_recovery(db, "recovery-probe", ChangedRecoveryNote)
        assert not result.fingerprint_matches
        assert result.disposition == "model_mismatch_manual_investigation"
        with pytest.raises(ValueError, match="exactly one"):
            assess_recovery(db, "missing-id", RecoveryNote)
        db.execute_write('UPDATE "aurorasql_schema_history" SET status=:status',
                         {"status": "applied"})
        with pytest.raises(ValueError, match="unresolved"):
            assess_recovery(db, "recovery-probe", RecoveryNote)
