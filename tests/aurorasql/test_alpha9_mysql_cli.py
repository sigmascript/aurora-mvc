"""MySQL CLI approval and fail-closed routing without a server."""

import json
from argparse import Namespace
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from aurora.aurorasql import cli_init
from tests.aurorasql.test_schema import Entry


def _args(approve=None, confirm=None):
    return Namespace(config="unused", database="default", model=["unused:Entry"],
                     models_config=None, approve=approve, confirm=confirm)


def _setup(monkeypatch, *, status="EMPTY"):
    connection = SimpleNamespace(dialect="mysql", fetch_first=lambda sql: {
        "database_name": "aurorasql_test", "server_name": "localhost", "server_port": 3306,
    })
    config = SimpleNamespace(
        resolve=lambda alias: "mysql+pymysql://localhost:3306/aurorasql_test",
        connect=lambda alias: nullcontext(connection),
    )
    monkeypatch.setattr(cli_init, "_load_config", lambda path: (config, None))
    monkeypatch.setattr(cli_init, "_select_models", lambda args, base_dir: (["unused:Entry"], [Entry]))
    monkeypatch.setattr(cli_init, "_mysql_read_only_preflight", lambda *args: {
        "status": status, "review_warnings": [], "read_only": True,
        "transactional_ddl": False, "write_enabled": False,
    })
    return connection


def test_mysql_review_never_writes(monkeypatch, capsys):
    _setup(monkeypatch)
    assert cli_init.run_init_apply(_args()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "EMPTY"
    assert report["write_enabled"] is False
    assert report["transactional_ddl"] is False
    assert len(report["approval_sha256"]) == 64


def test_mysql_wrong_approval_refuses_execution(monkeypatch):
    _setup(monkeypatch)
    with pytest.raises(ValueError, match="Exact schema approval"):
        cli_init.run_init_apply(_args("wrong", "APPLY"))


def test_mysql_nonempty_refuses_approval(monkeypatch, capsys):
    _setup(monkeypatch, status="REFUSED")
    assert cli_init.run_init_apply(_args()) == 3
    assert json.loads(capsys.readouterr().out)["approval_sha256"] is None


def test_mysql_delegates_only_exact_approval(monkeypatch, capsys):
    connection = _setup(monkeypatch)
    assert cli_init.run_init_apply(_args()) == 0
    digest = json.loads(capsys.readouterr().out)["approval_sha256"]
    from aurora.aurorasql import mysql_init
    calls = []
    monkeypatch.setattr(mysql_init, "execute_mysql_init", lambda conn, models, **kw: (
        calls.append((conn, models, kw)) or ("operation-1",)
    ))
    assert cli_init.run_init_apply(_args(digest, "APPLY")) == 0
    assert calls[0][0] is connection
    assert calls[0][2]["expected_identity"]
    assert json.loads(capsys.readouterr().out)["status"] == "INITIALIZED"


def test_mysql_digest_changes_with_database_identity():
    a = cli_init._mysql_approval_digest([Entry], "default", "database-A")
    b = cli_init._mysql_approval_digest([Entry], "default", "database-B")
    assert a != b


def test_mysql_identity_refuses_database_mismatch():
    from urllib.parse import urlsplit
    connection = SimpleNamespace(fetch_first=lambda sql: {
        "database_name": "wrong", "server_name": "localhost", "server_port": 3306,
    })
    with pytest.raises(RuntimeError, match="differs"):
        cli_init._mysql_database_identity(
            connection, urlsplit("mysql+pymysql://localhost/aurorasql_test")
        )
