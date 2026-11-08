"""MySQL successor must quarantine unknown DDL and never retry."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aurora.database import Model, column


class OriginalAccount(Model):
    __table__ = "original_accounts"
    id = column(int, primary_key=True)


class NewInvoice(Model):
    __table__ = "new_invoices"
    id = column(int, primary_key=True)


class MySQLConnection:
    dialect = "mysql"
    _transaction_active = False
    _migration_lock_active = False

    def __init__(self):
        self.events = []

    def execute_write(self, statement, params=None):
        assert self._migration_lock_active
        self.events.append(("write", params))
        return 1


def configure_successor(monkeypatch, tmp_path, *, fail_ddl=False, backup_fails=False,
                        schema_drift=False):
    from aurora.database import cli_init, cli_mysql_backup, mysql_successor
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    models = [OriginalAccount, NewInvoice]
    archive = tmp_path / "accounting.sql"

    def backup(config, alias, destination, login_path):
        if backup_fails:
            raise ValueError("backup refused")

        archive.write_text("verified controlled backup")
        return archive

    @contextmanager
    def locked(connection):
        connection._migration_lock_active = True
        connection.events.append(("lock", None))

        try:
            yield
        finally:
            connection._migration_lock_active = False
            connection.events.append(("unlock", None))

    previous = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                    "a" * 64, model_identity_map(models[:1]),
                                    "b" * 64, "applied")
    pending = ServerMigrationEntry(2, "Invoices", "Untitled", "accounting",
                                   "c" * 64, model_identity_map(models), None, "prepared")
    monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", backup)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "accounting-db")
    monkeypatch.setattr(mysql_successor, "migration_lock", locked)
    monkeypatch.setattr(mysql_successor, "prepare_server_successor", lambda *args: 2)
    monkeypatch.setattr(mysql_successor, "inspect_server_timeline", lambda *args: (previous, pending))

    def create(conn, *args, **kwargs):
        conn.events.append(("ddl", None))

        if fail_ddl:
            raise RuntimeError("unknown MySQL DDL outcome")

    monkeypatch.setattr(mysql_successor, "execute_create", create)
    monkeypatch.setattr(mysql_successor, "verify_server_declared_schema",
                        lambda conn, selected, evidence_models=None:
                        ("e" * 64 if schema_drift else "b" * 64)
                        if evidence_models is not None else "d" * 64)
    monkeypatch.setattr(mysql_successor, "complete_server_successor",
                        lambda conn, *args: conn.events.append(("complete", None)))
    monkeypatch.setattr(mysql_successor, "mark_server_successor_uncertain",
                        lambda conn, *args: conn.events.append(("uncertain", args)))
    return models, archive


def execute(connection, models, archive):
    from aurora.database.mysql_successor import execute_mysql_new_table_successor

    return execute_mysql_new_table_successor(
        connection, models, alias="accounting", identity="accounting-db",
        parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
        login_path=None, review=object(), approval="c" * 64, name="Invoices")


def test_mysql_successor_completes_with_verified_backup_and_history(monkeypatch, tmp_path):
    models, archive = configure_successor(monkeypatch, tmp_path)
    connection = MySQLConnection()
    operation, saved = execute(connection, models, archive)

    assert operation and saved == archive
    events = [name for name, _ in connection.events]
    assert events.index("ddl") < events.index("complete") < events.index("unlock")
    assert "uncertain" not in events


def test_mysql_unknown_ddl_is_quarantined_and_never_retried(monkeypatch, tmp_path):
    models, archive = configure_successor(monkeypatch, tmp_path, fail_ddl=True)
    connection = MySQLConnection()

    with pytest.raises(RuntimeError, match="unknown MySQL"):
        execute(connection, models, archive)

    events = [name for name, _ in connection.events]
    assert events.count("ddl") == 1
    assert events.index("ddl") < events.index("uncertain") < events.index("unlock")
    assert any(params and params.get("state") == "uncertain"
               for name, params in connection.events if name == "write")


def test_mysql_backup_refusal_happens_before_migration_lock(monkeypatch, tmp_path):
    models, archive = configure_successor(monkeypatch, tmp_path, backup_fails=True)
    connection = MySQLConnection()

    with pytest.raises(ValueError, match="backup refused"):
        execute(connection, models, archive)

    assert not connection.events


def test_mysql_successful_ddl_with_failed_schema_verification_is_still_uncertain(monkeypatch,
                                                                                 tmp_path):
    models, archive = configure_successor(monkeypatch, tmp_path, schema_drift=True)
    connection = MySQLConnection()

    with pytest.raises(RuntimeError, match="Initial schema changed"):
        execute(connection, models, archive)

    assert any(name == "ddl" for name, _ in connection.events)
    assert any(name == "uncertain" for name, _ in connection.events)
    updates = [params for name, params in connection.events
               if name == "write" and params and params.get("state") == "uncertain"]
    assert len(updates) == 1
