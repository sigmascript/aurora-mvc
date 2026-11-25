"""Read-only review when backend FK-action evidence cannot be established."""

import pytest

from aurora.database import Model, column
from aurora.database import schema_planning as planning
from aurora.database.schema_inspection import ColumnInfo, TableInfo


class Child(Model):
    __table__ = "e2_children"
    id = column(int, primary_key=True)
    parent_id = column(int, reference="e2_parents.id")


class Plain(Model):
    __table__ = "e2_plain"
    id = column(int, primary_key=True)


class FakeConnection:
    def __init__(self, dialect):
        self.dialect = dialect


def existing(model, *, constraints_verified=False, actions=None):
    return TableInfo(
        model.__table_name__,
        True,
        tuple(
            ColumnInfo(name, "BIGINT", False, field.primary_key)
            for name, field in model.__columns__.items()
        ),
        constraints_verified=constraints_verified,
        foreign_keys=(("parent_id", "e2_parents", "id"),) if model is Child else (),
        foreign_key_actions=actions,
    )


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_unverified_server_foreign_key_evidence_requires_review(monkeypatch, dialect):
    monkeypatch.setattr(planning, "inspect_table", lambda *_: existing(Child))
    result = planning.plan_schema(FakeConnection(dialect), Child)
    assert result.requires_review
    assert any("ON UPDATE/ON DELETE" in warning for warning in result.warnings)
    assert not result.execution_allowed


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_no_foreign_key_no_foreign_key_warning(monkeypatch, dialect):
    monkeypatch.setattr(planning, "inspect_table", lambda *_: existing(Plain))
    result = planning.plan_schema(FakeConnection(dialect), Plain)
    assert not any("ON UPDATE/ON DELETE" in warning for warning in result.warnings)


def test_historical_sqlite_inspection_missing_action_evidence_requires_review(monkeypatch):
    monkeypatch.setattr(
        planning, "inspect_table", lambda *_: existing(Child, constraints_verified=True)
    )
    result = planning.plan_schema(FakeConnection("sqlite"), Child)
    assert result.requires_review
    assert any("ON UPDATE/ON DELETE" in warning for warning in result.warnings)


def test_sqlite_explicit_no_action_evidence_does_not_add_unknown_action_warning(monkeypatch):
    monkeypatch.setattr(
        planning,
        "inspect_table",
        lambda *_: existing(
            Child, constraints_verified=True, actions=(("parent_id", "NO ACTION", "NO ACTION"),)
        ),
    )
    result = planning.plan_schema(FakeConnection("sqlite"), Child)
    assert not any("ON UPDATE/ON DELETE" in warning for warning in result.warnings)
