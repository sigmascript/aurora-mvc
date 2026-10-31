"""Opt-in cross-backend verification of AuroraSQL's existing CRUD contract.

Only disposable databases are permitted. No production or application schema is used.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import Model, column, connect, create_table


@pytest.mark.parametrize("dialect,environment", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_crud_result_shapes_and_generated_keys(dialect, environment):
    if os.getenv("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Requires explicit disposable-database DDL opt-in")
    url = os.getenv(environment)
    if not url:
        pytest.skip(f"{environment} not configured")

    table = f"stage_c_crud_{uuid4().hex[:12]}"
    Entry = type("Entry", (Model,), {
        "__table__": table,
        "id": column(int, primary_key=True),
        "title": column(str, length=80),
        "active": column(bool),
    })

    with connect(url) as db:
        assert db.dialect == dialect
        created_table = False
        try:
            create_table(db, Entry)
            created_table = True
            ops = Entry.using(db)

            first = ops.create(title="First", active=True)
            second = ops.create(title="Second", active=False)
            assert isinstance(first, Entry)
            assert isinstance(second, Entry)
            assert type(first.id) is int and first.id > 0
            assert type(second.id) is int and second.id > first.id

            rows = ops.read(order_by={"id": "ASC"}, as_dicts=True)
            assert rows == [
                {"id": first.id, "title": "First", "active": True},
                {"id": second.id, "title": "Second", "active": False},
            ]
            assert all(type(row["active"]) is bool for row in rows)
            assert ops.read({"id": -1}, as_dicts=True) == []

            assert ops.update(values={"title": "Changed"}, where={"id": first.id}) == 1
            assert ops.update(values={"title": "Missing"}, where={"id": -1}) == 0
            assert ops.read({"id": first.id}, as_dicts=True)[0]["title"] == "Changed"

            with pytest.raises(RuntimeError, match="confirm=True"):
                ops.update(values={"title": "Unsafe"})
            with pytest.raises(RuntimeError, match="confirm=True"):
                ops.delete()

            assert ops.delete(where={"id": second.id}) == 1
            assert ops.delete(where={"id": second.id}) == 0
            assert ops.delete(where={"id": first.id}) == 1
            assert ops.read(as_dicts=True) == []
        finally:
            if created_table:
                quoted = f"`{table}`" if dialect == "mysql" else f'"{table}"'
                db.execute_write(f"DROP TABLE {quoted}")
