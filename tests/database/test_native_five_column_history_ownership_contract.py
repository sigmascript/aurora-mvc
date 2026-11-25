"""Native five-column history inspection belongs to the history owner."""

import pytest

from aurora.database import migration_history


def test_native_history_reader_uses_history_owner_and_legacy_import():
    for name in ("history_table", "has_history", "_verify_table", "read_history", "_directory"):
        owner = getattr(migration_history, name)
        assert getattr(migration_history, name) is owner
        assert owner.__module__ == migration_history.__name__


def test_native_history_identity_fails_closed_for_conflicting_tables(monkeypatch):
    class NativeConnection:
        dialect = "mysql"

        def execute_write(self, *args, **kwargs):
            raise AssertionError("History discovery must not write")

    db = NativeConnection()
    monkeypatch.setattr(migration_history, "_objects", lambda _db: ["migrations", "__migrations__"])
    with pytest.raises(RuntimeError, match="Both migrations"):
        migration_history.history_table(db)


def test_native_history_reader_refuses_unrecognized_catalog_without_writes(monkeypatch, tmp_path):
    class NativeConnection:
        dialect = "postgresql"

        def fetch_all(self, *args, **kwargs):
            raise AssertionError("Invalid catalog must be refused before reading rows")

        def execute_write(self, *args, **kwargs):
            raise AssertionError("History reader must not write")

    db = NativeConnection()
    monkeypatch.setattr(
        migration_history, "_objects", lambda _db: ["__migrations__", "aurorasql_schema_history"]
    )
    with pytest.raises(RuntimeError, match="Unrecognized or mixed"):
        migration_history.read_history(db, tmp_path, "default")
