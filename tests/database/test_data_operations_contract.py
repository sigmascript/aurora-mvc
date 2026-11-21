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
            Customer.using(connection).create({"id": 1, "name": "Ada"})
            Customer.using(connection).create({"id": 2, "name": "Lin"})
            Order.using(connection).create({"id": 10, "customer_id": 1, "amount": 15})
            Order.using(connection).create({"id": 11, "customer_id": 2, "amount": 30})
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


def test_create_accepts_mapping_and_returns_plain_dict(db):
    customers = Customer.using(db)

    with db.transaction():
        created = customers.create({"name": "Morgan"})
        second = customers.create({"name": "Robin"})

    assert type(created) is dict
    assert isinstance(created["id"], int)
    assert customers.read({"id": created["id"]}) == [
        {"id": created["id"], "name": "Morgan"},
    ]
    assert customers.read({"id": second["id"]}) == [
        {"id": second["id"], "name": "Robin"},
    ]

    with Model.connection_scope(db):
        scoped = Customer.create({"name": "Taylor"})

    assert customers.read({"id": scoped["id"]}) == [
        {"id": scoped["id"], "name": "Taylor"},
    ]


def test_create_rejects_invalid_or_ambiguous_input_without_writing(db):
    customers = Customer.using(db)
    original_rows = customers.read()

    with pytest.raises(TypeError, match="dictionary or mapping"):
        customers.create([("name", "Morgan")])

    with pytest.raises(TypeError):
        customers.create({"name": "Morgan"}, name="Robin")

    with pytest.raises(TypeError):
        customers.create(name="Robin")

    with pytest.raises(TypeError):
        customers.create(data={"name": "Robin"})

    with pytest.raises(TypeError):
        customers.create()

    with pytest.raises(TypeError, match="Unknown column"):
        customers.create({"unknown": "Morgan"})

    with Model.connection_scope(db):
        with pytest.raises(TypeError):
            Customer.create({"name": "Morgan"}, id=99)

    assert customers.read() == original_rows


def test_create_data_named_column_and_positional_mapping_only(db):
    class Payload(Model):
        __table__ = "f07_payload"
        id = column(int, primary_key=True)
        data = column(str)

    db.execute_write("CREATE TABLE f07_payload (id INTEGER PRIMARY KEY, data TEXT)")
    source = {"data": "AIO value"}
    with db.transaction():
        created = Payload.using(db).create(source)
    assert type(created) is dict
    assert created["data"] == "AIO value"
    assert source == {"data": "AIO value"}
    assert Payload.using(db).read().first()["data"] == "AIO value"
    with pytest.raises(TypeError):
        Payload.using(db).create(data="AIO value")
    with Model.connection_scope(db):
        with pytest.raises(TypeError):
            Payload.create(data={"data": "AIO value"})
    assert Payload.using(db).read().count() == 1


def test_create_accepts_empty_mapping_when_table_has_defaults(db):
    class DefaultOnly(Model):
        __table__ = "f07_defaults"
        id = column(int, primary_key=True)

    db.execute_write("CREATE TABLE f07_defaults (id INTEGER PRIMARY KEY)")
    created = DefaultOnly.using(db).create({})
    assert type(created) is dict
    assert created["id"] == 1
