"""PostgreSQL ADD COLUMN executor is backup-first and transaction scoped."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


class Connection:
    dialect = "postgresql"

    def __init__(self):
        self._transaction_active = False
        self.events = []

    @contextmanager
    def transaction(self):
        self._transaction_active = True
        self.events.append("begin")

        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self._transaction_active = False

    def fetch_first(self, sql, params):
        self.events.append("lock")
        return {"acquired": True}

    def execute_write(self, statement, params=None):
        assert self._transaction_active
        self.events.append("alter" if statement.startswith("ALTER TABLE") else "journal")
        return 1


def test_postgres_column_addition_orders_backup_history_ddl_and_verification(monkeypatch,
                                                                             tmp_path):
    from aurora.database import (cli_init, cli_postgres_backup,
                                 postgres_column_addition)

    archive = tmp_path / "before.dump"

    def backup(*args):
        archive.write_bytes(b"PGDMP" + b"0" * 100)
        return archive

    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", backup)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "accounting-db")
    monkeypatch.setattr(postgres_column_addition, "prepare_server_column_successor",
                        lambda conn, *args: conn.events.append("prepared") or 2)
    monkeypatch.setattr(postgres_column_addition, "verify_server_declared_schema",
                        lambda conn, models: conn.events.append("verified") or "a" * 64)
    monkeypatch.setattr(postgres_column_addition, "complete_server_successor",
                        lambda conn, *args: conn.events.append("completed"))
    review = SimpleNamespace(operations=({"kind": "add_column", "table": "invoices",
                                         "column": "memo"},),
                             statements=('ALTER TABLE "invoices" ADD COLUMN "memo" TEXT',))
    connection = Connection()
    operation, saved = postgres_column_addition.execute_postgres_column_addition(
        connection, [object()], alias="accounting", identity="accounting-db",
        parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
        review=review, approval="b" * 64, name="AddMemo")

    assert operation and saved == archive
    assert connection.events == ["begin", "lock", "prepared", "journal", "alter",
                                 "journal", "verified", "completed", "commit"]


def test_postgres_column_addition_rolls_back_on_schema_failure(monkeypatch, tmp_path):
    from aurora.database import (cli_init, cli_postgres_backup,
                                 postgres_column_addition)

    archive = tmp_path / "before.dump"

    def backup(*args):
        archive.write_bytes(b"PGDMP" + b"0" * 100)
        return archive

    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", backup)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "accounting-db")
    monkeypatch.setattr(postgres_column_addition, "prepare_server_column_successor",
                        lambda *args: 2)
    monkeypatch.setattr(postgres_column_addition, "verify_server_declared_schema",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("schema drift")))
    review = SimpleNamespace(operations=({"kind": "add_column", "table": "invoices",
                                         "column": "memo"},),
                             statements=('ALTER TABLE "invoices" ADD COLUMN "memo" TEXT',))
    connection = Connection()

    with pytest.raises(RuntimeError, match="schema drift"):
        postgres_column_addition.execute_postgres_column_addition(
            connection, [object()], alias="accounting", identity="accounting-db",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            review=review, approval="b" * 64, name="AddMemo")

    assert "rollback" in connection.events
    assert "commit" not in connection.events
