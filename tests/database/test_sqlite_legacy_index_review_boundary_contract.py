"""The retained SQLite journal inspector reviews index ownership safely."""

import sqlite3

import pytest

from aurora.database.schema_inspection import _inspect_sqlite


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, slug TEXT UNIQUE);
        CREATE TABLE other (id INTEGER PRIMARY KEY, title TEXT);
        CREATE INDEX notes_title_idx ON notes(title);
        CREATE INDEX other_title_idx ON other(title);
    """)
    try:
        yield connection
    finally:
        connection.close()


def test_legacy_drop_index_reviews_ordinary_index_without_rename_lookup(database):
    catalog, warnings = _inspect_sqlite(
        database,
        {
            "kind": "drop_index",
            "table": "notes",
            "name": "notes_title_idx",
        },
        "data",
    )
    assert any(row[1] == "notes_title_idx" for row in catalog)
    # The independent legacy-journal guard may still refuse execution.
    assert "Missing or incompatible migration intent journal" in warnings
    assert database.execute("SELECT 1 FROM sqlite_master WHERE name='notes_title_idx'").fetchone()


@pytest.mark.parametrize("index", ["other_title_idx", "does_not_exist"])
def test_legacy_drop_index_refuses_unowned_or_absent_indexes(database, index):
    with pytest.raises(ValueError, match="selected table"):
        _inspect_sqlite(
            database,
            {
                "kind": "drop_index",
                "table": "notes",
                "name": index,
            },
            "data",
        )


def test_legacy_drop_index_refuses_constraint_owned_index(database):
    name = database.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'sqlite_autoindex_%'"
    ).fetchone()[0]
    with pytest.raises(ValueError, match="selected table|constraint-owned"):
        _inspect_sqlite(
            database,
            {
                "kind": "drop_index",
                "table": "notes",
                "name": name,
            },
            "data",
        )
