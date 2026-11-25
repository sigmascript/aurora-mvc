"""Native timeline inspection remains read-only after moving its owner."""

from types import SimpleNamespace

import pytest

from aurora.database.migration_timeline import (
    ServerMigrationEntry,
    inspect_server_timeline,
)
from aurora.database.migration_timeline import (
    inspect_server_timeline as legacy_inspection_import,
)


class ReadOnlyConnection:
    dialect = "mysql"

    def __init__(self, rows):
        self.rows = rows
        self.reads = []

    def fetch_all(self, sql):
        self.reads.append(sql)
        return self.rows

    def execute_write(self, *args, **kwargs):
        raise AssertionError("Native inspection must never execute writes")


def _catalog():
    names = (
        "migration_id",
        "name",
        "title",
        "database_alias",
        "review_sha256",
        "models_json",
        "operation_json",
        "schema_sha256",
        "state",
    )
    return SimpleNamespace(
        exists=True,
        columns=tuple(
            SimpleNamespace(
                name=name,
                primary_key=name == "migration_id",
                nullable=name in ("operation_json", "schema_sha256"),
            )
            for name in names
        ),
        uniqueness_verified=True,
        unique_columns=("name",),
    )


def test_native_inspection_reads_valid_initial_without_mutating(monkeypatch):
    from aurora.database import schema_inspection
    from aurora.database.model_discovery import canonical_model_map

    monkeypatch.setattr(schema_inspection, "inspect_table", lambda connection, table: _catalog())
    row = {
        "migration_id": 1,
        "name": "Initial",
        "title": "Initial migration",
        "database_alias": "default",
        "review_sha256": "a" * 64,
        "models_json": canonical_model_map({"models:Notes": "notes"}),
        "operation_json": None,
        "schema_sha256": "b" * 64,
        "state": "applied",
    }
    connection = ReadOnlyConnection([row])
    actual = inspect_server_timeline(connection, "default")
    assert actual == legacy_inspection_import(connection, "default")
    assert len(actual) == 1
    assert isinstance(actual[0], ServerMigrationEntry)
    assert actual[0].verified_current is False
    assert actual[0].physical_history_consistent is False
    assert len(connection.reads) == 2


def test_native_inspection_refuses_uncertain_successor_and_does_not_write(monkeypatch):
    from aurora.database import schema_inspection
    from aurora.database.model_discovery import canonical_model_map

    monkeypatch.setattr(schema_inspection, "inspect_table", lambda connection, table: _catalog())
    initial = {
        "migration_id": 1,
        "name": "Initial",
        "title": "Initial migration",
        "database_alias": "default",
        "review_sha256": "a" * 64,
        "models_json": canonical_model_map({"models:Notes": "notes"}),
        "operation_json": None,
        "schema_sha256": None,
        "state": "uncertain",
    }
    successor = dict(initial, migration_id=2, name="second", state="prepared")
    connection = ReadOnlyConnection([initial, successor])
    with pytest.raises(RuntimeError, match="unresolved DDL"):
        inspect_server_timeline(connection, "default")
    assert len(connection.reads) == 1
