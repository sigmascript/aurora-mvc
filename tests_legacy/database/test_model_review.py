"""Model-driven reviews must be read-only, additive and historically verified."""

import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.core import Connection
from aurora.database.model_review import create_sqlite_model_review
from aurora.database.sqlite_additive_migration import execute_sqlite_additive_migration


class Customer(Model):
    __table__ = "review_customers"
    id = column(int, primary_key=True)


class CustomerWithEmail(Model):
    __qualname__ = "Customer"
    __table__ = "review_customers"
    id = column(int, primary_key=True)
    email = column(str, nullable=True)


class NewArticle(Model):
    __table__ = "review_articles"
    id = column(int, primary_key=True)
    name = column(str)


class RequiredEmail(Model):
    __qualname__ = "Customer"
    __table__ = "review_customers"
    id = column(int, primary_key=True)
    email = column(str)


class RequiredStatus(Model):
    __qualname__ = "Customer"
    __table__ = "review_customers"
    id = column(int, primary_key=True)
    status = column(str, default="draft")


def _initialized(tmp_path):
    path = tmp_path / "review.sqlite3"

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (Customer,))

    return path


def test_model_review_is_read_only_and_can_drive_existing_guarded_executor(tmp_path):
    path = _initialized(tmp_path)
    before = path.read_bytes()

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        review = create_sqlite_model_review(connection, (CustomerWithEmail, NewArticle))

    assert path.read_bytes() == before
    assert {op["kind"] for op in review["plan"]["operations"]} == {
        "added_column", "added_table",
    }

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        execute_sqlite_additive_migration(
            raw, review, (CustomerWithEmail, NewArticle),
            reviewed_sha256=review["review_sha256"], confirm=True,
        )
        assert "email" in [row[1] for row in raw.execute('PRAGMA table_info("review_customers")')]
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='review_articles'"
        ).fetchone() == ("review_articles",)


def test_model_review_refuses_required_column(tmp_path):
    path = _initialized(tmp_path)

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        with pytest.raises(ValueError, match="nullable"):
            create_sqlite_model_review(connection, (RequiredEmail,))


def test_model_review_refuses_schema_drift(tmp_path):
    path = _initialized(tmp_path)

    with sqlite3.connect(path) as raw:
        raw.execute('ALTER TABLE review_customers ADD COLUMN unexpected TEXT')

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        with pytest.raises(RuntimeError, match="history"):
            create_sqlite_model_review(connection, (CustomerWithEmail,))


def test_model_review_applies_required_column_with_database_default(tmp_path):
    path = _initialized(tmp_path)

    with sqlite3.connect(path) as raw:
        raw.execute('INSERT INTO review_customers(id) VALUES (1)')

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        review = create_sqlite_model_review(connection, (RequiredStatus,))

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        execute_sqlite_additive_migration(
            raw, review, (RequiredStatus,), reviewed_sha256=review["review_sha256"],
            confirm=True,
        )
        assert raw.execute('SELECT status FROM review_customers WHERE id=1').fetchone() == (
            "draft",
        )
        status = next(row for row in raw.execute('PRAGMA table_info("review_customers")')
                      if row[1] == "status")
        assert status[3] == 1
        assert status[4] == "'draft'"
