"""Durable create-only audit history and refusal behavior."""

import pytest

from aurora.aurorasql import (
    Model, column, connect, execute_create_recorded, initialize_history,
    inspect_table, list_history, prepare_create,
)


class JournalNote(Model):
    id = column(int, primary_key=True)
    title = column(str, length=80)


def test_recorded_create_survives_reconnect(tmp_path):
    url = "sqlite:///" + str(tmp_path / "history.sqlite3").replace("\\", "/")
    with connect(url) as db:
        with pytest.raises(PermissionError):
            initialize_history(db)
        initialize_history(db, confirm=True)
        approval = prepare_create(db, JournalNote)
        with pytest.raises(PermissionError):
            execute_create_recorded(db, JournalNote, approval)
        operation_id = execute_create_recorded(db, JournalNote, approval, confirm=True)
        assert inspect_table(db, "journal_note").exists
        assert len(list_history(db)) == 1
    with connect(url) as db:
        entry, = list_history(db)
        assert entry.operation_id == operation_id
        assert entry.status == "applied"
        assert entry.table == "journal_note"
        with pytest.raises(RuntimeError):
            initialize_history(db, confirm=True)
        with pytest.raises(RuntimeError):
            execute_create_recorded(db, JournalNote, approval, confirm=True)


def test_missing_history_and_unresolved_status_refuse_create():
    with connect("sqlite:///:memory:") as db:
        approval = prepare_create(db, JournalNote)
        with pytest.raises(RuntimeError):
            list_history(db)
        with pytest.raises(RuntimeError):
            execute_create_recorded(db, JournalNote, approval, confirm=True)
        initialize_history(db, confirm=True)
        db.execute_write(
            'INSERT INTO "aurorasql_schema_history" '
            '(operation_id, table_name, dialect, fingerprint, status) '
            'VALUES (:id, :name, :dialect, :fingerprint, :status)',
            {"id": "manual-test", "name": "journal_note", "dialect": "sqlite",
             "fingerprint": approval.fingerprint, "status": "uncertain"},
        )
        with pytest.raises(RuntimeError, match="Unresolved"):
            execute_create_recorded(db, JournalNote, approval, confirm=True)
        assert not inspect_table(db, "journal_note").exists


def test_failed_ddl_is_marked_uncertain(monkeypatch):
    with connect("sqlite:///:memory:") as db:
        initialize_history(db, confirm=True)
        approval = prepare_create(db, JournalNote)

        def simulate_uncertain_failure(*args, **kwargs):
            raise OSError("connection lost during DDL")

        monkeypatch.setattr("aurora.aurorasql.history.execute_create", simulate_uncertain_failure)
        with pytest.raises(OSError, match="connection lost"):
            execute_create_recorded(db, JournalNote, approval, confirm=True)
        entry, = list_history(db)
        assert entry.status == "uncertain"
        with pytest.raises(RuntimeError, match="Unresolved"):
            execute_create_recorded(db, JournalNote, approval, confirm=True)
