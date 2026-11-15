"""Opt-in, rollback-only PostgreSQL integration checks.

Run with AURORASQL_PG_TEST_URL pointing exclusively at aurorasql_test.
No successful DDL is committed by these tests.
"""

import os
from uuid import uuid4
from urllib.parse import unquote, urlsplit

import pytest

from aurora.database.core import connect
from aurora.database.coordination import _PG_LOCK_KEY


def _test_url():
    url = os.environ.get("AURORASQL_PG_TEST_URL")

    if not url:
        pytest.skip("Set AURORASQL_PG_TEST_URL to run live PostgreSQL checks")

    parsed = urlsplit(url)

    if parsed.scheme != "postgresql+psycopg" or unquote(parsed.path.lstrip("/")) != "aurorasql_test":
        pytest.fail("Live test URL must target the dedicated aurorasql_test database")

    return url


def test_postgres_transactional_ddl_rolls_back():
    url = _test_url()
    table = "aurora_live_" + uuid4().hex

    with connect(url) as connection:
        assert connection.dialect == "postgresql"

        with pytest.raises(RuntimeError, match="intentional rollback"):
            with connection.transaction():
                locked = connection.fetch_first(
                    "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
                )
                assert locked and locked["acquired"] is True

                connection.execute_write(f'CREATE TABLE "{table}" (id BIGINT PRIMARY KEY)')
                assert connection.fetch_first(
                    "SELECT to_regclass(:name) IS NOT NULL AS present", {"name": table}
                )["present"] is True

                raise RuntimeError("intentional rollback")

        assert connection.fetch_first(
            "SELECT to_regclass(:name) IS NOT NULL AS present", {"name": table}
        )["present"] is False


def test_postgres_transaction_lock_is_released_on_rollback():
    url = _test_url()

    with connect(url) as first, connect(url) as second:
        with pytest.raises(RuntimeError, match="rollback"):
            with first.transaction():
                assert first.fetch_first(
                    "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
                )["acquired"] is True

                with second.transaction():
                    assert second.fetch_first(
                        "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
                    )["acquired"] is False

                raise RuntimeError("rollback")

        with second.transaction():
            assert second.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )["acquired"] is True
