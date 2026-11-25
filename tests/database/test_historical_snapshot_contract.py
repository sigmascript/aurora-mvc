"""E2 read-only characterization of persisted AuroraSQL schema.v1 evidence.

These tests must remain valid when the public column API transitions to the
approved spelling; no schema is created or historical record rewritten here.
"""

from copy import deepcopy

import pytest

from aurora.database import Model, column, compare_snapshots, schema_snapshot
from aurora.database.snapshots import _snapshot_tables

LEGACY_COLUMN = {
    "name": "value",
    "type": "str",
    "length": 24,
    "primary_key": False,
    "nullable": False,
    "unique": False,
    "references": None,
    "python_default": {"kind": "literal", "value": None},
    "server_default": "'existing'",
}


def historical(dialect="sqlite"):
    return {
        "format": "aurorasql.schema.v1",
        "dialect": dialect,
        "tables": [
            {
                "name": "record",
                "columns": [deepcopy(LEGACY_COLUMN)],
                "create_sql_preview": "OLD DDL PREVIEW MUST NOT BE EXECUTED",
            }
        ],
    }


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_historical_v1_record_remains_readable_and_unchanged(dialect):
    old = historical(dialect)
    witness = deepcopy(old)
    assert _snapshot_tables(old)["record"]["value"]["server_default"] == "'existing'"
    assert compare_snapshots(old, deepcopy(old)) == []
    assert old == witness


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_historical_nullability_and_references_still_mean_schema_change(dialect):
    old = historical(dialect)
    modified = deepcopy(old)
    modified["tables"][0]["columns"][0]["nullable"] = True
    assert compare_snapshots(old, modified) == [
        {
            "kind": "changed_column",
            "table": "record",
            "column": "value",
            "risk": "requires_data_strategy",
            "requires_review": True,
        }
    ]
    modified = deepcopy(old)
    modified["tables"][0]["columns"][0]["references"] = "parent.id"
    assert compare_snapshots(old, modified)[0]["kind"] == "changed_column"


def test_historical_required_column_without_default_requires_data_strategy():
    old = historical()
    after = deepcopy(old)
    added = deepcopy(LEGACY_COLUMN)
    added.update(name="new_field", server_default=None)
    after["tables"][0]["columns"].append(added)
    assert compare_snapshots(old, after) == [
        {
            "kind": "added_column",
            "table": "record",
            "column": "new_field",
            "risk": "requires_data_strategy",
            "requires_review": True,
        }
    ]


def test_historical_required_column_with_sql_default_still_requires_review():
    old = historical()
    after = deepcopy(old)
    added = deepcopy(LEGACY_COLUMN)
    added["name"] = "new_field"
    after["tables"][0]["columns"].append(added)
    assert compare_snapshots(old, after)[0] == {
        "kind": "added_column",
        "table": "record",
        "column": "new_field",
        "risk": "additive",
        "requires_review": True,
    }


def test_snapshot_rejects_unannounced_field_shape_without_changing_v1_reader():
    old = historical()
    changed = deepcopy(old)
    field = changed["tables"][0]["columns"][0]
    field["not_null"] = not field.pop("nullable")
    with pytest.raises(ValueError, match="snapshot column"):
        compare_snapshots(old, changed)


def test_current_sql_default_uses_historical_v1_key_with_identical_meaning():
    class Record(Model):
        value = column(str, length=24, default="existing")

    for dialect in ("sqlite", "mysql", "postgresql"):
        before = historical(dialect)
        after = schema_snapshot(Record, dialect=dialect)
        assert compare_snapshots(before, after) == []
        assert after["format"] == "aurorasql.schema.v1"
        assert after["tables"][0]["columns"][0]["server_default"] == "'existing'"


def test_legacy_server_default_evidence_is_distinct_from_python_default():
    old = historical()
    changed = deepcopy(old)
    column_ = changed["tables"][0]["columns"][0]
    column_["server_default"] = None
    column_["python_default"] = {"kind": "literal", "value": "existing"}
    assert compare_snapshots(old, changed)[0]["kind"] == "changed_column"
