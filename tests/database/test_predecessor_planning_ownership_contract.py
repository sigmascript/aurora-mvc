"""Historical predecessor model reconstruction stays read-only and compatible."""

import pytest

from aurora.database import migration_history, schema_planning
from aurora.database.models import Model, column


class Original(Model):
    __table__ = "historical_items"
    id = column(int, primary_key=True)
    name = column(str, length=80, not_null=False)


def test_historical_predecessor_builders_have_planning_owner_and_legacy_identity():
    for name in (
        "column_addition_predecessors",
        "default_addition_predecessor_models",
        "nullability_predecessor_models",
        "widening_predecessor_models",
    ):
        owned = getattr(schema_planning, name)
        assert getattr(migration_history, name) is owned
        assert owned.__module__ == schema_planning.__name__


def test_add_column_predecessor_does_not_mutate_source_model():
    before = dict(Original.__columns__)
    predecessor = schema_planning.column_addition_predecessors(
        [Original], {"kind": "add_column", "table": "historical_items", "column": "name"}
    )
    assert predecessor[0].__columns__.keys() == {"id"}
    assert Original.__columns__ == before


def test_predecessor_rejects_ambiguous_target_without_source_mutation():
    before = dict(Original.__columns__)
    with pytest.raises(RuntimeError, match="ambiguous"):
        schema_planning.column_addition_predecessors(
            [Original, Original],
            {"kind": "add_column", "table": "historical_items", "column": "name"},
        )
    assert Original.__columns__ == before


def test_default_predecessor_rejects_unreviewed_operation():
    with pytest.raises(ValueError, match="reviewed"):
        schema_planning.default_addition_predecessor_models(
            [Original], {"kind": "remove_default", "table": "historical_items", "column": "name"}
        )
