"""Behavior-focused SQL binding and lifecycle checks."""

import pytest

from aurora.aurorasql import connect
from aurora.aurorasql.core import _compile_sql


def test_sql_binding_ignores_literals_comments_casts_and_dollar_quotes():
    sql = ("SELECT :value, ':ignored', \"x:ignored\", `x:ignored`, "
           "$$:ignored$$, $tag$:ignored$tag$, :value::integer "
           "/* :ignored */ -- :ignored\n")
    statement, values = _compile_sql(sql, {"value": 5}, "pyformat")
    assert statement.count("%(value)s") == 2
    assert "':ignored'" in statement
    assert "$$:ignored$$" in statement
    assert "$tag$:ignored$tag$" in statement
    assert "::integer" in statement
    assert values == {"value": 5}


def test_sql_binding_validation_and_percent_literals():
    statement, values = _compile_sql("SELECT 100 % 3, :x, 'it''s :not_a_bind'", {"x": 2}, "pyformat")
    assert "100 %% 3" in statement
    assert values == {"x": 2}
    with pytest.raises(ValueError, match="Missing SQL parameter"):
        _compile_sql("SELECT :missing", {}, "named")
    with pytest.raises(ValueError, match="Unterminated"):
        _compile_sql("SELECT 'unfinished", {}, "named")


def test_sqlite_workflow_and_connection_cleanup():
    with connect("sqlite:///:memory:") as db:
        db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        with db.transaction():
            db.execute("INSERT INTO items(name) VALUES (:name)", {"name": "a:b"})
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO items(name) VALUES ('rollback')")
                raise RuntimeError("abort")
        assert db.execute("SELECT name FROM items") == [{"name": "a:b"}]
    with pytest.raises(RuntimeError, match="closed"):
        db.execute("SELECT 1")
