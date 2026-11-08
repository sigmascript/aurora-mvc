"""MySQL ADD COLUMN refuses replay when DDL or verification is uncertain."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


class Connection:
    dialect = "mysql"
    _transaction_active = False
    _migration_lock_active = False

    def __init__(self, fail_ddl=False):
        self.events = []
        self.fail_ddl = fail_ddl

    def execute_write(self, statement, params=None):
        assert self._migration_lock_active
        self.events.append(("alter" if statement.startswith("ALTER TABLE") else "journal", params))

        if statement.startswith("ALTER TABLE") and self.fail_ddl:
            raise RuntimeError("unknown MySQL ALTER outcome")

        return 1


def configure(monkeypatch, tmp_path):
    from aurora.database import cli_init, cli_mysql_backup, mysql_column_addition

    archive = tmp_path / "before.sql"

    def backup(*args):
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

    monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", backup)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "accounting-db")
    monkeypatch.setattr(mysql_column_addition, "migration_lock", locked)
    monkeypatch.setattr(mysql_column_addition, "prepare_server_column_successor",
                        lambda conn, *args: conn.events.append(("prepared", None)) or 2)
    monkeypatch.setattr(mysql_column_addition, "verify_server_declared_schema",
                        lambda conn, models: conn.events.append(("verified", None)) or "c" * 64)
    monkeypatch.setattr(mysql_column_addition, "complete_server_successor",
                        lambda conn, *args: conn.events.append(("complete", None)))
    monkeypatch.setattr(mysql_column_addition, "mark_server_successor_uncertain",
                        lambda conn, *args: conn.events.append(("uncertain", None)))
    return archive


def execute(connection, archive):
    from aurora.database.mysql_column_addition import execute_mysql_column_addition

    review = SimpleNamespace(operations=({"kind": "add_column", "table": "invoices",
                                         "column": "memo"},),
                             statements=("ALTER TABLE `invoices` ADD COLUMN `memo` TEXT",))
    return execute_mysql_column_addition(
        connection, [object()], alias="accounting", identity="accounting-db",
        parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
        login_path=None, review=review, approval="a" * 64, name="AddMemo")


def test_mysql_column_addition_completes_verified_operation(monkeypatch, tmp_path):
    archive = configure(monkeypatch, tmp_path)
    connection = Connection()
    operation_id, saved = execute(connection, archive)
    events = [event for event, _ in connection.events]

    assert operation_id and saved == archive
    assert events.index("alter") < events.index("verified") < events.index("complete")
    assert "uncertain" not in events


def test_mysql_column_addition_preserves_uncertainty_on_unknown_ddl(monkeypatch, tmp_path):
    archive = configure(monkeypatch, tmp_path)
    connection = Connection(fail_ddl=True)

    with pytest.raises(RuntimeError, match="unknown MySQL"):
        execute(connection, archive)

    events = [event for event, _ in connection.events]
    assert events.count("alter") == 1
    assert events.index("alter") < events.index("uncertain") < events.index("unlock")
    assert any(params and params.get("status") == "uncertain"
               for event, params in connection.events if event == "journal")
