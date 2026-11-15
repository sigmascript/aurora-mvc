"""E2 singular physical-table references preserve canonical v1 SQL evidence.

No connection or DDL execution. Model-name lookup and FK actions are NOT in
this batch: they need database-scoped resolution and versioned snapshots.
"""

import pytest

from aurora.database import Model, column, create_table_sql, schema_snapshot, compare_snapshots


class Parent(Model):
    __table__ = "e2_parent"
    id = column(int, primary_key=True)


class NewChild(Model):
    __table__ = "e2_child"
    id = column(int, primary_key=True)
    parent_id = column(int, reference="e2_parent.id")


class OldChild(Model):
    __table__ = "e2_child"
    id = column(int, primary_key=True)
    parent_id = column(int, references="e2_parent.id")


@pytest.mark.parametrize("dialect", ("sqlite", "mysql", "postgresql"))
def test_singular_physical_reference_matches_historical_sql_and_snapshot(dialect):
    new = schema_snapshot(Parent, NewChild, dialect=dialect)
    old = schema_snapshot(Parent, OldChild, dialect=dialect)
    assert new == old
    assert new["format"] == "aurorasql.schema.v1"
    assert compare_snapshots(old, new) == []
    assert create_table_sql(NewChild, dialect) == create_table_sql(OldChild, dialect)
    assert 'REFERENCES' in create_table_sql(NewChild, dialect)
    assert NewChild.__columns__["parent_id"].references == "e2_parent.id"


@pytest.mark.parametrize("bad", ([], ["first.id", "second.id"], "a", "a.b.c", ".id", "a.", "a;DROP.id", 12, {}))
def test_invalid_singular_targets_rejected_before_sql(bad):
    with pytest.raises((TypeError, ValueError)):
        column(int, reference=bad)


def test_unresolved_model_qualified_reference_fails_closed():
    class Child(Model):
        __table__ = "child_for_resolution"
        parent_id = column(int, reference="Customer.id")

    with pytest.raises(ValueError, match="selected model set"):
        create_table_sql(Child, "sqlite")


def test_conflicting_spellings_refused():
    with pytest.raises(ValueError, match="cannot be combined"):
        column(int, reference="e2_parent.id", references="e2_parent.id")


def test_action_options_require_explicit_scoped_ddl():
    class Child(Model):
        __table__ = "child_actions"
        parent_id = column(int, reference="e2_parent.id", on_delete="CASCADE")

    with pytest.raises(ValueError, match="selected model set"):
        create_table_sql(Child, "sqlite")
    with pytest.raises(ValueError, match="v2 snapshot"):
        schema_snapshot(Child, dialect="sqlite")
