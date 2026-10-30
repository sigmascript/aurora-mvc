"""Compatibility and fail-closed contracts for explicit JOIN queries."""

import pytest

from aurora.aurorasql import Model, column, connect


class Member(Model):
    __table__ = "members"
    id = column(int, primary_key=True)
    name = column(str)


class Task(Model):
    __table__ = "tasks"
    id = column(int, primary_key=True)
    member_id = column(int)
    title = column(str)


class Label(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    task_id = column(int)
    value = column(str)


@pytest.fixture
def database():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute_write("CREATE TABLE tasks (id INTEGER PRIMARY KEY, member_id INTEGER, title TEXT)")
        db.execute_write("CREATE TABLE labels (id INTEGER PRIMARY KEY, task_id INTEGER, value TEXT)")
        with db.transaction():
            Member.using(db).create(id=1, name="A")
            Member.using(db).create(id=2, name="B")
            Task.using(db).create(id=10, member_id=1, title="First")
            Task.using(db).create(id=11, member_id=2, title="Second")
            Label.using(db).create(id=100, task_id=10, value="urgent")
            Label.using(db).create(id=101, task_id=11, value="later")
        yield db


def test_two_table_result_keys_remain_compatible(database):
    rows = (Task.using(database).read()
            .join(Member, on=("member_id", "id"))
            .order_by("id").all())
    assert rows == [
        {"left__id": 10, "left__member_id": 1, "left__title": "First", "right__id": 1, "right__name": "A"},
        {"left__id": 11, "left__member_id": 2, "left__title": "Second", "right__id": 2, "right__name": "B"},
    ]


def test_three_table_join_qualified_filters_and_projection(database):
    query = (Task.using(database).read()
             .join(Member, on=("member_id", "id"))
             .join(Label, on=("tasks.id", "task_id"))
             .where_joined({"members.name": "A", "labels.value--like": "urg%"})
             .select("tasks.title", "members.name", "labels.value"))
    assert query.count() == 1
    assert query.exists()
    assert query.all() == [{"left__title": "First", "right1__name": "A", "right2__value": "urgent"}]


def test_invalid_join_fields_fail_closed(database):
    query = Task.using(database).read().join(Member, on=("member_id", "id"))
    with pytest.raises((ValueError, TypeError)):
        query.where_joined({"members.name; DROP TABLE members": "A"})
    with pytest.raises((ValueError, TypeError)):
        query.select("members.secret")
    with pytest.raises((ValueError, TypeError)):
        query.order_by("members.secret")
    with pytest.raises((ValueError, TypeError)):
        query.join(Label, on=("members.secret", "task_id"))
