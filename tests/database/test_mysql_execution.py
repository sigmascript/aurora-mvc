"""MySQL recorded DDL failure handling, without requiring a server."""

from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from aurora.database import cli_init, mysql_init
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
            self.status = parameters["status"]

        self.events.append((sql, parameters))
        return 1


def _prepare(monkeypatch, connection):
    monkeypatch.setattr(mysql_init, "migration_lock", lambda conn: nullcontext())
    monkeypatch.setattr(mysql_init, "inspect_table", lambda conn, name:
                        SimpleNamespace(exists=name in conn.tables))
    monkeypatch.setattr(mysql_init, "initialize_history", lambda conn, confirm:
                        conn.tables.add("aurorasql_schema_history"))
    monkeypatch.setattr(mysql_init, "review_history", lambda conn: ())
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
