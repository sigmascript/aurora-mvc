"""Shared eager result contract for direct read and JOIN operations."""

from decimal import Decimal

import pytest

from aurora.database import Model, column, connect


class Account(Model):
    __table__ = "account_records"
    id = column(int, primary_key=True)
    name = column(str)


class Invoice(Model):
    __table__ = "invoices_v2"
    id = column(int, primary_key=True)
    account_id = column(int, references="account_records.id")
    amount = column(Decimal)


@pytest.fixture
def database():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE account_records (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.execute_write("CREATE TABLE invoices_v2 (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, amount NUMERIC NOT NULL)")
        with db.transaction():
            Account.using(db).create(id=1, name="A")
            Account.using(db).create(id=2, name="B")
            Invoice.using(db).create(id=10, account_id=1, amount=Decimal("4.5"))
            Invoice.using(db).create(id=11, account_id=1, amount=Decimal("7.5"))
        yield db


def test_read_is_list_compatible_with_all_summaries(database):
    rows = Invoice.using(database).read(order_by="id")
    assert isinstance(rows, list)
    assert rows == rows.all()
    assert type(rows[0]) is dict
    assert rows.all()[0].id == 10
    assert rows.first().id == 10
    assert rows.first()["id"] == 10
    assert rows.last().id == 11
    assert rows.count() == 2
    assert rows.min("Invoice.amount") == Decimal("4.5")
    assert rows.max("invoices_v2.amount") == Decimal("7.5")
    assert rows.sum("amount") == Decimal("12.0")
    assert rows.avg("amount") == Decimal("6.0")
    assert Invoice.using(database).read({"id": 999}).first() is None
    empty = Invoice.using(database).read({"id": 999})
    assert empty.last() is None and empty.count() == 0
    assert empty.sum("amount") is None and empty.avg("amount") is None


def test_join_model_names_survive_physical_table_names(database):
    rows = Invoice.using(database).join(Account, order_by={"Invoice.id": "ASC"})
    assert rows.count() == 2
    assert rows.first()["left__id"] == 10
    assert rows.last()["left__id"] == 11
    assert rows.sum("Invoice.amount") == Decimal("12.0")
    assert rows.avg("Invoice.amount") == Decimal("6.0")
    assert rows.min("invoices_v2.amount") == Decimal("4.5")
    assert rows.max("Invoice.amount") == Decimal("7.5")
    with pytest.raises(ValueError, match="Unknown or unavailable"):
        rows.sum("nNtes.amount")
    with pytest.raises(ValueError, match="Unknown or unavailable"):
        rows.avg("Account.amount")


def test_join_projection_rejects_missing_aggregate_column(database):
    rows = Invoice.using(database).join(Account, cols=["Account.name"])
    assert rows.count() == 2
    with pytest.raises(ValueError, match="Unknown or unavailable"):
        rows.sum("Invoice.amount")
    with pytest.raises(TypeError, match="numeric"):
        rows.sum("Account.name")
