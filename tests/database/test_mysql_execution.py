"""MySQL recorded DDL failure handling, without requiring a server."""

from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from aurora.database import cli_init, mysql_init
from aurora.database import server_migration_timeline
from tests.database.test_schema import Entry


class FakeConnection:
    dialect = "mysql"
    _transaction_active = False

    def __init__(self):
        self.events = []
        self.tables = set()
        self.status = None

    def _ensure_open(self):
        pass

    def fetch_first(self, sql, parameters=None):
        return {"database_name": "aurorasql_test", "server_name": "localhost", "server_port": 3306}

    def execute_write(self, sql, parameters=None):
        if "INSERT INTO" in sql:
            self.status = "started"

        if "UPDATE" in sql:
            self.status = parameters.get("status", parameters.get("state"))

        self.events.append((sql, parameters))
        return 1


def _prepare(monkeypatch, connection):
    monkeypatch.setattr(mysql_init, "migration_lock", lambda conn: nullcontext())
    monkeypatch.setattr(mysql_init, "inspect_table", lambda conn, name:
                        SimpleNamespace(exists=name in conn.tables))
    monkeypatch.setattr(mysql_init, "initialize_history", lambda conn, confirm:
                        conn.tables.add("aurorasql_schema_history"))
    monkeypatch.setattr(mysql_init, "review_history", lambda conn: ())
    monkeypatch.setattr(mysql_init, "prepare_server_initial", lambda *args, **kwargs: None)
    monkeypatch.setattr(mysql_init, "verify_server_initial_schema", lambda *args: "a" * 64)
    monkeypatch.setattr(mysql_init, "complete_server_initial", lambda *args: None)
    monkeypatch.setattr(mysql_init, "mark_server_initial_uncertain", lambda *args: None)
    monkeypatch.setattr(cli_init, "_mysql_read_only_preflight", lambda *args: {
        "status": "EMPTY", "review_warnings": [],
    })
    parsed = urlsplit("mysql+pymysql://localhost/aurorasql_test")
    identity = cli_init._mysql_database_identity(connection, parsed)
    digest = cli_init._mysql_approval_digest((Entry,), "default", identity)
    return dict(approval=digest, expected_approval=digest,
                expected_identity=identity, parsed=parsed)


def test_mysql_post_ddl_failure_is_uncertain(monkeypatch):
    connection = FakeConnection()
    kwargs = _prepare(monkeypatch, connection)

    def failed_ddl(conn, model, token, *, confirm):
        conn.tables.add(model.__table_name__)
        raise RuntimeError("failure after implicit commit")

    monkeypatch.setattr(mysql_init, "execute_create", failed_ddl)

    with pytest.raises(RuntimeError, match="implicit commit"):
        mysql_init.execute_mysql_init(connection, (Entry,), **kwargs)

    assert connection.status == "uncertain"

    assert "entry" in connection.tables

    assert any("INSERT INTO" in sql for sql, _ in connection.events)


def test_mysql_success_marks_applied(monkeypatch):
    connection = FakeConnection()
    kwargs = _prepare(monkeypatch, connection)
    monkeypatch.setattr(mysql_init, "execute_create", lambda conn, model, token, confirm:
                        conn.tables.add(model.__table_name__))
    ids = mysql_init.execute_mysql_init(connection, (Entry,), **kwargs)
    assert len(ids) == 1

    assert connection.status == "applied"


def test_mysql_refuses_wrong_identity_before_journal(monkeypatch):
    connection = FakeConnection()
    kwargs = _prepare(monkeypatch, connection)
    kwargs["expected_identity"] = "wrong-server"

    with pytest.raises(RuntimeError, match="changed after approval"):
        mysql_init.execute_mysql_init(connection, (Entry,), **kwargs)

    assert not connection.events


def test_mysql_native_intent_is_prepared_before_ddl_and_completed_after_verification(monkeypatch):
    from contextlib import contextmanager

    connection = FakeConnection()
    kwargs = _prepare(monkeypatch, connection)

    @contextmanager
    def held_lock(conn):
        conn._migration_lock_active = True

        try:
            yield
        finally:
            conn._migration_lock_active = False

    monkeypatch.setattr(mysql_init, "migration_lock", held_lock)
    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_:
                        SimpleNamespace(exists=False))
    monkeypatch.setattr(mysql_init, "prepare_server_initial",
                        server_migration_timeline.prepare_server_initial)
    monkeypatch.setattr(mysql_init, "complete_server_initial",
                        server_migration_timeline.complete_server_initial)
    monkeypatch.setattr(mysql_init, "mark_server_initial_uncertain",
                        server_migration_timeline.mark_server_initial_uncertain)
    monkeypatch.setattr(mysql_init, "execute_create", lambda conn, model, token, confirm:
                        conn.tables.add(model.__table_name__))
    monkeypatch.setattr(mysql_init, "verify_server_initial_schema", lambda conn, models: "b" * 64)

    assert len(mysql_init.execute_mysql_init(connection, (Entry,), **kwargs)) == 1
    native_inserts = [(index, params) for index, (sql, params) in enumerate(connection.events)
                      if "aurorasql_migration_timeline" in sql and "INSERT INTO" in sql]
    native_updates = [(index, params) for index, (sql, params) in enumerate(connection.events)
                      if "aurorasql_migration_timeline" in sql and "UPDATE" in sql]
    assert len(native_inserts) == 1
    assert native_inserts[0][1]["state"] == "prepared"
    assert native_updates[0][0] > native_inserts[0][0]
    assert native_updates[0][1]["state"] == "applied"
    assert native_updates[0][1]["schema"] == "b" * 64


def test_mysql_postcondition_failure_leaves_native_initial_uncertain(monkeypatch):
    from contextlib import contextmanager

    connection = FakeConnection()
    kwargs = _prepare(monkeypatch, connection)

    @contextmanager
    def held_lock(conn):
        conn._migration_lock_active = True

        try:
            yield
        finally:
            conn._migration_lock_active = False

    monkeypatch.setattr(mysql_init, "migration_lock", held_lock)
    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_:
                        SimpleNamespace(exists=False))
    monkeypatch.setattr(mysql_init, "prepare_server_initial",
                        server_migration_timeline.prepare_server_initial)
    monkeypatch.setattr(mysql_init, "mark_server_initial_uncertain",
                        server_migration_timeline.mark_server_initial_uncertain)
    monkeypatch.setattr(mysql_init, "execute_create", lambda conn, model, token, confirm:
                        conn.tables.add(model.__table_name__))
    monkeypatch.setattr(mysql_init, "verify_server_initial_schema", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("catalog mismatch")))

    with pytest.raises(RuntimeError, match="catalog mismatch"):
        mysql_init.execute_mysql_init(connection, (Entry,), **kwargs)

    native_updates = [params for sql, params in connection.events
                      if "aurorasql_migration_timeline" in sql and "UPDATE" in sql]
    assert len(native_updates) == 1
    assert native_updates[0]["state"] == "uncertain"
    assert "entry" in connection.tables
