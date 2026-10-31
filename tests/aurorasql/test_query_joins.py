"""Focused real-world JOIN workflow tests using disposable in-memory SQLite."""

import pytest

from aurora.aurorasql import Model, column, connect


class Author(Model):
    __table__ = "authors"
    id = column(int, primary_key=True)
    name = column(str)


class Article(Model):
    __table__ = "articles"
    id = column(int, primary_key=True)
    author_id = column(int)
    title = column(str)


def test_join_filters_pagination_and_aggregates():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.execute_write("CREATE TABLE articles (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT)")
        with db.transaction():
            Author.using(db).create(id=1, name="Ada")
            Author.using(db).create(id=2, name="Lin")
            Article.using(db).create(id=10, author_id=1, title="First")
            Article.using(db).create(id=11, author_id=1, title="Second")
            Article.using(db).create(id=12, author_id=99, title="Orphan")
        joined = Article.using(db).legacy_query().join(Author, on=("author_id", "id"))
        assert joined.count() == 2
        assert joined.exists()
        assert [row["right__name"] for row in joined.order_by("id").all()] == ["Ada", "Ada"]
        assert joined.order_by("id", descending=True).limit(1).first() == {
            "left__id": 11, "left__author_id": 1, "left__title": "Second",
            "right__id": 1, "right__name": "Ada",
        }
        assert joined.order_by("id").limit(1).offset(1).all()[0]["left__id"] == 11
        assert Article.using(db).legacy_query({"title": "Orphan"}).join(
            Author, on=("author_id", "id")).exists() is False


def test_join_rejects_unsafe_models_and_columns():
    with connect("sqlite:///:memory:") as db:
        query = Article.using(db).legacy_query()
        with pytest.raises(ValueError, match="Join columns"):
            query.join(Author, on=("author_id", "id; DROP TABLE authors"))
        with pytest.raises(ValueError, match="itself"):
            query.join(Article, on=("id", "id"))
        with pytest.raises(TypeError, match="Model"):
            query.join(object, on=("id", "id"))
        with pytest.raises(ValueError, match="Only one join"):
            query.join(Author, on=("author_id", "id")).join(Author, on=("author_id", "id"))
