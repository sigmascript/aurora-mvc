"""Opt-in history/recovery integration against disposable MySQL and PostgreSQL.

Only uses uniquely named tables; never modifies the standard AuroraSQL history
journal or any pre-existing application table. DDL cleanup is best-effort.
"""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import (
    Model, assess_recovery, column, connect, execute_create_recorded,
    initialize_history, inspect_table, list_history, prepare_create,
    review_history, review_recovery_schema, unresolved_history,
)
from aurora.aurorasql import history as history_module


@pytest.mark.parametrize("dialect,environment", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_live_recorded_history_and_recovery_readonly(dialect, environment, monkeypatch):
    if os.environ.get("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Live DDL checks require explicit disposable-database opt-in")
    url = os.environ.get(environment)
    if not url:
        pytest.skip(f"{environment} not configured")

    suffix = uuid4().hex[:16]
    journal_name = "aurorasql_hist_" + suffix
    table_name = "aurorasql_record_" + suffix
    unresolved_name = "aurorasql_pending_" + suffix
    Probe = type("Probe", (Model,), {
        "__table__": table_name,
        "id": column(int, primary_key=True),
        "title": column(str, length=100),
    })
    Pending = type("Pending", (Model,), {
        "__table__": unresolved_name,
        "id": column(int, primary_key=True),
        "title": column(str, length=100),
    })
    # Isolate the test journal from real migration history, including other tests.
    monkeypatch.setattr(history_module, "_HISTORY_TABLE", journal_name)
    quote = "`" if dialect == "mysql" else '"'

    with connect(url) as db:
        assert db.dialect == dialect
        for name in (journal_name, table_name, unresolved_name):
            assert not inspect_table(db, name).exists
        created = []
        try:
            initialize_history(db, confirm=True)
            created.append(journal_name)
            approval = prepare_create(db, Probe)
            operation_id = execute_create_recorded(db, Probe, approval, confirm=True)
            created.append(table_name)
            entries = list_history(db)
            assert len(entries) == 1
            assert entries[0].operation_id == operation_id
            assert entries[0].status == "applied"
            assert not unresolved_history(db)
            assert not review_history(db)

            # Inject only an audit record, not DDL: assessment must remain read-only.
            pending_approval = prepare_create(db, Pending)
            db.execute_write(
                f"INSERT INTO {quote}{journal_name}{quote} "
                "(operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :status)",
                {"id": str(uuid4()), "table": unresolved_name,
                 "dialect": dialect, "fingerprint": pending_approval.fingerprint,
                 "status": "uncertain"},
            )
            pending = unresolved_history(db, table=unresolved_name)
            assert len(pending) == 1
            assessment = assess_recovery(db, pending[0].operation_id, Pending)
            assert not assessment.table_exists
            assert assessment.fingerprint_matches
            assert assessment.disposition == "table_absent_manual_investigation"
            findings = review_recovery_schema(db, pending[0].operation_id, Pending)
            assert any(item.kind == "table_missing" for item in findings)
            assert any(item.kind == "unresolved" for item in review_history(db))
            assert not inspect_table(db, unresolved_name).exists
        finally:
            # Never drop a table not created by this test. If cleanup fails,
            # report the error so operators can inspect the disposable database.
            for name in reversed(created):
                db.execute_write(f"DROP TABLE {quote}{name}{quote}")
