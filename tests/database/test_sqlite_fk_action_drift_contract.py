"""Read-only SQLite FK action drift detection before public action support."""

import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.schema_inspection import inspect_table
from aurora.database.schema_planning import plan_schema


class Child(Model):
    __table__ = "e2_action_children"
    id = column(int, primary_key=True)
    parent_id = column(int, reference="e2_action_parents.id")


class SQLiteReadOnly:
    dialect = "sqlite"

    def __init__(self, connection):
        self.connection = connection

    def fetch_all(self, sql, params=None):
        return self.connection.execute(sql, params or {}).fetchall()

    def fetch_first(self, sql, params=None):
        return self.connection.execute(sql, params or {}).fetchone()


@pytest.mark.parametrize(
    "on_update,on_delete,drift",
    [
        ("NO ACTION", "NO ACTION", False),
        ("CASCADE", "NO ACTION", True),
        ("NO ACTION", "CASCADE", True),
        ("RESTRICT", "SET NULL", True),
        ("SET DEFAULT", "RESTRICT", True),
    ],
)
def test_existing_sqlite_fk_actions_are_not_silently_equivalent(on_update, on_delete, drift):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('CREATE TABLE "e2_action_parents" ("id" INTEGER PRIMARY KEY)')
        connection.execute(
            'CREATE TABLE "e2_action_children" ('
            '"id" INTEGER PRIMARY KEY, "parent_id" INTEGER NOT NULL, '
            'FOREIGN KEY("parent_id") REFERENCES "e2_action_parents"("id") '
            f"ON UPDATE {on_update} ON DELETE {on_delete})"
        )
        read_only = SQLiteReadOnly(connection)
        info = inspect_table(read_only, "e2_action_children")
        assert info.foreign_key_actions == (("parent_id", on_update, on_delete),)
        plan = plan_schema(read_only, Child)
        action_changes = [
            item for item in plan.changes if item.kind == "foreign_key_action_mismatch"
        ]
        assert bool(action_changes) is drift
        if drift:
            assert action_changes[0].column == "parent_id"
            assert plan.requires_review
    finally:
        connection.close()


def test_unreferenced_columns_have_no_action_drift():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            'CREATE TABLE "e2_action_children" ("id" INTEGER PRIMARY KEY, "parent_id" INTEGER NOT NULL)'
        )
        plan = plan_schema(SQLiteReadOnly(connection), Child)
        assert any(item.kind == "foreign_key_mismatch" for item in plan.changes)
        assert not any(item.kind == "foreign_key_action_mismatch" for item in plan.changes)
    finally:
        connection.close()
