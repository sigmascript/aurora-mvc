"""E2 accepted nullability semantics and safe literal defaults, with immutable v1 history.

All tests are read-only. Existing nullable= is transitional for existing apps.
"""

from decimal import Decimal
import pytest

from aurora.database import Model, column, create_table_sql, schema_snapshot, compare_snapshots


class E2New(Model):
    __table__ = "e2_semantics"
    id = column(int, primary_key=True)
    title = column(str, not_null=True, default="O'Reilly")
    note = column(str, not_null=False)


class E2Old(Model):
    __table__ = "e2_semantics"
    id = column(int, primary_key=True)
    title = column(str, nullable=False, default="O'Reilly")
    note = column(str, nullable=True)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_new_and_historical_declarations_have_identical_schema_and_ddl(dialect):
    old = schema_snapshot(E2Old, dialect=dialect)
    new = schema_snapshot(E2New, dialect=dialect)
    assert old["format"] == new["format"] == "aurorasql.schema.v1"
    assert compare_snapshots(old, new) == []
    assert create_table_sql(E2Old, dialect) == create_table_sql(E2New, dialect)
    sql = create_table_sql(E2New, dialect)
    assert "DEFAULT 'O''Reilly'" in sql
    assert '"note" TEXT NOT NULL' not in sql
    assert '`note` TEXT NOT NULL' not in sql


def test_default_not_null_and_nullable_runtime_validation():
    assert column(str).not_null is True
    assert column(str).nullable is False
    assert column(str, not_null=False).nullable is True
    column(str, not_null=False).validate(None)
    with pytest.raises(ValueError, match="Non-nullable"):
        column(str).validate(None)


@pytest.mark.parametrize("kwargs", [
    {"not_null": None}, {"not_null": 1}, {"not_null": "false"},
    {"not_null": True, "nullable": False},
    {"not_null": False, "nullable": True},
])
def test_invalid_flags_and_conflicting_declarations_rejected(kwargs):
    with pytest.raises((TypeError, ValueError)):
        column(str, **kwargs)


def test_primary_key_cannot_be_nullable_with_new_or_old_flags():
    with pytest.raises(ValueError, match="Primary-key"):
        column(int, primary_key=True, not_null=False)
    with pytest.raises(ValueError, match="Primary-key"):
        column(int, primary_key=True, nullable=True)


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_nonfinite_float_database_defaults_rejected_early(value):
    with pytest.raises(ValueError, match="finite"):
        column(float, default=value)


@pytest.mark.parametrize("value", [Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN")])
def test_nonfinite_decimal_database_defaults_rejected_early(value):
    with pytest.raises(ValueError, match="finite"):
        column(Decimal, default=value)


def test_dynamic_and_wrong_type_defaults_still_refused():
    with pytest.raises(TypeError, match="Dynamic"):
        column(str, default=lambda: "value")
    with pytest.raises(TypeError, match="Expected"):
        column(int, default="42")
