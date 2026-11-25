"""Forward migration evidence stays separate from create-only FK v2 review."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database.initialization import _write_snapshot
from aurora.database.migration_execution import execute_server_forward_migration
from aurora.database.models import Model, column
from aurora.database.snapshots import compare_snapshots, schema_snapshot


class Parent(Model):
    __table__ = "forward_parent"
    id = column(int, primary_key=True)


class Child(Model):
    __table__ = "forward_child"
    id = column(int, primary_key=True)
    parent_id = column(int, reference="Parent.id", on_delete="CASCADE")


@pytest.mark.parametrize("dialect", ("sqlite", "mysql", "postgresql"))
def test_extended_reference_cannot_be_written_by_v1_history_path(dialect):
    with pytest.raises(ValueError, match="v2 snapshot review"):
        schema_snapshot(Parent, Child, dialect=dialect)


@pytest.mark.parametrize("dialect", ("sqlite", "mysql", "postgresql"))
def test_reviewing_v2_does_not_authorize_cross_version_migration(dialect):
    v1 = schema_snapshot(Parent, dialect=dialect)
    v2 = schema_snapshot(Parent, Child, dialect=dialect, allow_v2=True)
    with pytest.raises(ValueError, match="Cross-version"):
        compare_snapshots(v1, v2)


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_server_snapshot_writer_refuses_unverified_physical_fk_before_file_creation(
    tmp_path, monkeypatch, dialect
):
    from aurora.database import initialization

    monkeypatch.setattr(initialization, "_physical", lambda connection: [])
    connection = SimpleNamespace(dialect=dialect)
    with pytest.raises(RuntimeError, match="Observed server tables differ"):
        _write_snapshot(connection, tmp_path, "test_alias", (Parent, Child), "Initial")
    assert not (tmp_path / "migrations").exists()


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_server_forward_execution_refuses_unapproved_review_before_writes(dialect, tmp_path):
    class NeverWrite:
        _transaction_active = False
        _migration_lock_active = False

        def __init__(self):
            self.dialect = dialect
            self.calls = []

        def execute_write(self, *args, **kwargs):
            self.calls.append(args)
            raise AssertionError("DDL must not be attempted")

    connection = NeverWrite()
    invalid_review = SimpleNamespace(
        operations=({"kind": "drop_table"},),
        statements=("DROP TABLE precious",),
        review_sha256="review-value",
    )
    with pytest.raises(PermissionError, match="approved safe server operation"):
        execute_server_forward_migration(
            connection,
            (Parent,),
            tmp_path,
            "test_alias",
            identity="identity",
            parsed=object(),
            review=invalid_review,
            approval="review-value",
            title="Prohibited",
            backup_path=None,
            config_path=tmp_path,
        )
    assert connection.calls == []


def test_fk_action_edit_is_detected_in_v2_review():
    before = schema_snapshot(Parent, Child, dialect="sqlite", allow_v2=True)
    after = deepcopy(before)
    col = next(
        c
        for t in after["tables"]
        if t["name"] == "forward_child"
        for c in t["columns"]
        if c["name"] == "parent_id"
    )
    col["on_delete"] = "RESTRICT"
    assert compare_snapshots(before, after) == [
        {
            "kind": "changed_column",
            "table": "forward_child",
            "column": "parent_id",
            "risk": "requires_data_strategy",
            "requires_review": True,
        }
    ]
