"""Opt-in live MySQL acceptance against an explicitly dedicated empty scratch DB.

Set AURORASQL_MYSQL_TEST_URL to mysql+pymysql://.../aurorasql_alpha9_scratch.
Only known test-created tables are removed; an initially nonempty DB is refused.
"""

import os
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

import pytest

from aurora.database.core import connect
from aurora.database.init_validation import validate_init_models
from aurora.database.mysql_init import execute_mysql_init
from aurora.database import cli_init
from tests.database.test_schema import Entry
from tests.database.test_alpha9_init_validation import Parent, Child


@contextmanager
def _scratch():
    url = os.environ.get("AURORASQL_MYSQL_TEST_URL")
    if not url:
        pytest.skip("AURORASQL_MYSQL_TEST_URL not configured")
    parsed = urlsplit(url)
    if (parsed.scheme != "mysql+pymysql" or
            unquote(parsed.path.lstrip("/")) != "aurorasql_alpha9_scratch"):
        pytest.fail("MySQL live tests require the dedicated aurorasql_alpha9_scratch database")
    with connect(url) as connection:
        tables = connection.fetch_all(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
        )
        if tables:
            pytest.fail("MySQL scratch database must be empty before each test")
        try:
            yield connection, parsed
        finally:
            # The initial empty-schema assertion establishes ownership of these
            # known test tables. Drop children first; no unknown tables touched.
            for table in ("alpha9_child", "alpha9_parent", "entry", "aurorasql_schema_history"):
                connection.execute_write(f"DROP TABLE IF EXISTS `{table}`")


def _approve(connection, parsed, models):
    identity = cli_init._mysql_database_identity(connection, parsed)
    digest = cli_init._mysql_approval_digest(models, "default", identity)
    return dict(approval=digest, expected_approval=digest, alias="default",
                expected_identity=identity, parsed=parsed)


def test_live_mysql_parent_child_and_replay_refusal():
    with _scratch() as (connection, parsed):
        models = validate_init_models((Child, Parent), dialect="mysql")
        ids = execute_mysql_init(connection, models, **_approve(connection, parsed, models))
        assert len(ids) == 2
        statuses = connection.fetch_all(
            "SELECT table_name, status FROM aurorasql_schema_history ORDER BY table_name"
        )
        assert [(row["table_name"], row["status"]) for row in statuses] == [
            ("alpha9_child", "applied"), ("alpha9_parent", "applied")]
        with pytest.raises(Exception):
            connection.execute_write(
                "INSERT INTO `alpha9_child` (`id`, `parent_id`) VALUES (1, 999)"
            )
        with pytest.raises(RuntimeError, match="not empty"):
            execute_mysql_init(connection, models, **_approve(connection, parsed, models))


def test_live_mysql_failure_keeps_uncertain_journal(monkeypatch):
    from aurora.database import mysql_init
    original = mysql_init.execute_create

    def fail_after_ddl(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected post-DDL failure")

    with _scratch() as (connection, parsed):
        with monkeypatch.context() as patch:
            patch.setattr(mysql_init, "execute_create", fail_after_ddl)
            with pytest.raises(RuntimeError, match="injected post-DDL failure"):
                execute_mysql_init(connection, (Entry,),
                                   **_approve(connection, parsed, (Entry,)))
        status = connection.fetch_first(
            "SELECT status FROM aurorasql_schema_history WHERE table_name='entry'"
        )
        assert status["status"] == "uncertain"
        with pytest.raises(RuntimeError, match="not empty"):
            execute_mysql_init(connection, (Entry,),
                               **_approve(connection, parsed, (Entry,)))
