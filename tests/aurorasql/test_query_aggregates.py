"""Focused query count and existence behavior."""

from aurora.aurorasql import Model, column, connect


class Ticket(Model):
    __table__ = "tickets"
    id = column(int, primary_key=True)
    status = column(str)


def test_count_and_exists_are_filtered_and_ignore_pagination():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
        tickets = Ticket.using(db)
        with db.transaction():
            tickets.create(id=1, status="open")
            tickets.create(id=2, status="open")
            tickets.create(id=3, status="closed")

        query = tickets.legacy_query({"status": "open"}).order_by("id").limit(1).offset(1)
        assert query.count() == 2
        assert query.exists() is True
        assert [ticket.id for ticket in query.all()] == [2]
        assert tickets.legacy_query({"status": "missing"}).count() == 0
        assert tickets.legacy_query({"status": "missing"}).exists() is False
        assert tickets.legacy_query().count() == 3
        assert tickets.legacy_query().exists() is True


def test_count_and_exists_on_empty_table():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
        query = Ticket.using(db).legacy_query()
        assert query.count() == 0
        assert query.exists() is False
