"""Opt-in, destructive tests against explicitly disposable local databases.

These tests create and drop only uniquely named tables and the dedicated
additive journal; never run against shared or production databases.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import (
    Column, Model, connect, create_table, execute_add_column_recorded,
    initialize_additive_history, list_additive_history, migration_plan,
    review_add_column, schema_snapshot,
)


@pytest.mark.parametrize("dialect,env", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_live_additive_column(dialect, env):
    url = os.getenv(env)
    if not url or os.getenv("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Requires disposable database URL and explicit DDL opt-in")
    suffix = uuid4().hex[:12]
    table = f"aurorasql_add_{suffix}"
    journal = "aurorasql_additive_history_v1"

    class Before(Model):
        __table__ = table
        id = Column(int, primary_key=True)

    class After(Model):
        __table__ = table
        id = Column(int, primary_key=True)
        note = Column(str, nullable=True)

    with connect(url) as conn:
        # Journal is global per database, so never remove a preexisting one.
        from aurora.aurorasql import inspect_table
        if inspect_table(conn, journal).exists:
            pytest.skip("Dedicated journal already exists: use a fresh disposable database")
        journal_created = False
        try:
            create_table(conn, Before)
            before = schema_snapshot(Before, dialect=dialect)
            after = schema_snapshot(After, dialect=dialect)
            plan = migration_plan(before, after, migration_id=f"add_{suffix}")
            approval = review_add_column(conn, before, after, plan)
            initialize_additive_history(conn, confirm=True)
            journal_created = True
            execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
            assert list_additive_history(conn)[0].status == "applied"
            assert "note" in {column.name for column in inspect_table(conn, table).columns}
            with pytest.raises(RuntimeError):
                execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
        finally:
            # Cleanup is best effort and intentionally limited to the test-owned objects.
            from aurora.aurorasql.schema import _quote
            conn.execute_write(f"DROP TABLE IF EXISTS {_quote(table, dialect)}")
            if journal_created:
                conn.execute_write(f"DROP TABLE IF EXISTS {_quote(journal, dialect)}")
