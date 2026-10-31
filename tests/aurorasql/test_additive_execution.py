"""Fail-closed SQLite additive migration execution tests."""

import pytest

from aurora.aurorasql import (
    Column, Model, connect, migration_plan, schema_snapshot,
    initialize_additive_history, list_additive_history, execute_add_column_recorded,
    review_add_column,
)


class Before(Model):
    __table__ = "b2b_items"
    id = Column(int, primary_key=True)


class After(Model):
    __table__ = "b2b_items"
    id = Column(int, primary_key=True)
    note = Column(str, nullable=True)


def setup():
    conn = connect("sqlite:///:memory:")
    conn.execute_write('CREATE TABLE "b2b_items" ("id" INTEGER PRIMARY KEY)')
    conn.execute_write('INSERT INTO "b2b_items" (id) VALUES (1)')
    before = schema_snapshot(Before, dialect="sqlite")
    after = schema_snapshot(After, dialect="sqlite")
    plan = migration_plan(before, after, migration_id="m001_note")
    approval = review_add_column(conn, before, after, plan)
    return conn, before, after, plan, approval


def test_explicit_journal_and_execution():
    conn, before, after, plan, approval = setup()
    with pytest.raises(RuntimeError):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
    with pytest.raises(PermissionError):
        initialize_additive_history(conn)
    initialize_additive_history(conn, confirm=True)
    with pytest.raises(PermissionError):
        execute_add_column_recorded(conn, before, after, plan, approval)
    op_id = execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
    assert len(op_id) == 36
    assert conn.fetch_first('SELECT note FROM "b2b_items" WHERE id=1')["note"] is None
    assert list_additive_history(conn)[0].status == "applied"
    with pytest.raises(RuntimeError):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_drift_and_approval_rejected_before_journal():
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('ALTER TABLE "b2b_items" ADD COLUMN "other" INTEGER')
    with pytest.raises(RuntimeError):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
    assert list_additive_history(conn) == ()


def test_type_drift_rejected():
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('DROP TABLE "b2b_items"')
    conn.execute_write('CREATE TABLE "b2b_items" ("id" TEXT PRIMARY KEY)')
    with pytest.raises(RuntimeError, match="type|Primary|Indexed"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
    assert list_additive_history(conn) == ()


def test_unresolved_journal_blocks_new_ddl():
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('INSERT INTO "aurorasql_additive_history_v1" VALUES '
                       "('id1','previous','b2b_items','old','fingerprint','uncertain')")
    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_rejects_modified_approval():
    from dataclasses import replace
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    with pytest.raises(ValueError):
        execute_add_column_recorded(conn, before, after, plan,
                                    replace(approval, sql_preview='DROP TABLE b2b_items'), confirm=True)
    assert list_additive_history(conn) == ()
