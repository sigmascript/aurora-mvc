"""Opt-in integration smoke tests. ONLY run on explicitly disposable databases.

Set AURORASQL_TEST_ALLOW_DDL=YES_DISPOSABLE_DB and a backend URL.
Tests create and DROP only their own randomly named table. Never point at production.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import Model, column, connect, create_table, inspect_table, plan_schema


@pytest.mark.parametrize("dialect,environment", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_live_database_workflow(dialect, environment):
    if os.environ.get("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Live DDL checks require explicit disposable-database opt-in")
    url = os.environ.get(environment)
    if not url:
        pytest.skip(f"{environment} not configured")

    table_name = "aurorasql_probe_" + uuid4().hex[:16]
    # The model is generated only to obtain a collision-resistant table name.
    Probe = type("Probe", (Model,), {
        "__table__": table_name,
        "id": column(int, primary_key=True),
        "title": column(str, length=100),
    })
    quoted = f"`{table_name}`" if dialect == "mysql" else f'"{table_name}"'
    with connect(url) as db:
        assert db.dialect == dialect
        created = False
        try:
            create_table(db, Probe)
            created = True
            assert inspect_table(db, table_name).exists
            ops = Probe.using(db)
            with db.transaction():
                row = ops.create(title="alpha")
                assert isinstance(row.id, int)
                assert ops.first(where={"id": row.id}).title == "alpha"
                assert ops.update(values={"title": "beta"}, where={"id": row.id}) == 1
            assert ops.first(where={"id": row.id}).title == "beta"
            with pytest.raises(ValueError):
                with db.transaction():
                    ops.create(title="rollback")
                    raise ValueError("rollback probe")
            assert len(ops.all()) == 1
            assert ops.delete(where={"id": row.id}) == 1
            assert ops.all() == []
            plan = plan_schema(db, Probe)
            assert not plan.execution_allowed
        finally:
            if created:
                # DDL may implicitly commit on MySQL; cleanup is not atomic.
                db.execute_write(f"DROP TABLE {quoted}")
