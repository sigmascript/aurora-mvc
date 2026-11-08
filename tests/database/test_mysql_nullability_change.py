"""MySQL nullability execution refuses unknown DDL replay."""

from contextlib import contextmanager
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
            raise RuntimeError("MySQL ALTER outcome unknown")

        return 1


@pytest.mark.parametrize("fail", [False, True])
def test_mysql_nullability_backup_first_and_uncertain_on_ddl_error(monkeypatch, tmp_path, fail):
    from aurora.database import cli_init, cli_mysql_backup, mysql_nullability_change

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
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "accounting-id")
    monkeypatch.setattr(mysql_nullability_change, "migration_lock", locked)
    monkeypatch.setattr(mysql_nullability_change, "prepare_server_nullability_successor",
                        lambda conn, *args: conn.events.append(("prepared", None)) or 2)
    monkeypatch.setattr(mysql_nullability_change, "verify_server_declared_schema",
                        lambda conn, models: conn.events.append(("verified", None)) or "b" * 64)
    monkeypatch.setattr(mysql_nullability_change, "complete_server_successor",
                        lambda conn, *args: conn.events.append(("complete", None)))
    monkeypatch.setattr(mysql_nullability_change, "mark_server_successor_uncertain",
                        lambda conn, *args: conn.events.append(("uncertain", None)))
    review = SimpleNamespace(operations=({"kind": "change_nullability", "table": "labels",
                                         "column": "label", "from": True, "to": False},),
                             statements=("ALTER TABLE `labels` MODIFY COLUMN `label` VARCHAR(80) NOT NULL",))
    connection = Connection(fail=fail)

    def execute():
        return mysql_nullability_change.execute_mysql_nullability_change(
            connection, [object()], alias="accounting", identity="accounting-id",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            login_path=None, review=review, approval="c" * 64, name="RequireLabel")

    if fail:
        with pytest.raises(RuntimeError, match="outcome unknown"):
            execute()

        assert any(event == "uncertain" for event, _ in connection.events)
        assert any(params and params.get("status") == "uncertain"
                   for event, params in connection.events if event == "journal")
    else:
        identifier, saved = execute()
        assert identifier and saved == archive
        events = [event for event, _ in connection.events]
        assert events.index("alter") < events.index("verified") < events.index("complete")
