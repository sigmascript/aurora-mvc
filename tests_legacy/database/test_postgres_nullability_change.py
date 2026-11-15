"""PostgreSQL nullability execution preserves atomic rollback."""

from contextlib import contextmanager
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

    def fetch_first(self, sql, params=None):
        self.events.append("lock")
        return {"acquired": True}

    def execute_write(self, statement, params=None):
        assert self._transaction_active
        self.events.append("alter" if statement.startswith("ALTER TABLE") else "journal")
        return 1


@pytest.mark.parametrize("fail_verification", [False, True])
def test_postgres_nullability_is_transaction_scoped(monkeypatch, tmp_path, fail_verification):
    from aurora.database import cli_init, cli_postgres_backup, postgres_nullability_change

    archive = tmp_path / "before.dump"

    def backup(*args):
        archive.write_bytes(b"PGDMP" + b"0" * 100)
        return archive

    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", backup)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "accounting-id")
    monkeypatch.setattr(postgres_nullability_change, "prepare_server_nullability_successor",
                        lambda conn, *args: conn.events.append("prepared") or 2)

    def verify(conn, models):
        conn.events.append("verified")

        if fail_verification:
            raise RuntimeError("catalog differs")

        return "b" * 64

    monkeypatch.setattr(postgres_nullability_change, "verify_server_declared_schema", verify)
    monkeypatch.setattr(postgres_nullability_change, "complete_server_successor",
                        lambda conn, *args: conn.events.append("complete"))
    review = SimpleNamespace(operations=({"kind": "change_nullability", "table": "labels",
                                         "column": "label", "from": True, "to": False},),
                             statements=('ALTER TABLE "labels" ALTER COLUMN "label" SET NOT NULL',))
    connection = Connection()

    def execute():
        return postgres_nullability_change.execute_postgres_nullability_change(
            connection, [object()], alias="accounting", identity="accounting-id",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            review=review, approval="c" * 64, name="RequireLabel")

    if fail_verification:
        with pytest.raises(RuntimeError, match="catalog differs"):
            execute()

        assert connection.events[-1] == "rollback"
        assert "commit" not in connection.events
    else:
        identifier, saved = execute()
        assert identifier and saved == archive
        assert connection.events == ["begin", "lock", "prepared", "journal", "alter",
                                     "journal", "verified", "complete", "commit"]
