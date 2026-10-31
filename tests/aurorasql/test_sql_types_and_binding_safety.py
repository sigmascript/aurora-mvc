"""Explicit SQL types and fail-closed named-binding checks."""

from decimal import Decimal

import pytest

from aurora.aurorasql import Model, column, connect
from aurora.aurorasql.core import _compile_sql
from aurora.aurorasql.schema import create_table_sql
from aurora.aurorasql.snapshots import compare_snapshots, schema_snapshot


class Invoice(Model):
    id = column(int, primary_key=True)
    title = column("VARCHAR(80)")
    note = column("TEXT", nullable=True)
    amount = column("DECIMAL(12,2)")


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_explicit_sql_type_ddl_and_snapshots(dialect):
    ddl = create_table_sql(Invoice, dialect)
    assert "VARCHAR(80)" in ddl
    assert "DECIMAL(12,2)" in ddl
    snap = schema_snapshot(Invoice, dialect=dialect)
    fields = {item["name"]: item for item in snap["tables"][0]["columns"]}
    assert fields["amount"]["type"] == "sql:DECIMAL(12,2)"
    assert compare_snapshots(snap, snap) == []


@pytest.mark.parametrize("declaration", [
    "VARCHAR(0)", "DECIMAL(12,13)", "DECIMAL(70,2)",
    "TEXT); DROP TABLE users; --", "BLOB", "VARCHAR(999999)",
])
def test_unsafe_or_unsupported_sql_types_rejected(declaration):
    with pytest.raises((ValueError, TypeError)):
        column(declaration)


def test_decimal_precision_scale_and_string_length():
    field = column("DECIMAL(5,2)")
    field.validate(Decimal("123.45"))
    for value in [Decimal("1234.56"), Decimal("1.234"), Decimal("NaN")]:
        with pytest.raises(ValueError):
            field.validate(value)
    with pytest.raises(ValueError):
        column("VARCHAR(3)").validate("abcd")


def test_sqlite_roundtrip_and_read_normalization():
    with connect("sqlite:///:memory:") as db:
        db.execute_write(create_table_sql(Invoice, "sqlite"))
        with Invoice.connection_scope(db):
            item = Invoice.create(title="A", amount=Decimal("12.50"))
            assert item.id is not None
            rows = Invoice.read(where={"id": item.id})
            assert rows[0]["amount"] == Decimal("12.50")


def test_unused_and_missing_parameters_fail_closed():
    for sql, values in [
        ("SELECT :id", {}),
        ("SELECT 1", {"id": 1}),
        ("SELECT ':not_a_placeholder'", {"id": 1}),
    ]:
        with pytest.raises(ValueError):
            _compile_sql(sql, values, "named")
    assert _compile_sql("SELECT :id", {"id": "1 OR 1=1"}, "named")[1] == {"id": "1 OR 1=1"}
