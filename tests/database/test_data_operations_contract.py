"""Essential current AuroraSQL CRUD, JOIN, and parameter-safety contracts."""

from datetime import date, datetime
from decimal import Decimal

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
        connection.execute_write(
            "CREATE TABLE f07_orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER)"
        )
        with connection.transaction():
            Customer.using(connection).create({"id": 1, "name": "Ada"})
            Customer.using(connection).create({"id": 2, "name": "Lin"})
            Order.using(connection).create({"id": 10, "customer_id": 1, "amount": 15})
            Order.using(connection).create({"id": 11, "customer_id": 2, "amount": 30})
        yield connection


def test_filtered_crud_and_whole_table_write_boundary(db):
    orders = Order.using(db)
    assert orders.read(
        {"$or": [{"amount--lt": 20}, {"customer_id": 2}]}, order_by={"id": "ASC"}
    ) == [
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
        models=[Customer],
        cols=["Order.amount", "Customer.name"],
        where={"Customer.name": "Ada"},
    )
    assert rows == [{"left__amount": 15, "right__name": "Ada"}]
    assert Order.using(db).query(
        "SELECT id FROM f07_orders WHERE amount > :minimum ORDER BY id",
        {"minimum": 20},
    ) == [{"id": 11}]
    assert (
        Order.using(db).query(
            "SELECT id FROM f07_orders WHERE amount > :minimum",
            {"minimum": "0 OR 1=1"},
        )
        == []
    )


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


def test_join_normalizes_declared_fields_and_preserves_projection_lookup(db):
    class Detail(Model):
        __table__ = "f07_details"
        id = column(int, primary_key=True)
        order_id = column(int, reference="f07_orders.id")
        enabled = column(bool)
        optional = column(bool, not_null=False)
        quantity = column(int)
        price = column("DECIMAL(8,2)")
        day = column(date)
        created = column(datetime)

    db.execute_write(
        "CREATE TABLE f07_details (id INTEGER PRIMARY KEY, order_id INTEGER, "
        "enabled INTEGER, optional INTEGER, quantity INTEGER, price NUMERIC, "
        "day TEXT, created TEXT)"
    )
    db.execute_write(
        "INSERT INTO f07_details VALUES "
        "(1, 10, 1, NULL, 1, 12.25, '2026-11-25', '2026-11-25T10:00:00'), "
        "(2, 11, 0, 1, 0, 0.5, '2026-11-26', '2026-11-26T10:00:00')"
    )
    details = Detail.using(db)
    expected = details.read(order_by={"id": "ASC"})
    fields = ("enabled", "optional", "quantity", "price", "day", "created")
    rows = Order.using(db).join(
        [Detail, Customer],
        cols=["amount", *[f"Detail.{field}" for field in fields], "Customer.name"],
        order_by={"Order.id": "ASC"},
    )
    assert len(rows) == len(expected) == 2
    assert rows.sum("Order.amount") == 45
    assert rows.sum("f07_orders.amount") == 45
    for row, direct in zip(rows, expected):
        for field in fields:
            value = row[f"right1__{field}"]
            assert value == direct[field]
            assert type(value) is type(direct[field])
    assert rows[0]["right1__enabled"] is True
    assert rows[1]["right1__enabled"] is False
    assert rows[0]["right1__optional"] is None
    assert type(rows[0]["right1__quantity"]) is int
    assert rows[0]["right1__price"] == Decimal("12.25")
    assert rows[0]["right1__day"] == date(2026, 11, 25)
    assert rows[0]["right1__created"] == datetime(2026, 11, 25, 10)
    query = Order.using(db).legacy_query().join(Detail, on=("id", "order_id"))
    query = query.select("f07_details.enabled", "f07_details.optional", "amount")
    query = query.order_by("id")
    assert query.first()["right__enabled"] is True
    assert query.first()["right__optional"] is None
    assert query.all()[1]["right__enabled"] is False
    full_rows = Order.using(db).join(Detail, order_by={"Order.id": "ASC"})
    assert full_rows[0]["right__enabled"] is True
    assert full_rows[1]["right__enabled"] is False
    assert full_rows.sum("Detail.price") == Decimal("12.75")
    with pytest.raises(TypeError, match="numeric column"):
        full_rows.sum("Detail.enabled")
    with pytest.raises(ValueError, match="Unknown or unavailable aggregate column"):
        rows.sum("Detail.id")
    assert type(details.query("SELECT enabled FROM f07_details WHERE id=1")[0]["enabled"]) is int
