"""PostgreSQL successor primitive stays transaction-bound and inaccessible via CLI."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aurora.database import Model, column


class OriginalInvoice(Model):
    __table__ = "original_invoices"
    id = column(int, primary_key=True)


class NewPayment(Model):
    __table__ = "new_payments"
    id = column(int, primary_key=True)


class TransactionalConnection:
    dialect = "postgresql"

    def __init__(self, *, locked=True):
        self._transaction_active = False
        self.locked = locked
        self.events = []
        self.rolled_back = False

    @contextmanager
    def transaction(self):
        assert not self._transaction_active
        self._transaction_active = True
        self.events.append("begin")

        try:
            yield
        except BaseException:
            self.rolled_back = True
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self._transaction_active = False

    def fetch_first(self, statement, params=None):
        assert "pg_try_advisory_xact_lock" in statement
        self.events.append("lock")
        return {"acquired": self.locked}

    def execute_write(self, statement, params=None):
        assert self._transaction_active
        self.events.append("write")
        return 1


def prepare_test_successor(monkeypatch, tmp_path, *, drift=False, backup_failure=False):
    from aurora.database import cli_init, cli_postgres_backup, postgres_successor
    from aurora.database import server_migration_timeline, server_initial_verification
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    models = [OriginalInvoice, NewPayment]
    archive = tmp_path / "accounting.dump"

    def backup(config, alias, destination):
        if backup_failure:
            raise ValueError("backup unavailable")

        archive.write_bytes(b"PGDMP" + b"0" * 100)
        return archive

    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", backup)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "correct-db")
    monkeypatch.setattr(postgres_successor, "prepare_server_successor",
                        lambda *args: 2)
    predecessor = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                       "a" * 64, model_identity_map(models[:1]),
                                       "b" * 64, "applied")
    pending = ServerMigrationEntry(2, "Payments", "Untitled", "accounting",
                                   "c" * 64, model_identity_map(models), None, "prepared")
    monkeypatch.setattr(postgres_successor, "inspect_server_timeline",
                        lambda *_: (predecessor, pending))
    monkeypatch.setattr(postgres_successor, "execute_create",
                        lambda conn, model, token, **kwargs: conn.events.append("ddl"))
    monkeypatch.setattr(postgres_successor, "verify_server_declared_schema",
                        lambda connection, models, evidence_models=None:
                        "c" * 64 if evidence_models is None else "d" * 64 if drift else "b" * 64)
    monkeypatch.setattr(postgres_successor, "complete_server_successor",
                        lambda conn, *args: conn.events.append("complete"))
    return models, archive


def test_postgres_new_table_successor_executes_inside_single_transaction(monkeypatch, tmp_path):
    from aurora.database.postgres_successor import execute_postgres_new_table_successor

    models, archive = prepare_test_successor(monkeypatch, tmp_path)
    connection = TransactionalConnection()
    operation, saved = execute_postgres_new_table_successor(
        connection, models, alias="accounting", identity="correct-db",
        parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
        review=object(), approval="c" * 64, name="Payments")

    assert operation and saved == archive
    assert connection.events[0:2] == ["begin", "lock"]
    assert connection.events.index("ddl") < connection.events.index("complete")
    assert connection.events[-1] == "commit"


@pytest.mark.parametrize("failure", ["backup", "lock", "drift"])
def test_postgres_successor_refuses_unsafe_state_without_commit(monkeypatch, tmp_path, failure):
    from aurora.database.postgres_successor import execute_postgres_new_table_successor

    models, archive = prepare_test_successor(
        monkeypatch, tmp_path, drift=failure == "drift", backup_failure=failure == "backup")
    connection = TransactionalConnection(locked=failure != "lock")

    with pytest.raises((ValueError, RuntimeError)):
        execute_postgres_new_table_successor(
            connection, models, alias="accounting", identity="correct-db",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            review=object(), approval="c" * 64, name="Payments")

    assert "commit" not in connection.events

    if failure in ("backup", "lock"):
        assert "ddl" not in connection.events
    else:
        assert connection.rolled_back


def test_postgresql_identity_refuses_database_redirection():
    from aurora.database.cli_init import _postgres_database_identity

    class Server:
        def fetch_first(self, statement):
            return {"database_name": "other_database", "schema_name": "public"}

    parsed = SimpleNamespace(hostname="localhost", port=5432, path="/accounting")

    with pytest.raises(RuntimeError, match="differs from configuration"):
        _postgres_database_identity(Server(), parsed)
