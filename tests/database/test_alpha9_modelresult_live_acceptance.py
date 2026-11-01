"""Opt-in ModelResult acceptance against disposable MySQL and PostgreSQL databases.

Creates only uniquely named tables; never resets a database or removes existing tables.
"""

import os
from uuid import uuid4

import pytest

from aurora.database import Model, column, connect, create_table


@pytest.mark.parametrize("dialect,environment", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_modelresult_read_join_crud_and_guards(dialect, environment):
    if os.getenv("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Requires explicit disposable-database DDL opt-in")
    url = os.getenv(environment)
    if not url:
        pytest.skip(f"{environment} is not configured")

    suffix = uuid4().hex[:12]
    account_table = f"a9_accounts_{suffix}"
    invoice_table = f"a9_invoices_{suffix}"
    Account = type("Account", (Model,), {
        "__table__": account_table,
        "id": column(int, primary_key=True),
        "name": column(str, length=80),
    })
    Invoice = type("Invoice", (Model,), {
        "__table__": invoice_table,
        "id": column(int, primary_key=True),
        "account_id": column(int, references=f"{account_table}.id"),
        "amount": column(int),
    })

    with connect(url) as db:
        assert db.dialect == dialect
        created = []
        try:
            for model in (Account, Invoice):
                create_table(db, model)
                created.append(model.__table_name__)

            with db.transaction():
                Account.using(db).create(id=1, name="One")
                Account.using(db).create(id=2, name="Two")
                Invoice.using(db).create(id=10, account_id=1, amount=4)
                Invoice.using(db).create(id=11, account_id=1, amount=8)

            invoices = Invoice.using(db)
            rows = invoices.read(order_by={"id": "ASC"})
            assert isinstance(rows, list)
            assert type(rows[0]) is dict
            assert rows.first().id == 10 and rows.last().id == 11
            assert rows.all()[0].amount == 4
            assert rows.count() == 2
            assert rows.min("Invoice.amount") == 4
            assert rows.max("Invoice.amount") == 8
            assert rows.sum("Invoice.amount") == 12
            assert rows.avg("Invoice.amount") == 6
            assert invoices.read({"id": -1}).first() is None

            joined = invoices.join(Account, order_by={"Invoice.id": "ASC"})
            assert joined.count() == 2
            assert joined.first()["left__id"] == 10
            assert joined.last()["left__id"] == 11
            assert joined.sum("Invoice.amount") == 12
            assert joined.avg("Invoice.amount") == 6
            assert joined.min("Invoice.amount") == 4
            assert joined.max("Invoice.amount") == 8
            assert joined.sum(f"{invoice_table}.amount") == 12
            with pytest.raises(ValueError, match="Unknown or unavailable"):
                joined.sum("Typo.amount")
            with pytest.raises(ValueError, match="Unknown or unavailable"):
                joined.sum("Account.amount")

            with pytest.raises(RuntimeError, match="confirm=True"):
                invoices.update(values={"amount": 99})
            with pytest.raises(RuntimeError, match="confirm=True"):
                invoices.delete()
            assert invoices.update(values={"amount": 9}, where={"id": 10}) == 1
            assert invoices.read({"id": 10}).first().amount == 9
            assert invoices.delete(where={"id": 11}) == 1
            assert invoices.read().count() == 1
        finally:
            # Drop only tables successfully created by this test, child first.
            for table in reversed(created):
                quoted = f"`{table}`" if dialect == "mysql" else f'"{table}"'
                db.execute_write(f"DROP TABLE {quoted}")
