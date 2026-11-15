"""Server dialect previews detect bounded type/default drift without DDL."""

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database import planning


class Invoice(Model):
    __table__ = "invoices"
    id = column(int, primary_key=True)
    amount = column("DECIMAL(9,2)")
    label = column(str, length=40)
    active = column(bool)
    note = column(str, nullable=True)


@pytest.mark.parametrize("dialect,decimal,varchar,boolean", [
    ("mysql", "decimal(9,2)", "varchar(40)", "tinyint(1)"),
    ("postgresql", "numeric(9,2)", "character varying(40)", "boolean"),
])
def test_equivalent_declarations_are_detected_as_types_not_conversions(
        monkeypatch, dialect, decimal, varchar, boolean):
    table = TableInfo("invoices", True, (
        ColumnInfo("id", "bigint", False, True),
        ColumnInfo("amount", decimal, False, False),
        ColumnInfo("label", varchar, False, False),
        ColumnInfo("active", boolean, False, False),
        ColumnInfo("note", "text", True, False),
    ), constraints_verified=False, uniqueness_verified=True)
    monkeypatch.setattr(planning, "inspect_table", lambda *_: table)
    connection = type("ReadOnlyConnection", (), {"dialect": dialect})()
    plan = planning.plan_schema(connection, Invoice)
    assert not any(change.kind in ("type_mismatch", "unknown_type") for change in plan.changes)
    assert plan.execution_allowed is False


@pytest.mark.parametrize("dialect,changed_type", [
    ("mysql", "decimal(10,3)"),
    ("postgresql", "numeric(10,3)"),
])
def test_decimal_precision_change_is_reported_read_only(monkeypatch, dialect, changed_type):
    table = TableInfo("invoices", True, (
        ColumnInfo("id", "bigint", False, True),
        ColumnInfo("amount", changed_type, False, False),
        ColumnInfo("label", "varchar(40)" if dialect == "mysql" else "character varying(40)", False, False),
        ColumnInfo("active", "tinyint(1)" if dialect == "mysql" else "boolean", False, False),
        ColumnInfo("note", "text", True, False),
    ), uniqueness_verified=True)
    monkeypatch.setattr(planning, "inspect_table", lambda *_: table)
    connection = type("ReadOnlyConnection", (), {"dialect": dialect})()
    plan = planning.plan_schema(connection, Invoice)
    assert ("type_mismatch", "amount") in {
        (change.kind, change.column) for change in plan.changes
    }
    assert plan.execution_allowed is False
