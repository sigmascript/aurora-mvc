"""General safety contracts for explicit create-only schema execution."""

import pytest

from aurora.aurorasql import (
    Model, column, connect, execute_create, inspect_table, prepare_create,
)


class Note(Model):
    id = column(int, primary_key=True)
    title = column(str, length=80)


class ChangedNote(Model):
    __table__ = "note"
    id = column(int, primary_key=True)
    title = column(str, length=120)


def test_explicit_create_requires_approval_and_refuses_existing_table():
    with connect("sqlite:///:memory:") as db:
        approval = prepare_create(db, Note)
        assert not inspect_table(db, "note").exists
        with pytest.raises(PermissionError):
            execute_create(db, Note, approval)
        assert not inspect_table(db, "note").exists
        execute_create(db, Note, approval, confirm=True)
        assert inspect_table(db, "note").exists
        with pytest.raises(ValueError):
            prepare_create(db, Note)
        with pytest.raises(RuntimeError):
            execute_create(db, Note, approval, confirm=True)


def test_approval_cannot_authorize_changed_model_or_dialect():
    with connect("sqlite:///:memory:") as db:
        approval = prepare_create(db, Note)
        with pytest.raises(ValueError):
            execute_create(db, ChangedNote, approval, confirm=True)
        assert not inspect_table(db, "note").exists
        with pytest.raises(ValueError):
            execute_create(db, Note, type(approval)(approval.table, "mysql", approval.fingerprint), confirm=True)
        assert not inspect_table(db, "note").exists
