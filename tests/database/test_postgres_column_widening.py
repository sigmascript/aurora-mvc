"""PostgreSQL widening rolls back the reviewed DDL on verification failure."""

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

    def fetch_first(self, statement, params):
        self.events.append("lock")
        return {"acquired": True}

    def execute_write(self, statement, params=None):
        assert self._transaction_active
        self.events.append("alter" if statement.startswith("ALTER TABLE") else "journal")
        return 1


@pytest.mark.parametrize("fail_verification", [False, True])
def test_postgres_widening_is_backup_first_and_transactional(monkeypatch, tmp_path,
                                                              fail_verification):
    from aurora.database import cli_init, cli_postgres_backup, postgres_column_widening

    archive = tmp_path / "before.dump"

    def backup(*args):
        archive.write_bytes(b"PGDMP" + b"0" * 100)
        return archive

    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", backup)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "accounting-db")
    monkeypatch.setattr(postgres_column_widening, "prepare_server_widening_successor",
                        lambda conn, *args: conn.events.append("prepared") or 2)

    def verify(connection, models):
        connection.events.append("verified")

        if fail_verification:
            raise RuntimeError("physical schema mismatch")

        return "a" * 64

    monkeypatch.setattr(postgres_column_widening, "verify_server_declared_schema", verify)
    monkeypatch.setattr(postgres_column_widening, "complete_server_successor",
                        lambda conn, *args: conn.events.append("complete"))
    review = SimpleNamespace(operations=({"kind": "widen_column", "table": "invoices",
                                         "column": "memo", "from": "varchar(100)",
                                         "to": "varchar(200)"},),
                             statements=('ALTER TABLE "invoices" ALTER COLUMN "memo" TYPE VARCHAR(200)',))
    connection = Connection()

    def execute():
        return postgres_column_widening.execute_postgres_column_widening(
            connection, [object()], alias="accounting", identity="accounting-db",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            review=review, approval="a" * 64, name="ExpandMemo")

    if fail_verification:
        with pytest.raises(RuntimeError, match="physical schema mismatch"):
            execute()

        assert connection.events[-1] == "rollback"
        assert "commit" not in connection.events
    else:
        identifier, saved = execute()
        assert identifier and saved == archive
        assert connection.events == ["begin", "lock", "prepared", "journal", "alter",
                                     "journal", "verified", "complete", "commit"]


def test_postgres_repeated_widening_rolls_back_failed_chain_proof(monkeypatch, tmp_path):
    from aurora.database import cli_init, cli_postgres_backup, postgres_column_widening
    from aurora.database import server_history_chain

    archive = tmp_path / "before.dump"

    def backup(*args):
        archive.write_bytes(b"PGDMP" + b"0" * 100)
        return archive

    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", backup)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "accounting-db")
    monkeypatch.setattr(postgres_column_widening, "prepare_server_widening_successor",
                        lambda conn, *args: conn.events.append("prepared") or 3)
    monkeypatch.setattr(postgres_column_widening, "verify_server_declared_schema",
                        lambda *_: "a" * 64)
    monkeypatch.setattr(postgres_column_widening, "complete_server_successor",
                        lambda conn, *args: conn.events.append("complete"))
    monkeypatch.setattr(server_history_chain, "verify_server_applied_chain", lambda *_: False)
    review = SimpleNamespace(operations=({"kind": "widen_column", "table": "invoices",
                                         "column": "memo", "from": "varchar(100)",
                                         "to": "varchar(200)"},),
                             statements=('ALTER TABLE "invoices" ALTER COLUMN "memo" '
                                         'TYPE VARCHAR(200)',))
    connection = Connection()

    with pytest.raises(RuntimeError, match="history chain is unverified"):
        postgres_column_widening.execute_postgres_column_widening(
            connection, [object()], alias="accounting", identity="accounting-db",
            parsed=SimpleNamespace(), config_path="ignored", backup_path=str(archive),
            review=review, approval="a" * 64, name="Migration3")

    assert connection.events[-1] == "rollback"
    assert "commit" not in connection.events
    assert connection.events.index("alter") < connection.events.index("complete")
    assert archive.read_bytes().startswith(b"PGDMP")
