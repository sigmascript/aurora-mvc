"""Essential Legacy comparison flags remain bound, validated and safe."""

import pytest

from aurora.aurorasql import Model, column, connect


class Item(Model):
    __table__ = "filter_items"
    id = column(int, primary_key=True)
    title = column(str)
    score = column(int)
    group_name = column(str, nullable=True)


class Group(Model):
    __table__ = "filter_groups"
    id = column(int, primary_key=True)
    name = column(str)


@pytest.fixture
def database():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE filter_items (id INTEGER PRIMARY KEY, title TEXT, score INTEGER, group_name TEXT)")
        db.execute("CREATE TABLE filter_groups (id INTEGER PRIMARY KEY, name TEXT)")
        for row in ((1, "alpha", 10, None), (2, "beta", 20, "work"), (3, "alphabet", 30, "work")):
            Item.using(db).create(id=row[0], title=row[1], score=row[2], group_name=row[3])
        Group.using(db).create(id=1, name="one")
        yield db


def test_legacy_flags_and_join(database):
    items = Item.using(database)
    assert [x.id for x in items.legacy_query({"score--gt": 10}).all()] == [2, 3]
    assert [x.id for x in items.legacy_query({"score--ge": 20}).all()] == [2, 3]
    assert [x.id for x in items.legacy_query({"score--lt": 20}).all()] == [1]
    assert [x.id for x in items.legacy_query({"score--le": 20}).all()] == [1, 2]
    assert [x.id for x in items.legacy_query({"title--like": "alpha%"}).all()] == [1, 3]
    assert [x.id for x in items.legacy_query({"title--not-like": "alpha%"}).all()] == [2]
    assert [x.id for x in items.legacy_query({"id--in": [1, 3]}).all()] == [1, 3]
    assert [x.id for x in items.legacy_query({"id--not-in": [1, 3]}).all()] == [2]
    assert [x.id for x in items.legacy_query({"score--between": [10, 20]}).all()] == [1, 2]
    assert [x.id for x in items.legacy_query({"score--not-between": [10, 20]}).all()] == [3]
    assert [x.id for x in items.legacy_query({"group_name--ne": None}).all()] == [2, 3]
    assert items.legacy_query({"score--gt": 10}).join(Group, on=("id", "id")).count() == 0


def test_write_filters_and_safe_parameters(database):
    items = Item.using(database)
    assert items.update(values={"title": "changed"}, where={"score--gt": 20}) == 1
    assert items.delete(where={"score--lt": 15}) == 1
    assert [x.id for x in items.all()] == [2, 3]
    assert items.legacy_query({"title--like": "%\' OR 1=1 --"}).all() == []
    assert items.legacy_query({"id--in": [2, 3]}).count() == 2


@pytest.mark.parametrize("where,error", [
    ({"score--invalid": 1}, ValueError),
    ({"not_a_column--gt": 1}, TypeError),
    ({"score--gt": "1 OR 1=1"}, TypeError),
    ({"score--in": []}, ValueError),
    ({"score--in": [1, None]}, ValueError),
    ({"score--between": [1]}, ValueError),
    ({"score--gt": None}, ValueError),
    ({"score--like": "2%"}, TypeError),
    ({"id--in": {1, 2}}, TypeError),
])
def test_reject_invalid_filters(database, where, error):
    with pytest.raises(error):
        Item.using(database).legacy_query(where)
