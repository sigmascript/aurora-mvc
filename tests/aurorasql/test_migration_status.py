"""Small operator-facing tests for the read-only migration status API."""

import pytest

from aurora.aurorasql import (
    Model, column, connect, initialize_history, migration_status,
    execute_create_recorded, prepare_create,
)


class StatusNote(Model):
    id = column(int, primary_key=True)
    title = column(str, length=40)


def test_migration_status_reports_empty_and_applied_history():
    with connect("sqlite:///:memory:") as db:
        with pytest.raises(RuntimeError, match="missing"):
            migration_status(db)
        initialize_history(db, confirm=True)
        initial = migration_status(db)
        assert (initial.total, initial.applied, initial.unresolved) == (0, 0, 0)
        assert initial.ready_for_new_create and not initial.findings
        approval = prepare_create(db, StatusNote)
        execute_create_recorded(db, StatusNote, approval, confirm=True)
        status = migration_status(db)
        assert (status.total, status.applied, status.unresolved) == (1, 1, 0)
        assert status.ready_for_new_create


def test_migration_status_blocks_uncertain_history():
    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        db.execute_write(
            'INSERT INTO "aurorasql_schema_history" '
            '(operation_id, table_name, dialect, fingerprint, status) '
            'VALUES (:id, :table, :dialect, :fingerprint, :status)',
            {"id": "uncertain-test", "table": "status_note", "dialect": "sqlite",
             "fingerprint": "0" * 64, "status": "uncertain"},
        )
        status = migration_status(db)
        assert status.total == 1 and status.unresolved == 1
        assert not status.ready_for_new_create
        assert any(f.kind == "unresolved" for f in status.findings)
