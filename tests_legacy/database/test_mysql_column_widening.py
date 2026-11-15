"""MySQL widening records uncertainty instead of guessing DDL outcomes."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


class Connection:
    dialect = "mysql"
    _transaction_active = False
    _migration_lock_active = False

    def __init__(self, fail=False):
        self.fail = fail
        self.events = []

    def execute_write(self, statement, params=None):
        assert self._migration_lock_active
        self.events.append(("alter" if statement.startswith("ALTER TABLE") else "journal", params))

        if self.fail and statement.startswith("ALTER TABLE"):
            raise RuntimeError("unknown ALTER outcome")

        return 1


@pytest.mark.parametrize("sequence", [2, 3])
def test_mysql_widening_requires_backup_and_quarantines_unknown_ddl(monkeypatch, tmp_path,
                                                                    sequence):
    from aurora.database import (cli_init, cli_mysql_backup, mysql_column_widening,
                                 server_history_chain)

    archive = tmp_path / "before.sql"

    def backup(*args):
        archive.write_text("verified backup")
        return archive

    @contextmanager
    def locked(connection):
        connection._migration_lock_active = True

        try:
            yield
        finally:
            connection._migration_lock_active = False

    monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", backup)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "accounting-db")
    monkeypatch.setattr(mysql_column_widening, "migration_lock", locked)
    monkeypatch.setattr(mysql_column_widening, "prepare_server_widening_successor",
                        lambda conn, *args: conn.events.append(("prepared", None)) or sequence)
    monkeypatch.setattr(mysql_column_widening, "verify_server_declared_schema",
                        lambda conn, models: conn.events.append(("verified", None)) or "a" * 64)
    monkeypatch.setattr(mysql_column_widening, "complete_server_successor",
                        lambda conn, *args: conn.events.append(("complete", None)))
    monkeypatch.setattr(server_history_chain, "verify_server_applied_chain", lambda *_: True)
    monkeypatch.setattr(mysql_column_widening, "mark_server_successor_uncertain",
                        lambda conn, *args: conn.events.append(("uncertain", args[0])))
    review = SimpleNamespace(operations=({"kind": "widen_column", "table": "invoices",
                                         "column": "memo", "from": "varchar(100)",
                                         "to": "varchar(200)"},),
                             statements=("ALTER TABLE `invoices` MODIFY COLUMN `memo` VARCHAR(200) NOT NULL",))

    def execute(connection):
        return mysql_column_widening.execute_mysql_column_widening(
            connection, [object()], alias="accounting", identity="accounting-db",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            login_path=None, review=review, approval="a" * 64, name="ExpandMemo")

    successful = Connection()
    identifier, saved = execute(successful)
    assert identifier and isinstance(saved, Path) and saved == archive
    events = [event for event, _ in successful.events]
    assert events.index("alter") < events.index("verified") < events.index("complete")

    uncertain = Connection(fail=True)

    with pytest.raises(RuntimeError, match="unknown ALTER"):
        execute(uncertain)

    events = [event for event, _ in uncertain.events]
    assert events.index("alter") < events.index("uncertain")
    assert ("uncertain", sequence) in uncertain.events
    assert any(params and params.get("status") == "uncertain"
               for event, params in uncertain.events if event == "journal")


def test_mysql_repeated_widening_quarantines_failed_post_completion_proof(
        monkeypatch, tmp_path):
    from aurora.database import cli_init, cli_mysql_backup, mysql_column_widening
    from aurora.database import server_history_chain

    archive = tmp_path / "before.sql"

    def backup(*args):
        archive.write_text("verified backup")
        return archive

    @contextmanager
    def locked(connection):
        connection._migration_lock_active = True

        try:
            yield
        finally:
            connection._migration_lock_active = False

    monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", backup)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "accounting-db")
    monkeypatch.setattr(mysql_column_widening, "migration_lock", locked)
    monkeypatch.setattr(mysql_column_widening, "prepare_server_widening_successor", lambda *_: 3)
    monkeypatch.setattr(mysql_column_widening, "verify_server_declared_schema", lambda *_: "a" * 64)
    monkeypatch.setattr(mysql_column_widening, "complete_server_successor",
                        lambda conn, *args: conn.events.append(("complete", args[0])))
    monkeypatch.setattr(mysql_column_widening, "mark_server_successor_uncertain",
                        lambda conn, *args: conn.events.append(("uncertain", args[0])))
    monkeypatch.setattr(server_history_chain, "verify_server_applied_chain", lambda *_: False)
    review = SimpleNamespace(operations=({"kind": "widen_column", "table": "invoices",
                                         "column": "memo", "from": "varchar(100)",
                                         "to": "varchar(200)"},),
                             statements=("ALTER TABLE `invoices` MODIFY COLUMN `memo` "
                                         "VARCHAR(200) NOT NULL",))
    connection = Connection()

    with pytest.raises(RuntimeError, match="history chain is unverified"):
        mysql_column_widening.execute_mysql_column_widening(
            connection, [object()], alias="accounting", identity="accounting-db",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            login_path=None, review=review, approval="a" * 64, name="Migration3")

    assert ("complete", 3) in connection.events
    assert ("uncertain", 3) in connection.events
    assert any(params and params.get("status") == "uncertain"
               for event, params in connection.events if event == "journal")
    assert archive.read_text() == "verified backup"
