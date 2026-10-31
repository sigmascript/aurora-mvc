"""Legacy database.query(...).fetchall() compatibility and binding."""

import pytest

from aurora.aurorasql import connect


def test_database_query_fetchall_and_fetchone():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE category (id INTEGER PRIMARY KEY, title TEXT)")
        db.query("INSERT INTO category (title) VALUES (:title)", {"title": "News"})
        db.query("INSERT INTO category (title) VALUES (:title)", {"title": "Sports"})
        result = db.query(
            "SELECT id, title FROM category WHERE id >= :minimum ORDER BY id",
            {"minimum": 1},
        )
        assert result.fetchone() == {"id": 1, "title": "News"}
        assert result.fetchall() == [{"id": 2, "title": "Sports"}]
        assert result.fetchall() == []
        assert db.query("SELECT id FROM category WHERE id = :id", {"id": 99}).fetchone() is None


def test_database_query_write_rowcount_and_fetch_rejection():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE category (id INTEGER PRIMARY KEY, title TEXT)")
        result = db.query("INSERT INTO category (title) VALUES (:title)", {"title": "News"})
        assert result.rowcount == 1
        with pytest.raises(ValueError, match="did not return rows"):
            result.fetchall()


def test_database_query_bindings_prevent_value_injection():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE category (id INTEGER PRIMARY KEY, title TEXT)")
        db.query("INSERT INTO category (title) VALUES (:title)", {"title": "News"})
        malicious = "News' OR 1=1 --"
        assert db.query(
            "SELECT id FROM category WHERE title = :title", {"title": malicious}
        ).fetchall() == []
        with pytest.raises(ValueError, match="Missing SQL parameter"):
            db.query("SELECT id FROM category WHERE title = :title")


def test_database_query_aggregation_with_bound_limit():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE blog_category (id INTEGER PRIMARY KEY, category TEXT)")
        db.execute("CREATE TABLE blog (id INTEGER PRIMARY KEY, category_id INTEGER, lang_id INTEGER, visits INTEGER)")
        db.query("INSERT INTO blog_category VALUES (1, :name)", {"name": "News"})
        db.query("INSERT INTO blog VALUES (1, 1, :lang, :visits)", {"lang": 2, "visits": 5})
        db.query("INSERT INTO blog VALUES (2, 1, :lang, :visits)", {"lang": 2, "visits": 7})
        rows = db.query(
            "SELECT c.category AS title, SUM(b.visits) AS visits "
            "FROM blog b INNER JOIN blog_category c ON b.category_id = c.id "
            "WHERE b.lang_id = :lang GROUP BY c.category "
            "ORDER BY visits DESC LIMIT :limit",
            {"lang": 2, "limit": 10},
        ).fetchall()
        assert rows == [{"title": "News", "visits": 12}]
