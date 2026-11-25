"""Create-only approval ownership stays guarded and keeps public imports stable."""

from hashlib import sha256
from types import SimpleNamespace

import pytest

from aurora.database import CreateApproval as public_approval
from aurora.database import execute_create as public_execute
from aurora.database import migration_history, table_operations
from aurora.database import prepare_create as public_prepare


def test_create_approval_and_execution_are_owned_by_table_operations():
    assert public_approval is migration_history.CreateApproval is table_operations.CreateApproval
    assert public_prepare is migration_history.prepare_create is table_operations.prepare_create
    assert public_execute is migration_history.execute_create is table_operations.execute_create


def test_create_only_review_refuses_existing_table_and_other_plans(monkeypatch):
    connection = SimpleNamespace(dialect="sqlite")
    model = SimpleNamespace(__table_name__="items")
    plan = SimpleNamespace(
        exists=False,
        changes=[SimpleNamespace(kind="create_table")],
        table="items",
        create_sql_preview="CREATE TABLE items (id INTEGER)",
    )
    monkeypatch.setattr(table_operations, "plan_schema", lambda *_: plan)
    result = table_operations.prepare_create(connection, model)
    assert result == table_operations.CreateApproval(
        "items", "sqlite", sha256(plan.create_sql_preview.encode()).hexdigest()
    )
    plan.exists = True
    with pytest.raises(ValueError, match="Only missing-table creation"):
        table_operations.prepare_create(connection, model)


def test_guarded_create_requires_confirmation_and_exact_schema(monkeypatch):
    writes = []
    connection = SimpleNamespace(dialect="sqlite", execute_write=lambda sql: writes.append(sql))
    model = SimpleNamespace(__table_name__="items")
    ddl = 'CREATE TABLE "items" ("id" INTEGER)'
    monkeypatch.setattr(table_operations, "create_table_sql", lambda *_: ddl)
    monkeypatch.setattr(table_operations, "inspect_table", lambda *_: SimpleNamespace(exists=False))
    approval = table_operations.CreateApproval("items", "sqlite", sha256(ddl.encode()).hexdigest())
    with pytest.raises(PermissionError, match="confirm=True"):
        table_operations.execute_create(connection, model, approval)
    with pytest.raises(ValueError, match="Approval does not match"):
        table_operations.execute_create(
            connection,
            model,
            table_operations.CreateApproval("items", "sqlite", "0" * 64),
            confirm=True,
        )
    assert writes == []
    assert table_operations.execute_create(connection, model, approval, confirm=True) is None
    assert writes == [ddl]
