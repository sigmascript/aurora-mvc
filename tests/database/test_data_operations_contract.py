"""Essential current AuroraSQL CRUD, JOIN, and parameter-safety contracts."""

import pytest

from aurora.database import Model, column, connect


class Customer(Model):
    __table__ = "f07_customers"
    id = column(int, primary_key=True)
    name = column(str)


class Order(Model):
    __table__ = "f07_orders"
    id = column(int, primary_key=True)
    customer_id = column(int, reference="f07_customers.id")
    amount = column(int)


@pytest.fixture
def db():
    with connect("sqlite:///:memory:") as connection:
        connection.execute_write("CREATE TABLE f07_customers (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute_write("CREATE TABLE f07_orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER)")
        with connection.transaction():
            Customer.using(connection).create(id=1, name="Ada")
            Customer.using(connection).create(id=2, name="Lin")
            Order.using(connection).create(id=10, customer_id=1, amount=15)
            Order.using(connection).create(id=11, customer_id=2, amount=30)
        yield connection


def test_filtered_crud_and_whole_table_write_boundary(db):
    orders = Order.using(db)
    assert orders.read({"$or": [{"amount--lt": 20}, {"customer_id": 2}]},
                       order_by={"id": "ASC"}) == [
        {"id": 10, "customer_id": 1, "amount": 15},
        {"id": 11, "customer_id": 2, "amount": 30},
    ]
    with pytest.raises(RuntimeError, match="confirm=True"):
        orders.update(values={"amount": 0})
    with pytest.raises(RuntimeError, match="confirm=True"):
        orders.delete(where={})
    assert orders.update(values={"amount": 20}, where={"id": 10}) == 1
    assert orders.delete(where={"id": 11}) == 1
    assert orders.read() == [{"id": 10, "customer_id": 1, "amount": 20}]


def test_direct_join_and_parameterized_query(db):
    rows = Order.using(db).join(
        models=[Customer], cols=["Order.amount", "Customer.name"],
        where={"Customer.name": "Ada"},
    )
    assert rows == [{"left__amount": 15, "right__name": "Ada"}]
    assert Order.using(db).query(
        "SELECT id FROM f07_orders WHERE amount > :minimum ORDER BY id",
        {"minimum": 20},
    ) == [{"id": 11}]
    assert Order.using(db).query(
        "SELECT id FROM f07_orders WHERE amount > :minimum",
        {"minimum": "0 OR 1=1"},
    ) == []
