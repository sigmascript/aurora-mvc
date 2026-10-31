"""Alpha 7 integration: sequential additive migrations and data preservation.

Live backends are opt-in and MUST be disposable. No production connections.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import (
    Column, Model, connect, create_table, execute_add_column_recorded,
    initialize_additive_history, inspect_table, list_additive_history,
    migration_plan, review_add_column, schema_snapshot,
)
from aurora.aurorasql.schema import _quote


JOURNAL = "aurorasql_additive_history_v1"


def _models(table):
    class Original(Model):
        __table__ = table
        id = Column(int, primary_key=True)

    class WithTitle(Model):
        __table__ = table
        id = Column(int, primary_key=True)
        title = Column(str, nullable=True)

    class WithNotes(Model):
        __table__ = table
        id = Column(int, primary_key=True)
        title = Column(str, nullable=True)
        notes = Column(str, nullable=True)

    return Original, WithTitle, WithNotes


def _migrate(conn, source, target, migration_id, dialect):
    before = schema_snapshot(source, dialect=dialect)
    after = schema_snapshot(target, dialect=dialect)
    plan = migration_plan(before, after, migration_id=migration_id)
    approval = review_add_column(conn, before, after, plan)
    return execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def _exercise(conn, dialect, table, suffix):
    original, titled, noted = _models(table)
    create_table(conn, original)
    quoted = _quote(table, dialect)
    conn.execute_write(f"INSERT INTO {quoted} (id) VALUES (1)")
    initialize_additive_history(conn, confirm=True)
    _migrate(conn, original, titled, f"a7_title_{suffix}", dialect)
    conn.execute_write(f"UPDATE {quoted} SET title=:title WHERE id=1", {"title": "preserved"})
    _migrate(conn, titled, noted, f"a7_notes_{suffix}", dialect)
    row = conn.fetch_first(f"SELECT id, title, notes FROM {quoted} WHERE id=1")
    assert row["id"] == 1
    assert row["title"] == "preserved"
    assert row["notes"] is None
    assert {column.name for column in inspect_table(conn, table).columns} == {
        "id", "title", "notes"
    }
    entries = list_additive_history(conn)
    assert len(entries) == 2
    assert {entry.status for entry in entries} == {"applied"}
    assert {entry.migration_id for entry in entries} == {
        f"a7_title_{suffix}", f"a7_notes_{suffix}"
    }
    before = schema_snapshot(original, dialect=dialect)
    after = schema_snapshot(titled, dialect=dialect)
    plan = migration_plan(before, after, migration_id=f"a7_title_{suffix}")
    # Reusing an already reviewed operation must be refused by the journal,
    # even if the source schema no longer matches the live table.
    from aurora.aurorasql import AddColumnReview
    approval = AddColumnReview(plan["id"], dialect, table, "title", "0" * 64, "", False)
    with pytest.raises(RuntimeError, match="already recorded"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
    assert len(list_additive_history(conn)) == 2


def test_sqlite_two_sequential_aio_style_changes_preserve_data():
    with connect("sqlite:///:memory:") as conn:
        _exercise(conn, "sqlite", "a7_integration_items", "sqlite")


@pytest.mark.parametrize("dialect,env", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_live_two_sequential_aio_style_changes_preserve_data(dialect, env):
    url = os.getenv(env)
    if not url or os.getenv("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Requires disposable database URL and explicit DDL opt-in")
    suffix = uuid4().hex[:12]
    table = f"a7_integration_{suffix}"
    with connect(url) as conn:
        # The journal is shared across tests. Never modify an existing journal.
        if inspect_table(conn, JOURNAL).exists:
            pytest.skip("Additive journal exists: use a fresh disposable database")
        created_journal = False
        try:
            # Mark test-owned journal before exercising; clean only what we own.
            # _exercise creates the journal after creating its test table.
            _exercise(conn, dialect, table, suffix)
            created_journal = True
        finally:
            conn.execute_write(f"DROP TABLE IF EXISTS {_quote(table, dialect)}")
            # If _exercise failed after creating the journal, it is intentionally
            # preserved as recovery evidence rather than silently deleted.
            if created_journal:
                conn.execute_write(f"DROP TABLE IF EXISTS {_quote(JOURNAL, dialect)}")
