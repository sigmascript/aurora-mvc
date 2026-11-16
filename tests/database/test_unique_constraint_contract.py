"""Single-column UNIQUE: creation, physical enforcement, and read-only drift review."""

import sqlite3

import pytest

from aurora.database import Model, column, connect, create_table_sql, plan_schema
from aurora.database.schema import create_table


class UniqueEmail(Model):
    __table__ = "unique_email_contract"
    id = column(int, primary_key=True)
    email = column(str, length=254, unique=True)


class RepeatedEmail(Model):
    __table__ = "unique_email_contract"
    id = column(int, primary_key=True)
    email = column(str, length=254, unique=False)


@pytest.mark.parametrize("dialect", ("sqlite", "mysql", "postgresql"))
def test_new_table_declares_only_requested_uniqueness(dialect):
    unique_sql = create_table_sql(UniqueEmail, dialect)
    repeated_sql = create_table_sql(RepeatedEmail, dialect)
    assert "UNIQUE" in unique_sql
    assert "UNIQUE" not in repeated_sql


def test_sqlite_enforces_uniqueness_and_detects_both_drift_directions():
    with connect("sqlite:///:memory:") as db:
        create_table(db, UniqueEmail)
        db.execute_write("INSERT INTO unique_email_contract (id, email) VALUES (1, 'a@example.com')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute_write("INSERT INTO unique_email_contract (id, email) VALUES (2, 'a@example.com')")
        changes = plan_schema(db, RepeatedEmail).changes
        assert any(change.kind == "unique_mismatch" and change.column == "email" for change in changes)
        assert db.fetch_first("SELECT email FROM unique_email_contract WHERE id = 1")["email"] == "a@example.com"

    with connect("sqlite:///:memory:") as db:
        create_table(db, RepeatedEmail)
        db.execute_write("INSERT INTO unique_email_contract (id, email) VALUES (1, 'a@example.com')")
        db.execute_write("INSERT INTO unique_email_contract (id, email) VALUES (2, 'a@example.com')")
        changes = plan_schema(db, UniqueEmail).changes
        assert any(change.kind == "unique_mismatch" and change.column == "email" for change in changes)
        assert db.fetch_first("SELECT COUNT(*) AS total FROM unique_email_contract")["total"] == 2
