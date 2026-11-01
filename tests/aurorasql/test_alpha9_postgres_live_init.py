"""Live, isolated PostgreSQL init acceptance tests.

Requires AURORASQL_PG_TEST_URL pointing to aurorasql_test. Every test owns a
random schema and drops only that schema; never touches public/application data.
"""

import os
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import pytest

from aurora.aurorasql.core import connect
from aurora.aurorasql.postgres_init import execute_postgres_init
from tests.aurorasql.test_schema import Entry


def _test_url():
    url = os.environ.get("AURORASQL_PG_TEST_URL")
    if not url:
        pytest.skip("AURORASQL_PG_TEST_URL not configured")
    parsed = urlsplit(url)
    if parsed.scheme != "postgresql+psycopg" or unquote(parsed.path.lstrip("/")) != "aurorasql_test":
        pytest.fail("Live initialization tests require aurorasql_test database")
    return url


@contextmanager
def _isolated_schema():
    schema = "aurora_alpha9_" + uuid4().hex
    with connect(_test_url()) as connection:
        connection.execute_write(f'CREATE SCHEMA "{schema}"')
        try:
            connection.execute_write(f'SET search_path TO "{schema}"')
            assert connection.fetch_first("SELECT current_schema() AS name")["name"] == schema
            yield connection
        finally:
            # Only our random schema is eligible for cleanup.
            connection.execute_write("RESET search_path")
            connection.execute_write(f'DROP SCHEMA "{schema}" CASCADE')


def test_live_postgres_approved_init_commits_journal_and_refuses_replay():
    with _isolated_schema() as connection:
        ids = execute_postgres_init(connection, [Entry], approval="a" * 64,
                                    expected_approval="a" * 64)
        assert len(ids) == 1
        assert connection.fetch_first(
            "SELECT to_regclass('entry') IS NOT NULL AS present"
        )["present"] is True
        assert connection.fetch_first(
            "SELECT status FROM aurorasql_schema_history"
        )["status"] == "applied"
        with pytest.raises(RuntimeError, match="not empty"):
            execute_postgres_init(connection, [Entry], approval="a" * 64,
                                  expected_approval="a" * 64)


def test_live_postgres_failed_ddl_rolls_back_journal_and_tables(monkeypatch):
    import aurora.aurorasql.postgres_init as postgres_init
    original = postgres_init.execute_create

    def fail_after_create(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure after DDL")

    with _isolated_schema() as connection:
        with monkeypatch.context() as patch:
            patch.setattr(postgres_init, "execute_create", fail_after_create)
            with pytest.raises(RuntimeError, match="injected failure"):
                execute_postgres_init(connection, [Entry], approval="b" * 64,
                                      expected_approval="b" * 64)
        assert connection.fetch_first(
            "SELECT to_regclass('entry') IS NOT NULL AS present"
        )["present"] is False
        assert connection.fetch_first(
            "SELECT to_regclass('aurorasql_schema_history') IS NOT NULL AS present"
        )["present"] is False


def test_live_postgres_foreign_key_batch_and_constraint():
    from aurora.aurorasql.init_validation import validate_init_models
    from tests.aurorasql.test_alpha9_init_validation import Parent, Child
    with _isolated_schema() as connection:
        models = validate_init_models((Child, Parent), dialect="postgresql")
        assert len(execute_postgres_init(connection, models, approval="c" * 64,
                                         expected_approval="c" * 64)) == 2
        assert [row["table_name"] for row in connection.fetch_all(
            "SELECT table_name FROM aurorasql_schema_history ORDER BY table_name"
        )] == ["alpha9_child", "alpha9_parent"]
        with pytest.raises(Exception):
            connection.execute_write(
                'INSERT INTO "alpha9_child" ("id", "parent_id") VALUES (1, 999)'
            )


def test_live_postgres_cli_review_apply_and_replay(monkeypatch, capsys):
    import json
    from argparse import Namespace
    from contextlib import nullcontext
    from aurora.aurorasql import cli_init

    url = _test_url()
    with _isolated_schema() as connection:
        config = type("TestConfig", (), {
            "resolve": lambda self, alias: url,
            "connect": lambda self, alias: nullcontext(connection),
        })()
        monkeypatch.setattr(cli_init, "_load_config", lambda path: (config, None))
        monkeypatch.setattr(cli_init, "_select_models", lambda args: (["test:Entry"], [Entry]))
        args = Namespace(config="unused", database="default", approve=None, confirm=None)
        assert cli_init.run_init_apply(args) == 0
        digest = json.loads(capsys.readouterr().out)["approval_sha256"]
        assert digest and len(digest) == 64
        args.approve, args.confirm = digest, "APPLY"
        assert cli_init.run_init_apply(args) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "INITIALIZED"
        with pytest.raises(RuntimeError, match="not empty"):
            cli_init.run_init_apply(args)
