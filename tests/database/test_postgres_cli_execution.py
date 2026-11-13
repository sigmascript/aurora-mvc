"""PostgreSQL CLI approval, database identity, and fail-closed execution."""

import json
from argparse import Namespace
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from aurora.database import cli_init
from tests.database.test_schema import Entry


def _args(approve=None, confirm=None):
    return Namespace(config="unused", database="default", model=["unused:Entry"],
                     models_config=None, approve=approve, confirm=confirm)


def _setup(monkeypatch, *, status="EMPTY"):
    connection = SimpleNamespace(
        dialect="postgresql",
        fetch_first=lambda sql: {"database_name": "aurorasql_test", "schema_name": "public"},
    )
    config = SimpleNamespace(
        resolve=lambda alias: "postgresql+psycopg://localhost:5432/aurorasql_test",
        connect=lambda alias: nullcontext(connection),
    )
    monkeypatch.setattr(cli_init, "_load_config", lambda path: (config, None))
    monkeypatch.setattr(cli_init, "_select_models", lambda args, base_dir: (["unused:Entry"], [Entry]))
    monkeypatch.setattr(cli_init, "_postgres_read_only_preflight", lambda *args, **kwargs: {
        "status": status, "review_warnings": [], "read_only": True,
    })
    return connection


def test_postgres_review_never_writes(monkeypatch, capsys):
    _setup(monkeypatch)
    assert cli_init.run_init_apply(_args()) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "EMPTY"

    assert report["write_enabled"] is False

    assert len(report["approval_sha256"]) == 64


def test_postgres_wrong_approval_refuses_execution(monkeypatch):
    _setup(monkeypatch)

    with pytest.raises(ValueError, match="Exact schema approval"):
        cli_init.run_init_apply(_args("incorrect", "APPLY"))


def test_postgres_approved_execution_delegates_once(monkeypatch, capsys):
    connection = _setup(monkeypatch)
    cli_init.run_init_apply(_args())
    digest = json.loads(capsys.readouterr().out)["approval_sha256"]
    calls = []
    import aurora.database.server_forward_init as forward_init
    monkeypatch.setattr(forward_init, "execute_server_forward_init", lambda conn, models, root, alias, **kw: (
        calls.append((conn, models, kw)) or __import__("pathlib").Path("20261112-000000.json")
    ))
    assert cli_init.run_init_apply(_args(digest, "APPLY")) == 0

    assert len(calls) == 1 and calls[0][0] is connection

    assert calls[0][2]["approval"] == digest

    assert json.loads(capsys.readouterr().out)["status"] == "INITIALIZED"


def test_postgres_schema_identity_changes_approval():
    parsed = urlsplit("postgresql+psycopg://localhost/aurorasql_test")
    def identity(schema):
        conn = SimpleNamespace(fetch_first=lambda sql: {
            "database_name": "aurorasql_test", "schema_name": schema,
        })
        return cli_init._postgres_database_identity(conn, parsed)
    assert cli_init._postgres_approval_digest([Entry], "default", identity("public")) != (
        cli_init._postgres_approval_digest([Entry], "default", identity("private"))
    )


def test_postgres_nonempty_schema_refuses_approval(monkeypatch, capsys):
    _setup(monkeypatch, status="REFUSED")
    assert cli_init.run_init_apply(_args()) == 3

    assert json.loads(capsys.readouterr().out)["approval_sha256"] is None
