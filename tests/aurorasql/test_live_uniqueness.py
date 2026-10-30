"""Opt-in uniqueness inspection against disposable MySQL and PostgreSQL.

Each test uses only a randomly named table and cleans up that table. Never
points these tests at production or shared application databases.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import Model, column, connect, inspect_table, plan_schema
from aurora.aurorasql.schema import create_table


_BACKENDS = (
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
)


def _test_url(environment):
    if os.environ.get("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Live DDL checks require explicit disposable-database opt-in")
    url = os.environ.get(environment)
    if not url:
        pytest.skip(f"{environment} not configured")
    return url


@pytest.mark.parametrize("dialect,environment", _BACKENDS)
def test_live_model_unique_column_inspection(dialect, environment):
    url = _test_url(environment)
    name = "aurorasql_unique_" + uuid4().hex[:16]
    Probe = type("Probe", (Model,), {
        "__table__": name,
        "id": column(int, primary_key=True),
        "email": column(str, length=120, unique=True),
        "label": column(str, length=80),
    })
    quote = "`" if dialect == "mysql" else '"'
    with connect(url) as db:
        assert db.dialect == dialect
        assert not inspect_table(db, name).exists
        created = False
        try:
            create_table(db, Probe)
            created = True
            observed = inspect_table(db, name)
            assert observed.exists and observed.uniqueness_verified
            assert "email" in observed.unique_columns
            assert "label" not in observed.unique_columns
            assert "id" not in observed.unique_columns
            plan = plan_schema(db, Probe)
            assert not any(change.kind == "unique_mismatch" for change in plan.changes)
        finally:
            if created:
                db.execute_write(f"DROP TABLE {quote}{name}{quote}")


@pytest.mark.parametrize("dialect,environment", _BACKENDS)
def test_live_composite_index_not_single_column_unique(dialect, environment):
    url = _test_url(environment)
    name = "aurorasql_composite_" + uuid4().hex[:16]
    quote = "`" if dialect == "mysql" else '"'
    quoted = f"{quote}{name}{quote}"
    with connect(url) as db:
        assert db.dialect == dialect
        assert not inspect_table(db, name).exists
        created = False
        try:
            db.execute_write(
                f"CREATE TABLE {quoted} ("
                "first_name VARCHAR(60) NOT NULL, "
                "last_name VARCHAR(60) NOT NULL, "
                "UNIQUE (first_name, last_name))"
            )
            created = True
            observed = inspect_table(db, name)
            assert observed.exists and observed.uniqueness_verified
            assert "first_name" not in observed.unique_columns
            assert "last_name" not in observed.unique_columns
            assert observed.indexes
            if dialect == "postgresql":
                # Cross-check the composite index against PostgreSQL metadata.
                composite = db.fetch_first(
                    "SELECT i.indexrelid AS index_oid "
                    "FROM pg_catalog.pg_index i "
                    "WHERE i.indrelid = to_regclass(:name) "
                    "AND i.indisunique AND NOT i.indisprimary "
                    "AND i.indisvalid AND i.indpred IS NULL "
                    "AND i.indexprs IS NULL AND i.indnkeyatts = 2",
                    {"name": name},
                )
                assert composite is not None
        finally:
            if created:
                db.execute_write(f"DROP TABLE {quoted}")
