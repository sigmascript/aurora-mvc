"""Server widening proposals are read-only and reject narrowing."""

from types import SimpleNamespace

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database.planning import SchemaChange, SchemaPlan


class WiderName(Model):
    __table__ = "products"
    id = column(int, primary_key=True)
    name = column(str, length=200)


class WiderPrice(Model):
    __table__ = "products"
    id = column(int, primary_key=True)
    price = column("DECIMAL(12,2)")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize("model,field,old_type", [
    (WiderName, "name", "VARCHAR(100)"),
    (WiderPrice, "price", "DECIMAL(10,2)"),
])
def test_read_only_server_widening(monkeypatch, dialect, model, field, old_type):
    from aurora.database import server_column_widening

    monkeypatch.setattr(server_column_widening, "plan_schema", lambda *_: SchemaPlan(
        "products", True, (SchemaChange("type_mismatch", field, "widening"),), ()))
    monkeypatch.setattr(server_column_widening, "inspect_table", lambda *_: TableInfo(
        "products", True, (ColumnInfo("id", "BIGINT", False, True),
                           ColumnInfo(field, old_type, False, False)),
        indexes=("PRIMARY",) if dialect == "mysql" else ("products_pkey",),
        uniqueness_verified=True))
    report = server_column_widening.review_server_column_widening(
        SimpleNamespace(dialect=dialect), [model], "accounting", "database-id")
    assert report.operations[0]["kind"] == "widen_column"
    assert report.operations[0]["from"] != report.operations[0]["to"]
    assert report.statements[0].startswith("ALTER TABLE")
    assert report.execution_allowed is False


@pytest.mark.parametrize("old_type", ["VARCHAR(400)", "TEXT", "INTEGER"])
def test_server_widening_refuses_narrowing_or_type_conversion(monkeypatch, old_type):
    from aurora.database import server_column_widening

    monkeypatch.setattr(server_column_widening, "plan_schema", lambda *_: SchemaPlan(
        "products", True, (SchemaChange("type_mismatch", "name", "changed"),), ()))
    monkeypatch.setattr(server_column_widening, "inspect_table", lambda *_: TableInfo(
        "products", True, (ColumnInfo("id", "BIGINT", False, True),
                           ColumnInfo("name", old_type, False, False)),
        indexes=("PRIMARY",), uniqueness_verified=True))

    with pytest.raises(ValueError, match="narrowing|Only compatible"):
        server_column_widening.review_server_column_widening(
            SimpleNamespace(dialect="mysql"), [WiderName], "default", "database-id")


def test_server_widening_refuses_implicit_default_change(monkeypatch):
    from aurora.database import server_column_widening

    monkeypatch.setattr(server_column_widening, "plan_schema", lambda *_: SchemaPlan(
        "products", True, (SchemaChange("type_mismatch", "name", "widening"),), ()))
    monkeypatch.setattr(server_column_widening, "inspect_table", lambda *_: TableInfo(
        "products", True, (ColumnInfo("id", "BIGINT", False, True),
                           ColumnInfo("name", "VARCHAR(100)", False, False, "draft")),
        indexes=("PRIMARY",), uniqueness_verified=True))

    with pytest.raises(ValueError, match="constrained"):
        server_column_widening.review_server_column_widening(
            SimpleNamespace(dialect="mysql"), [WiderName], "default", "database-id")
