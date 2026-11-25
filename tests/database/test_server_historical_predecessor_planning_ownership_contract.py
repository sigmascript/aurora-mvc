"""The native server predecessor model builder is pure planning, not history I/O."""

import pytest

from aurora.database import migration_history, schema_planning
from aurora.database.models import Model, column


def test_predecessor_helpers_have_one_owner():
    for name in ("_historical_column", "predecessor_models", "_compatible_widening"):
        assert getattr(migration_history, name) is getattr(schema_planning, name)


def test_add_column_predecessor_is_temporary_and_source_unchanged():
    class Example(Model):
        __table__ = "hist_example"
        id = column(int, primary_key=True)
        title = column(str, nullable=True)

    source_fields = dict(Example.__columns__)
    previous = schema_planning.predecessor_models(
        (Example,), {"kind": "add_column", "table": "hist_example", "column": "title"}
    )
    assert "title" not in previous[0].__columns__
    assert Example.__columns__ == source_fields
    assert previous[0] is not Example


def test_ambiguous_or_dependent_predecessor_fails_closed():
    class Example(Model):
        __table__ = "hist_example_ambiguous"
        id = column(int, primary_key=True)
        title = column(str, nullable=True)

    operation = {"kind": "add_column", "table": "hist_example_ambiguous", "column": "title"}
    with pytest.raises(ValueError, match="Ambiguous"):
        schema_planning.predecessor_models((Example, Example), operation)
    with pytest.raises(ValueError, match="Dependent"):
        schema_planning.predecessor_models((Example,), {**operation, "column": "id"})


def test_widening_compatibility_unchanged_for_integer_and_decimal_cases():
    compatible = schema_planning._compatible_widening
    assert compatible("varchar(20)", "varchar(40)")
    assert compatible("decimal(8,2)", "decimal(10,3)")
    assert not compatible("varchar(40)", "varchar(20)")
    assert not compatible("decimal(8,2)", "decimal(8,2)")
