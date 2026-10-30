"""Additive column review stays read-only and fails closed."""


import pytest

from aurora.aurorasql import Column, Model, connect, migration_plan, schema_snapshot
from aurora.aurorasql.additive_approval import review_add_column


class Old(Model):
    __table__ = "a7_b2_review"
    id = Column(int, primary_key=True)


class New(Model):
    __table__ = "a7_b2_review"
    id = Column(int, primary_key=True)
    note = Column(str, nullable=True)


class Required(Model):
    __table__ = "a7_b2_review"
    id = Column(int, primary_key=True)
    note = Column(str, nullable=False)


class Different(Model):
    __table__ = "a7_b2_review"
    id = Column(int, primary_key=True)
    note = Column(int, nullable=True)


def _setup():
    connection = connect("sqlite:///:memory:")
    connection.execute_write('CREATE TABLE "a7_b2_review" ("id" INTEGER PRIMARY KEY)')
    before = schema_snapshot(Old, dialect="sqlite")
    after = schema_snapshot(New, dialect="sqlite")
    plan = migration_plan(before, after, migration_id="m001_add_note")
    return connection, before, after, plan


def test_review_returns_non_executable_preview():
    connection, before, after, plan = _setup()
    review = review_add_column(connection, before, after, plan)
    assert review.execution_allowed is False
    assert review.sql_preview == 'ALTER TABLE "a7_b2_review" ADD COLUMN "note" TEXT'
    assert len(connection.fetch_all('PRAGMA table_info("a7_b2_review")')) == 1


def test_review_is_deterministic():
    connection, before, after, plan = _setup()
    assert review_add_column(connection, before, after, plan) == review_add_column(connection, before, after, plan)


def test_rejects_tampered_plan():
    connection, before, after, plan = _setup()
    plan["operations"][0]["table"] = "another_table"
    with pytest.raises(ValueError):
        review_add_column(connection, before, after, plan)


def test_rejects_required_column():
    connection, before, _, _ = _setup()
    after = schema_snapshot(Required, dialect="sqlite")
    plan = migration_plan(before, after, migration_id="m001_required")
    with pytest.raises((ValueError, RuntimeError)):
        review_add_column(connection, before, after, plan)


def test_rejects_schema_drift():
    connection, before, after, plan = _setup()
    connection.execute_write('ALTER TABLE "a7_b2_review" ADD COLUMN "extra" TEXT')
    with pytest.raises(RuntimeError):
        review_add_column(connection, before, after, plan)


def test_rejects_already_applied_column():
    connection, before, after, plan = _setup()
    connection.execute_write('ALTER TABLE "a7_b2_review" ADD COLUMN "note" TEXT')
    with pytest.raises(RuntimeError):
        review_add_column(connection, before, after, plan)


def test_rejects_changed_snapshot():
    connection, before, _, plan = _setup()
    with pytest.raises(ValueError):
        review_add_column(connection, before, schema_snapshot(Different, dialect="sqlite"), plan)
