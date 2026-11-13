"""Public init preflight must recognize verified forward-only server state."""

from types import SimpleNamespace

import pytest

from aurora.database import cli_init
from aurora.database.snapshots import schema_snapshot
from tests.database.test_schema import Entry


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_new_server_history_verified_as_initialized(monkeypatch, tmp_path, dialect):
    from aurora.database import server_forward_history, planning
    from aurora.database import cli_init as module

    connection = SimpleNamespace(dialect=dialect, fetch_all=lambda sql: [
        {"name": "entry"}, {"name": "migrations"}
    ])
    monkeypatch.setattr(module, "plan_schema", lambda conn, model: SimpleNamespace(
        table="entry", exists=True, changes=[], warnings=[]
    ))
    models = [Entry]
    snapshot = {"models": schema_snapshot(*models, dialect=dialect)["tables"]}
    monkeypatch.setattr(server_forward_history, "read_history", lambda *args: ([1], snapshot))
    report = cli_init._preflight(connection, models, "data", project_root=tmp_path)
    assert report["status"] == "ALREADY_INITIALIZED"
    assert report["unexpected_tables"] == []

    snapshot["models"] = []
    assert cli_init._preflight(connection, models, "data", project_root=tmp_path)["status"] == "EXISTING_UNVERIFIED"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_invalid_forward_server_history_refuses_init(monkeypatch, tmp_path, dialect):
    from aurora.database import server_forward_history
    connection = SimpleNamespace(dialect=dialect, fetch_all=lambda sql: [
        {"name": "entry"}, {"name": "migrations"}
    ])
    monkeypatch.setattr(cli_init, "plan_schema", lambda conn, model: SimpleNamespace(
        table="entry", exists=True, changes=[], warnings=[]
    ))
    def fail(*args):
        raise RuntimeError("Snapshot is missing")
    monkeypatch.setattr(server_forward_history, "read_history", fail)
    report = cli_init._preflight(connection, [Entry], "data", project_root=tmp_path)
    assert report["status"] == "REFUSED"
    assert "Snapshot is missing" in report["reason"]
