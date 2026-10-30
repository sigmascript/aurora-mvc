"""Opt-in coordination checks against disposable MySQL and PostgreSQL.

Uses a unique isolated journal and table. No application history is touched.
Lock checks require two simultaneous physical connections.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import (
    Model, column, connect, execute_create_recorded, initialize_history,
    inspect_table, list_history, prepare_create,
)
from aurora.aurorasql import history as history_module
from aurora.aurorasql.coordination import migration_lock


_BACKENDS = (
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
)


def _test_url(environment):
    if os.environ.get("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Live DDL checks require explicit disposable-database opt-in")
    url = os.environ.get(environment)
    if not url:
        pytest.skip(f"{environment} not configured")
    return url


@pytest.mark.parametrize("dialect,environment", _BACKENDS)
def test_live_competing_connections_and_recorded_create(dialect, environment, monkeypatch):
    url = _test_url(environment)
    suffix = uuid4().hex[:16]
    journal = "aurorasql_lockhist_" + suffix
    table = "aurorasql_lockprobe_" + suffix
    Probe = type("Probe", (Model,), {
        "__table__": table,
        "id": column(int, primary_key=True),
        "title": column(str, length=100),
    })
    monkeypatch.setattr(history_module, "_HISTORY_TABLE", journal)
    quote = "`" if dialect == "mysql" else '"'
    created = []
    with connect(url) as first, connect(url) as second:
        assert first.dialect == second.dialect == dialect
        assert not inspect_table(first, journal).exists
        assert not inspect_table(first, table).exists
        try:
            initialize_history(first, confirm=True)
            created.append(journal)
            approval = prepare_create(second, Probe)
            with migration_lock(first):
                with pytest.raises(RuntimeError, match="lock unavailable"):
                    execute_create_recorded(second, Probe, approval, confirm=True)
                assert not list_history(first)
                assert not inspect_table(first, table).exists
            operation_id = execute_create_recorded(second, Probe, approval, confirm=True)
            created.append(table)
            entries = list_history(first)
            assert len(entries) == 1
            assert entries[0].operation_id == operation_id
            assert entries[0].status == "applied"
        finally:
            # Clean only the unique tables belonging to this test.
            for name in reversed(created):
                first.execute_write(f"DROP TABLE {quote}{name}{quote}")


@pytest.mark.parametrize("dialect,environment", _BACKENDS)
def test_live_lock_released_on_python_exception(dialect, environment):
    url = _test_url(environment)
    with connect(url) as first, connect(url) as second:
        with pytest.raises(ValueError, match="intentional test failure"):
            with migration_lock(first):
                with pytest.raises(RuntimeError, match="lock unavailable"):
                    with migration_lock(second):
                        pass
                raise ValueError("intentional test failure")
        # The other physical connection must now be able to acquire the lock.
        with migration_lock(second):
            pass
