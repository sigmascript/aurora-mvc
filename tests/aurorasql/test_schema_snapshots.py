"""Deterministic, advisory schema snapshots with no migration execution."""

import json

import pytest

from aurora.aurorasql import Model, column, connect, inspect_table, schema_snapshot, compare_snapshots


class Account(Model):
    id = column(int, primary_key=True)
    email = column(str, length=120, unique=True)
    active = column(bool, default=True)


class ExpandedAccount(Model):
    __table__ = "account"
    id = column(int, primary_key=True)
    email = column(str, length=160, unique=True)
    active = column(bool, default=True)
    nickname = column(str, nullable=True)


class Other(Model):
    id = column(int, primary_key=True)


def test_snapshot_is_deterministic_json_and_order_independent():
    first = schema_snapshot(Other, Account, dialect="sqlite")
    second = schema_snapshot(Account, Other, dialect="sqlite")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["tables"][0]["name"] == "account"
    assert first["tables"][0]["columns"][0]["name"] == "active"
    assert compare_snapshots(first, second) == []


def test_snapshot_changes_are_advisory_and_preserve_data():
    before = schema_snapshot(Account, dialect="sqlite")
    after = schema_snapshot(ExpandedAccount, dialect="sqlite")
    changes = compare_snapshots(before, after)
    assert {(c["kind"], c["column"]) for c in changes} == {
        ("changed_column", "email"), ("added_column", "nickname")
    }
    assert all(c["requires_review"] for c in changes)
    with connect("sqlite:///:memory:") as db:
        assert not inspect_table(db, "account").exists
        compare_snapshots(before, after)
        assert not inspect_table(db, "account").exists


def test_snapshot_removals_and_additions_are_not_guessed_as_renames():
    before = schema_snapshot(Account, dialect="sqlite")
    after = schema_snapshot(Other, dialect="sqlite")
    changes = compare_snapshots(before, after)
    assert {c["kind"] for c in changes} == {"removed_table", "added_table"}


def test_snapshot_rejects_duplicates_malformed_inputs_and_dialect_mismatch():
    with pytest.raises(ValueError, match="Duplicate"):
        schema_snapshot(Account, Account, dialect="sqlite")
    with pytest.raises(ValueError, match="At least"):
        schema_snapshot(dialect="sqlite")
    with pytest.raises(ValueError, match="dialect"):
        compare_snapshots(schema_snapshot(Account, dialect="sqlite"), schema_snapshot(Account, dialect="mysql"))
    with pytest.raises(ValueError, match="format"):
        compare_snapshots({}, {})


def test_snapshot_does_not_execute_callable_defaults():
    def explode():
        raise AssertionError("Default executed during snapshot")

    class Local(Model):
        id = column(int, primary_key=True)
        value = column(str, default=explode)

    snapshot = schema_snapshot(Local, dialect="postgresql")
    default = snapshot["tables"][0]["columns"][1]["python_default"]
    assert default["kind"] == "callable"
    assert "explode" in default["name"]


def test_server_default_is_explicitly_flagged_not_silently_assumed():
    from aurora.aurorasql import plan_schema

    class Defaulted(Model):
        id = column(int, primary_key=True)
        status = column(str, length=20, server_default="active")

    with connect("sqlite:///:memory:") as db:
        db.execute_write('CREATE TABLE "defaulted" ("id" INTEGER PRIMARY KEY, "status" VARCHAR(20) NOT NULL)')
        plan = plan_schema(db, Defaulted)
        assert any("server_default" in warning and "not emitted" in warning for warning in plan.warnings)
        assert plan.execution_allowed is False


def test_added_required_column_needs_data_strategy_even_with_python_default():
    class RequiredAccount(Model):
        __table__ = "account"
        id = column(int, primary_key=True)
        email = column(str, length=120, unique=True)
        active = column(bool, default=True)
        new_value = column(str, default="provided_in_python")

    changes = compare_snapshots(
        schema_snapshot(Account, dialect="sqlite"),
        schema_snapshot(RequiredAccount, dialect="sqlite"),
    )
    assert changes == [{
        "kind": "added_column", "table": "account", "column": "new_value",
        "risk": "requires_data_strategy", "requires_review": True,
    }]


def test_removed_columns_are_flagged_as_potential_data_loss():
    class SmallerAccount(Model):
        __table__ = "account"
        id = column(int, primary_key=True)

    changes = compare_snapshots(
        schema_snapshot(Account, dialect="mysql"),
        schema_snapshot(SmallerAccount, dialect="mysql"),
    )
    assert {c["risk"] for c in changes} == {"potential_data_loss"}


def test_preview_only_differences_are_not_schema_changes():
    before = schema_snapshot(Account, dialect="sqlite")
    after = json.loads(json.dumps(before))
    after["tables"][0]["create_sql_preview"] = "different preview text"
    assert compare_snapshots(before, after) == []


def test_snapshot_rejects_duplicate_columns_and_invalid_field_shapes():
    snapshot = schema_snapshot(Account, dialect="sqlite")
    malformed = json.loads(json.dumps(snapshot))
    malformed["tables"][0]["columns"].append(malformed["tables"][0]["columns"][0])
    with pytest.raises(ValueError, match="Duplicate snapshot column"):
        compare_snapshots(snapshot, malformed)

    malformed = json.loads(json.dumps(snapshot))
    del malformed["tables"][0]["columns"][0]["nullable"]
    with pytest.raises(ValueError, match="Invalid snapshot column"):
        compare_snapshots(snapshot, malformed)
