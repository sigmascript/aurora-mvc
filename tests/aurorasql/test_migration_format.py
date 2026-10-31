"""Alpha 7 read-only migration format contract."""
import json

import pytest

from aurora.aurorasql import (Model, column, schema_snapshot, migration_plan,
                             migration_plan_json, validate_migration_plan,
                             snapshot_fingerprint)


class Old(Model):
    __table__ = "people"
    id = column(int, primary_key=True)
    name = column(str, length=80)


class New(Model):
    __table__ = "people"
    id = column(int, primary_key=True)
    name = column(str, length=80)
    age = column(int, nullable=True)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_deterministic_read_only_plan(dialect):
    before = schema_snapshot(Old, dialect=dialect)
    after = schema_snapshot(New, dialect=dialect)
    plan = migration_plan(before, after, migration_id="m001_add_age")
    assert plan["execution_allowed"] is False
    assert plan["operations"] == [{"kind": "added_column", "table": "people",
                                   "column": "age", "risk": "additive", "requires_review": True}]
    assert json.loads(migration_plan_json(plan)) == plan
    assert migration_plan_json(plan) == migration_plan_json(
        migration_plan(before, after, migration_id="m001_add_age"))


def test_preview_does_not_change_fingerprint():
    snap = schema_snapshot(Old, dialect="sqlite")
    changed = json.loads(json.dumps(snap))
    changed["tables"][0]["create_sql_preview"] = "DO NOT EXECUTE"
    assert snapshot_fingerprint(snap) == snapshot_fingerprint(changed)


def test_destructive_change_requires_review():
    before = schema_snapshot(New, dialect="sqlite")
    after = schema_snapshot(Old, dialect="sqlite")
    plan = migration_plan(before, after, migration_id="m002_remove_age")
    assert plan["operations"][0]["risk"] == "potential_data_loss"
    assert plan["execution_allowed"] is False


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(execution_allowed=True),
    lambda p: p["operations"][0].update(requires_review=False),
    lambda p: p["operations"][0].update(kind="execute_sql"),
    lambda p: p.update(extra="unexpected"),
])
def test_rejects_unsafe_or_unknown_plans(mutation):
    plan = migration_plan(schema_snapshot(Old, dialect="sqlite"),
                          schema_snapshot(New, dialect="sqlite"), migration_id="m003")
    mutation(plan)
    with pytest.raises(ValueError):
        validate_migration_plan(plan)


def test_invalid_id_and_dialect_mismatch():
    before = schema_snapshot(Old, dialect="sqlite")
    after = schema_snapshot(New, dialect="mysql")
    with pytest.raises(ValueError):
        migration_plan(before, after, migration_id="m004")
    with pytest.raises(ValueError):
        migration_plan(before, before, migration_id="../../unsafe")
