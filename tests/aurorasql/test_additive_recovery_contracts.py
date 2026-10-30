"""Alpha 7 Batch C: fail-closed additive migration recovery contracts.

No production connections; all tests use isolated SQLite databases.
"""

import pytest

from aurora.aurorasql import (
    Column, Model, connect, execute_add_column_recorded,
    initialize_additive_history, list_additive_history, migration_plan,
    review_add_column, schema_snapshot,
)


class Before(Model):
    __table__ = "a7_recovery_items"
    id = Column(int, primary_key=True)


class After(Model):
    __table__ = "a7_recovery_items"
    id = Column(int, primary_key=True)
    note = Column(str, nullable=True)


def _case():
    connection = connect("sqlite:///:memory:")
    connection.execute_write('CREATE TABLE "a7_recovery_items" ("id" INTEGER PRIMARY KEY)')
    before = schema_snapshot(Before, dialect="sqlite")
    after = schema_snapshot(After, dialect="sqlite")
    plan = migration_plan(before, after, migration_id="a7_recovery_001")
    approval = review_add_column(connection, before, after, plan)
    initialize_additive_history(connection, confirm=True)
    return connection, before, after, plan, approval


def _execute(case):
    connection, before, after, plan, approval = case
    return execute_add_column_recorded(connection, before, after, plan, approval, confirm=True)


def test_ddl_failure_marks_uncertain_and_blocks_retry(monkeypatch):
    case = _case()
    connection = case[0]
    original = connection.execute_write

    def fail_ddl(sql, parameters=None):
        if sql.startswith("ALTER TABLE"):
            raise RuntimeError("simulated DDL failure")
        return original(sql, parameters)

    monkeypatch.setattr(connection, "execute_write", fail_ddl)
    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        _execute(case)
    assert [entry.status for entry in list_additive_history(connection)] == ["uncertain"]
    with pytest.raises(RuntimeError, match="Unresolved"):
        _execute(case)


def test_audit_update_failure_preserves_started_and_blocks_retry(monkeypatch):
    case = _case()
    connection = case[0]
    original = connection.execute_write

    def fail_audit(sql, parameters=None):
        if sql.startswith("UPDATE") and parameters and parameters.get("status") == "applied":
            raise RuntimeError("simulated audit failure")
        return original(sql, parameters)

    monkeypatch.setattr(connection, "execute_write", fail_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        _execute(case)
    assert [entry.status for entry in list_additive_history(connection)] == ["started"]
    assert connection.fetch_first('SELECT note FROM "a7_recovery_items"') is None
    with pytest.raises(RuntimeError, match="Unresolved"):
        _execute(case)


def test_live_schema_drift_prevents_journal_write():
    case = _case()
    connection = case[0]
    connection.execute_write('ALTER TABLE "a7_recovery_items" ADD COLUMN "unexpected" TEXT')
    with pytest.raises(RuntimeError, match="snapshot|schema|columns"):
        _execute(case)
    assert list_additive_history(connection) == ()


def test_unresolved_previous_migration_blocks_new_operation():
    case = _case()
    connection = case[0]
    connection.execute_write(
        'INSERT INTO "aurorasql_additive_history_v1" '
        '(operation_id, migration_id, table_name, column_name, fingerprint, status) '
        'VALUES (:oid, :mid, :table, :column, :fingerprint, :status)',
        {"oid": "prior", "mid": "prior_migration", "table": "other", "column": "x",
         "fingerprint": "0" * 64, "status": "started"},
    )
    with pytest.raises(RuntimeError, match="Unresolved"):
        _execute(case)
    assert len(list_additive_history(connection)) == 1


def test_success_then_reapply_refused():
    case = _case()
    _execute(case)
    with pytest.raises(RuntimeError, match="already recorded"):
        _execute(case)
    assert [entry.status for entry in list_additive_history(case[0])] == ["applied"]
