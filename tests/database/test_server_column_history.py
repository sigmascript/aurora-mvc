"""Historical column-addition proofs never alter a model or database."""

from hashlib import sha256

import pytest

from aurora.database import Model, column
from aurora.database.history import HistoryEntry
from aurora.database.model_identity import model_identity_map
from aurora.database.schema import create_table_sql
from aurora.database.server_migration_timeline import ServerMigrationEntry
from aurora.database.server_model_review import ServerModelProposal


class OldInvoice(Model):
    __table__ = "invoices"
    id = column(int, primary_key=True)


class NewInvoice(Model):
    __table__ = "invoices"
    id = column(int, primary_key=True)
    memo = column(str, nullable=True)


def test_added_column_requires_historical_schema_and_preserves_live_model(monkeypatch):
    from aurora.database import server_column_history

    original = sha256(create_table_sql(OldInvoice, "mysql").encode()).hexdigest()
    entry = HistoryEntry("initial-table", "invoices", "mysql", original, "applied")
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map([NewInvoice]),
                                   "b" * 64, "applied")
    monkeypatch.setattr(server_column_history, "inspect_server_timeline", lambda *_: (initial,))
    monkeypatch.setattr(server_column_history, "list_history", lambda _: (entry,))
    monkeypatch.setattr(server_column_history, "review_history", lambda *_, **__: ())
    examined = []

    def verify(conn, selected):
        examined.extend(selected)
        return "b" * 64

    monkeypatch.setattr(server_column_history, "verify_server_initial_schema", verify)
    proposal = ServerModelProposal("mysql", ("ALTER TABLE invoices ADD COLUMN memo TEXT",),
                                   ({"kind": "add_column", "table": "invoices",
                                     "column": "memo"},), "c" * 64)
    connection = type("ReadOnly", (), {"dialect": "mysql"})()
    predecessor = server_column_history.verify_server_column_addition_predecessor(
        connection, [NewInvoice], "accounting", proposal)

    assert len(predecessor) == 1
    assert set(predecessor[0].__columns__) == {"id"}
    assert create_table_sql(predecessor[0], "mysql") == create_table_sql(OldInvoice, "mysql")
    assert set(NewInvoice.__columns__) == {"id", "memo"}
    assert examined == list(predecessor)

    monkeypatch.setattr(server_column_history, "verify_server_initial_schema",
                        lambda *args: "e" * 64)

    with pytest.raises(RuntimeError, match="fingerprint differs"):
        server_column_history.verify_server_column_addition_predecessor(
            connection, [NewInvoice], "accounting", proposal)


def test_column_addition_does_not_accept_incorrect_historical_create(monkeypatch):
    from aurora.database import server_column_history

    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map([NewInvoice]),
                                   "b" * 64, "applied")
    monkeypatch.setattr(server_column_history, "inspect_server_timeline", lambda *_: (initial,))
    monkeypatch.setattr(server_column_history, "list_history", lambda _: (
        HistoryEntry("untrusted", "invoices", "postgresql", "e" * 64, "applied"),))
    monkeypatch.setattr(server_column_history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_column_history, "verify_server_initial_schema",
                        lambda *args: pytest.fail("Must refuse before physical verification"))
    proposal = ServerModelProposal("postgresql", ("ALTER TABLE invoices ADD COLUMN memo TEXT",),
                                   ({"kind": "add_column", "table": "invoices",
                                     "column": "memo"},), "c" * 64)
    connection = type("ReadOnly", (), {"dialect": "postgresql"})()

    with pytest.raises(RuntimeError, match="CREATE fingerprint"):
        server_column_history.verify_server_column_addition_predecessor(
            connection, [NewInvoice], "accounting", proposal)
