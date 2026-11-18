"""Guarded MySQL/PostgreSQL five-column history adoption, without live DDL."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_history as history


class Server:
    def __init__(self, dialect):
        self.dialect = dialect
        self.table = "migrations"
        self.identity = "correct"
        self._transaction_active = False
        self._migration_lock_active = False
        self.rows = [{"id": 1, "title": "Initial", "snapshot": "20261117-123456.json"}]
        self.record = {"database": "data", "physical_schema": [{"table": "user"}]}
        self.statements = []
        self.fail_after_ddl = False
        self.rollbacks = 0

    def execute_write(self, sql):
        self.statements.append(sql)
        self.table = "__migrations__"

    def fetch_first(self, sql, params=None):
        return {"acquired": True}

    def fetch_all(self, sql, params=None):
        raise AssertionError("CLI should use patched verified-history reader")

    @contextmanager
    def transaction(self):
        self._transaction_active = True
        try:
            yield self
        except BaseException:
            self.table = "migrations"
            self.rollbacks += 1
            raise
        finally:
            self._transaction_active = False


def _patch(monkeypatch):
    from aurora.database import cli_init, coordination

    monkeypatch.setattr(history, "history_table", lambda db: db.table)

    def read(db, root, alias):
        if db.fail_after_ddl and db.table == "__migrations__":
            raise RuntimeError("Cannot verify new table")
        return db.rows.copy(), db.record.copy()

    monkeypatch.setattr(history, "read_history", read)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda db, parsed: db.identity)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda db, parsed: db.identity)

    @contextmanager
    def lock(db):
        db._migration_lock_active = True
        try:
            yield
        finally:
            db._migration_lock_active = False

    monkeypatch.setattr(coordination, "migration_lock", lock)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_adopts_one_verified_history_without_rewriting_records(monkeypatch, tmp_path, dialect):
    _patch(monkeypatch)
    db = Server(dialect)
    original_rows = db.rows.copy()
    original_record = db.record.copy()
    assert history.adopt_history(db, tmp_path, "data", identity="correct", parsed=object()) == 1
    assert db.table == "__migrations__"
    assert db.statements == ["ALTER TABLE migrations RENAME TO __migrations__"]
    assert db.rows == original_rows and db.record == original_record


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_refuses_unverified_or_conflicting_state_before_ddl(monkeypatch, tmp_path, dialect):
    _patch(monkeypatch)
    db = Server(dialect)
    with pytest.raises(RuntimeError, match="identity changed"):
        history.adopt_history(db, tmp_path, "data", identity="other", parsed=object())
    db.table = "__migrations__"
    with pytest.raises(RuntimeError, match="no single legacy"):
        history.adopt_history(db, tmp_path, "data", identity="correct", parsed=object())
    assert db.statements == []


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_post_ddl_verification_failure_is_not_a_success(monkeypatch, tmp_path, dialect):
    _patch(monkeypatch)
    db = Server(dialect)
    db.fail_after_ddl = True
    with pytest.raises(RuntimeError, match="uncertain" if dialect == "mysql" else "Cannot verify"):
        history.adopt_history(db, tmp_path, "data", identity="correct", parsed=object())
    assert len(db.statements) == 1
    if dialect == "postgresql":
        assert db.table == "migrations" and db.rollbacks == 1
    else:
        assert db.table == "__migrations__"


@pytest.mark.parametrize("confirmation,expected", [(None, False), ("yes", True)])
def test_cli_requires_separate_confirmation_for_unchanged_legacy_history(
        monkeypatch, tmp_path, capsys, confirmation, expected):
    from aurora.database import cli_server_model_migration as cli
    from aurora.database import cli_init, server_forward_history as server
    from aurora.database import snapshots

    db = Server("postgresql")
    config = SimpleNamespace(resolve=lambda name: "postgresql+psycopg://localhost/data")

    @contextmanager
    def connect(name):
        assert name == "data"
        yield db

    config.connect = connect
    model = SimpleNamespace(__table_name__="user")
    monkeypatch.setattr(cli, "_load_config", lambda _: (config, tmp_path))
    monkeypatch.setattr(cli, "_select_models", lambda args, root: (None, [model]))
    monkeypatch.setattr(server, "has_history", lambda conn: True)
    monkeypatch.setattr(server, "read_history", lambda *a: ([{"id": 1}], {"models": ["User"]}))
    monkeypatch.setattr(server, "history_table", lambda conn: "migrations")
    monkeypatch.setattr(snapshots, "schema_snapshot", lambda *a, **kw: {"tables": ["User"]})
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *a: "same-identity")
    calls = []
    monkeypatch.setattr(server, "adopt_history", lambda *a, **kw: calls.append(kw) or 1)
    args = SimpleNamespace(database="data", config=None, confirm=confirmation)
    assert cli.run_server_model_migration(args) == 0
    assert bool(calls) is expected
    if expected:
        assert calls[0]["identity"] == "same-identity"
        assert "preserved 1 history record" in capsys.readouterr().out
    else:
        assert "--confirm yes" in capsys.readouterr().out


def test_cli_does_not_adopt_legacy_history_when_models_have_changed(monkeypatch, tmp_path):
    from aurora.database import cli_server_model_migration as cli
    from aurora.database import server_forward_history as server, snapshots

    db = Server("mysql")
    config = SimpleNamespace(resolve=lambda name: "mysql+pymysql://localhost/data")

    @contextmanager
    def connect(name):
        yield db

    config.connect = connect
    monkeypatch.setattr(cli, "_load_config", lambda _: (config, tmp_path))
    monkeypatch.setattr(cli, "_select_models", lambda args, root: (None, [object()]))
    monkeypatch.setattr(server, "has_history", lambda conn: True)
    monkeypatch.setattr(server, "read_history", lambda *a: ([{"id": 1}], {"models": ["Old"]}))
    monkeypatch.setattr(server, "history_table", lambda conn: "migrations")
    monkeypatch.setattr(snapshots, "schema_snapshot", lambda *a, **kw: {"tables": ["Changed"]})
    with pytest.raises(RuntimeError, match="model declarations must first match"):
        cli.run_server_model_migration(SimpleNamespace(database="data", config=None, confirm="yes"))
    assert db.statements == []
