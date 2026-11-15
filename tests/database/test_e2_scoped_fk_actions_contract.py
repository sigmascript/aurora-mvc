"""E2: selected-model FK resolution, action DDL and immutable v1 history."""
import sqlite3

import pytest

from aurora.database.models import Model, column
from aurora.database.schema import create_table_sql
from aurora.database.init_validation import validate_init_models
from aurora.database.snapshots import schema_snapshot, compare_snapshots, _snapshot_tables
from aurora.database.migration_format import snapshot_fingerprint


class E2Customer(Model):
    __table__ = "e2_customers"
    id = column(int, primary_key=True)


class E2Order(Model):
    __table__ = "e2_orders"
    id = column(int, primary_key=True)
    customer_id = column(int, reference="E2Customer.id", on_delete="CASCADE")


class E2NullableNote(Model):
    __table__ = "e2_notes"
    id = column(int, primary_key=True)
    customer_id = column(int, not_null=False, reference="e2_customers.id", on_delete="SET NULL")


def test_scoped_sql_and_snapshot_resolve_physical_table():
    models = (E2Customer, E2Order)
    assert validate_init_models(models, allow_extended=True) == models
    for dialect in ("sqlite", "mysql", "postgresql"):
        ddl = create_table_sql(E2Order, dialect, models=models)
        assert "e2_customers" in ddl
        assert "E2Customer" not in ddl
        assert "ON UPDATE NO ACTION ON DELETE CASCADE" in ddl
        snapshot = schema_snapshot(*models, dialect=dialect, allow_v2=True)
        assert snapshot["format"] == "aurorasql.schema.v2"
        assert compare_snapshots(snapshot, snapshot) == []
        assert len(snapshot_fingerprint(snapshot)) == 64
        col = next(c for t in snapshot["tables"] if t["name"] == "e2_orders"
                   for c in t["columns"] if c["name"] == "customer_id")
        assert (col["references"], col["on_update"], col["on_delete"]) == (
            "e2_customers.id", "NO ACTION", "CASCADE")


def test_existing_migration_paths_fail_closed():
    with pytest.raises(ValueError):
        create_table_sql(E2Order, "sqlite")
    with pytest.raises(ValueError, match="migration/init"):
        validate_init_models((E2Customer, E2Order))
    with pytest.raises(ValueError, match="v2"):
        schema_snapshot(E2Customer, E2Order, dialect="sqlite")
    v2 = schema_snapshot(E2Customer, E2Order, dialect="sqlite", allow_v2=True)
    with pytest.raises(ValueError, match="Unsupported"):
        _snapshot_tables(v2)


def test_real_disposable_sqlite_cascade_and_set_null():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        models = (E2Customer, E2Order, E2NullableNote)
        for model in validate_init_models(models, allow_extended=True):
            conn.execute(create_table_sql(model, "sqlite", models=models))
        conn.execute("INSERT INTO e2_customers(id) VALUES (1)")
        conn.execute("INSERT INTO e2_orders(id, customer_id) VALUES (11, 1)")
        conn.execute("INSERT INTO e2_notes(id, customer_id) VALUES (21, 1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO e2_orders(id, customer_id) VALUES (12, 999)")
        conn.execute("DELETE FROM e2_customers WHERE id = 1")
        assert conn.execute("SELECT COUNT(*) FROM e2_orders").fetchone()[0] == 0
        assert conn.execute("SELECT customer_id FROM e2_notes").fetchone()[0] is None
    finally:
        conn.close()


def test_missing_ambiguous_and_cross_database_models_fail():
    with pytest.raises(ValueError, match="Unknown model-qualified"):
        create_table_sql(E2Order, "sqlite", models=(E2Order,))
    with pytest.raises(ValueError, match="outside selected"):
        create_table_sql(E2NullableNote, "sqlite", models=(E2NullableNote,))
    with pytest.raises(ValueError, match="Ambiguous"):
        create_table_sql(E2Order, "sqlite", models=(E2Customer, E2Customer, E2Order))


@pytest.mark.parametrize("action", ("CASCADE; DROP TABLE x", "ENABLE", "SET", [], 3))
def test_unsafe_actions_rejected(action):
    with pytest.raises((TypeError, ValueError)):
        column(int, reference="e2_customers.id", on_delete=action)


def test_set_null_and_set_default_rejected_when_unsupported():
    with pytest.raises(ValueError, match="nullable"):
        column(int, reference="e2_customers.id", on_delete="SET NULL")
    with pytest.raises(ValueError, match="default"):
        column(int, reference="e2_customers.id", on_delete="SET DEFAULT")
    class DefaultAction(Model):
        __table__ = "e2_default_action"
        customer_id = column(int, reference="e2_customers.id", on_delete="SET DEFAULT", default=1)
    with pytest.raises(ValueError, match="MySQL"):
        create_table_sql(DefaultAction, "mysql", models=(DefaultAction, E2Customer))


def test_historical_v1_remains_valid_and_cross_version_refuses():
    class Legacy(Model):
        __table__ = "e2_legacy"
        id = column(int, primary_key=True)
        customer_id = column(int, reference="e2_customers.id")
    v1 = schema_snapshot(Legacy, dialect="sqlite")
    assert v1["format"] == "aurorasql.schema.v1"
    fingerprint = snapshot_fingerprint(v1)
    assert fingerprint == snapshot_fingerprint(v1)
    v2 = schema_snapshot(E2Customer, E2Order, dialect="sqlite", allow_v2=True)
    with pytest.raises(ValueError, match="Cross-version"):
        compare_snapshots(v1, v2)
    broken = {**v2, "tables": [dict(t) for t in v2["tables"]]}
    for t in broken["tables"]:
        t["columns"] = [dict(c) for c in t["columns"]]
    broken["tables"][1]["columns"][0]["on_delete"] = "DROP TABLE"
    with pytest.raises(ValueError):
        snapshot_fingerprint(broken)


def test_public_create_only_aurorasql_flow():
    from aurora.database import connect, create_tables

    with connect("sqlite:///:memory:") as db:
        db.execute_write("PRAGMA foreign_keys=ON")
        create_tables(db, E2Customer, E2Order)
        db.execute_write("INSERT INTO e2_customers(id) VALUES (1)")
        db.execute_write("INSERT INTO e2_orders(id,customer_id) VALUES (11,1)")
        db.execute_write("DELETE FROM e2_customers WHERE id=1")
        assert db.fetch_all("SELECT id FROM e2_orders") == []


def test_readonly_sqlite_planner_compares_declared_actions():
    from aurora.database import connect
    from aurora.database.planning import plan_schema

    with connect("sqlite:///:memory:") as db:
        db.execute_write("PRAGMA foreign_keys=ON")
        models = (E2Customer, E2Order)
        for model in models:
            db.execute_write(create_table_sql(model, "sqlite", models=models))
        plan = plan_schema(db, E2Order, models=models)
        assert not any(c.kind == "foreign_key_action_mismatch" for c in plan.changes)
        with pytest.raises(ValueError, match="selected model set"):
            plan_schema(db, E2Order)


def test_legacy_live_evidence_readers_reject_v2_before_inspection():
    from aurora.database.live_schema_evidence import inspect_reviewed_baseline
    from aurora.database.strict_schema_evidence import assess_strict_schema

    class NoIO:
        dialect = "sqlite"

    v2 = schema_snapshot(E2Customer, E2Order, dialect="sqlite", allow_v2=True)
    with pytest.raises(ValueError, match="Unsupported"):
        inspect_reviewed_baseline(NoIO(), v2)
    with pytest.raises(ValueError, match="Unsupported"):
        assess_strict_schema(NoIO(), v2)
