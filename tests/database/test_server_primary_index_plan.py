"""Server primary-key indexes must not prevent read-only schema readiness."""

from types import SimpleNamespace

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database import planning


class Product(Model):
    id = column(int, primary_key=True)
    name = column(str, length=50)


@pytest.mark.parametrize(
    ("dialect", "primary_index"),
    [("mysql", "PRIMARY"), ("postgresql", "product_pkey")],
)
def test_generated_primary_key_index_does_not_require_review(monkeypatch, dialect, primary_index):
    columns = (
        ColumnInfo("id", "bigint", False, True),
        ColumnInfo("name", "varchar(50)", False, False),
    )
    table = TableInfo("product", True, columns, indexes=(primary_index,), uniqueness_verified=True)
    monkeypatch.setattr(planning, "inspect_table", lambda connection, name: table)

    plan = planning.plan_schema(SimpleNamespace(dialect=dialect), Product)

    assert not any("Existing indexes require manual review" in warning for warning in plan.warnings)


@pytest.mark.parametrize(
    ("dialect", "primary_index"),
    [("mysql", "PRIMARY"), ("postgresql", "product_pkey")],
)
def test_extra_index_still_requires_review(monkeypatch, dialect, primary_index):
    columns = (
        ColumnInfo("id", "bigint", False, True),
        ColumnInfo("name", "varchar(50)", False, False),
    )
    table = TableInfo("product", True, columns, indexes=(primary_index, "product_name_idx"), uniqueness_verified=True)
    monkeypatch.setattr(planning, "inspect_table", lambda connection, name: table)

    plan = planning.plan_schema(SimpleNamespace(dialect=dialect), Product)

    assert any("Existing indexes require manual review" in warning for warning in plan.warnings)
