"""Generic explicit join-chain and projection contracts."""

import pytest

from aurora.aurorasql import Model, column, connect


class Writer(Model):
    __table__ = "writers"
    id = column(int, primary_key=True)
    name = column(str)


class Story(Model):
    __table__ = "stories"
    id = column(int, primary_key=True)
    writer_id = column(int)
    title = column(str)


class Review(Model):
    __table__ = "reviews"
    id = column(int, primary_key=True)
    story_id = column(int)
    score = column(int)


@pytest.fixture
def database():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE writers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.execute_write("CREATE TABLE stories (id INTEGER PRIMARY KEY, writer_id INTEGER, title TEXT)")
        db.execute_write("CREATE TABLE reviews (id INTEGER PRIMARY KEY, story_id INTEGER, score INTEGER)")
        with db.transaction():
            Writer.using(db).create(id=1, name="Mira")
            Writer.using(db).create(id=2, name="Noor")
            Story.using(db).create(id=10, writer_id=1, title="One")
            Story.using(db).create(id=11, writer_id=2, title="Two")
            Review.using(db).create(id=100, story_id=10, score=4)
            Review.using(db).create(id=101, story_id=11, score=5)
        yield db


def test_three_table_chain_projection_and_order(database):
    query = (Story.using(database).legacy_query()
             .join(Writer, on=("writer_id", "id"))
             .join(Review, on=("stories.id", "story_id"))
             .select("stories.title", "writers.name", "reviews.score"))
    rows = query.order_by("reviews.score", descending=True).all()
    assert rows == [
        {"left__title": "Two", "right1__name": "Noor", "right2__score": 5},
        {"left__title": "One", "right1__name": "Mira", "right2__score": 4},
    ]
    assert query.count() == 2
    assert query.limit(1).offset(1).all() != []


def test_joined_where_and_bound_values(database):
    query = (Story.using(database).legacy_query()
             .join(Writer, on=("writer_id", "id"))
             .where_joined({"writers.name": "Mira"}))
    assert query.count() == 1
    assert query.first()["left__title"] == "One"
    assert (Story.using(database).legacy_query()
            .join(Writer, on=("writer_id", "id"))
            .where_joined({"writers.name": "Mira' OR 1=1 --"}).count()) == 0


def test_reject_unknown_and_duplicate_columns(database):
    query = Story.using(database).legacy_query().join(Writer, on=("writer_id", "id"))
    with pytest.raises(ValueError):
        query.select("writers.nonexistent")
    with pytest.raises(ValueError):
        query.select("stories.id", "stories.id")
    with pytest.raises(ValueError):
        query.order_by("writers.name;DROP")
    with pytest.raises(ValueError):
        query.join(Review, on=("writers.missing", "story_id"))
