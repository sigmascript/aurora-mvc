"""Opt-in live MySQL/PostgreSQL Batch C checks on disposable databases only.

These tests are skipped unless BOTH a disposable-database acknowledgement and
an explicit backend URL are provided. They create/drop only a random table.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import Model, column, connect, create_table, schema_snapshot


@pytest.mark.parametrize("dialect,env", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_live_batch_c_filter_transaction_and_schema(dialect, env):
    if os.environ.get("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Requires explicit disposable-database opt-in")
    url = os.environ.get(env)
    if not url:
        pytest.skip(f"{env} not configured")

    table = "aurorasql_c_" + uuid4().hex[:16]
    Probe = type("BatchCProbe", (Model,), {
        "__table__": table,
        "id": column(int, primary_key=True),
        "name": column(str, length=80),
        "score": column(int),
    })
    quoted = f"`{table}`" if dialect == "mysql" else f'"{table}"'
    with connect(url) as db:
        created = False
        try:
            create_table(db, Probe)
            created = True
            ops = Probe.using(db)
            with db.transaction():
                a = ops.create(name="alpha", score=10)
                b = ops.create(name="beta", score=20)
                c = ops.create(name="gamma", score=30)
            query = {"score--ge": 10, "$or": [{"id": a.id}, {"name": "beta"}]}
            assert {item.name for item in ops.legacy_query(query).all()} == {"alpha", "beta"}
            assert ops.update(values={"score": 25}, where={"id": b.id}) == 1
            assert ops.first(where={"id": b.id}).score == 25
            with pytest.raises(RuntimeError, match="intentional rollback"):
                with db.transaction():
                    ops.delete(where={"id": c.id})
                    raise RuntimeError("intentional rollback")
            assert ops.first(where={"id": c.id}) is not None
            assert len(ops.legacy_query({"score--between": [10, 30]}).all()) == 3
            snapshot = schema_snapshot(Probe, dialect=dialect)
            assert snapshot is not None
            with pytest.raises((TypeError, ValueError)):
                ops.delete(where={"$or": []})
            assert len(ops.all()) == 3
        finally:
            if created:
                # MySQL DDL is not transactional; cleanup may itself fail.
                db.execute_write(f"DROP TABLE {quoted}")
