"""Practical model field contracts for the opt-in AuroraSQL implementation."""

from datetime import date, datetime

import pytest

from aurora.aurorasql import Model, column, connect, create_table_sql, create_tables


class Account(Model):
    __table__ = "field_accounts"
    id = column(int, primary_key=True)
    username = column(str, length=40, unique=True)
    enabled = column(bool, default=True)
    joined = column(date, nullable=True)


class Message(Model):
    __table__ = "field_messages"
    id = column(int, primary_key=True)
    account_id = column(int, references="field_accounts.id")
    body = column(str, length=120)
    created = column(datetime, default=lambda: datetime(2026, 1, 1))


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_declared_field_schema_is_explicit_and_backend_quoted(dialect):
    account = create_table_sql(Account, dialect)
    message = create_table_sql(Message, dialect)
    quote = "`" if dialect == "mysql" else '"'
    assert f"{quote}username{quote} VARCHAR(40) NOT NULL UNIQUE" in account
    assert f"FOREIGN KEY ({quote}account_id{quote})" in message
    assert f"{quote}enabled{quote} {'INTEGER' if dialect == 'sqlite' else 'BOOLEAN'} NOT NULL" in account  # No SQL default.
    assert f"{quote}created{quote} " in message  # No SQL default.


def test_field_declaration_rejects_unsupported_types_and_options():
    with pytest.raises(TypeError, match="Unsupported"):
        column(bytes)
    with pytest.raises(ValueError, match="length"):
        column(int, length=20)
    with pytest.raises(ValueError, match="length"):
        column(str, length=0)
    with pytest.raises(TypeError, match="booleans"):
        column(str, unique=1)
    with pytest.raises(ValueError, match="nullable"):
        column(int, primary_key=True, nullable=True)
    with pytest.raises(ValueError, match="identifier"):
        column(int, references="accounts.id;DROP")
    with pytest.raises(TypeError, match="Expected"):
        column(int, default="wrong")


def test_explicit_null_primary_key_is_rejected_before_insert():
    with connect("sqlite:///:memory:") as db:
        create_tables(db, Account)
        with pytest.raises(ValueError, match="Primary-key"):
            Account.using(db).create(id=None, username="invalid")
        assert Account.using(db).read().count() == 0


def test_python_defaults_uniqueness_nullability_and_foreign_keys():
    with connect("sqlite:///:memory:") as db:
        db.execute("PRAGMA foreign_keys=ON")
        create_tables(db, Account, Message)
        accounts = Account.using(db)
        messages = Message.using(db)
        account = accounts.create(username="alice")
        assert account.enabled is True
        assert account.id == 1
        assert accounts.first(where={"id": 1}).enabled is True
        assert accounts.update(values={"enabled": False}, where={"id": 1}) == 1
        assert accounts.first(where={"id": 1}).enabled is False
        with pytest.raises(Exception):
            accounts.create(username="alice")
        with pytest.raises(ValueError, match="Non-nullable"):
            accounts.create(username=None)
        with pytest.raises(ValueError, match="String exceeds"):
            accounts.create(username="x" * 41)
        with pytest.raises(TypeError, match="Boolean"):
            accounts.create(username="bob", id=True)
        message = messages.create(account_id=account.id, body="hello")
        assert isinstance(message.created, datetime)
        assert messages.first(where={"id": message.id}).body == "hello"
        with pytest.raises(Exception):
            messages.create(account_id=999, body="orphan")
