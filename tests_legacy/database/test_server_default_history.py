"""A new default must match recorded default-free Initial DDL."""

from hashlib import sha256

import pytest

from aurora.database import Model, column
from aurora.database.history import HistoryEntry
from aurora.database.model_identity import model_identity_map
from aurora.database.schema import create_table_sql
from aurora.database.server_migration_timeline import ServerMigrationEntry
from aurora.database.server_model_review import ServerModelProposal


class DefaultedLabel(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    label = column(str, length=80, default="new")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_default_history_proves_original_without_changing_current_model(monkeypatch, dialect):
    from aurora.database import server_default_history

    operation = {"kind": "add_default", "table": "labels", "column": "label"}
    proposal = ServerModelProposal(dialect, ("ALTER TABLE labels",), (operation,), "a" * 64)
    previous = server_default_history.default_addition_predecessor_models(
        [DefaultedLabel], operation)
    assert previous[0].__columns__["label"].default is None
    assert DefaultedLabel.__columns__["label"].default == "new"

    fingerprint = sha256(create_table_sql(previous[0], dialect).encode()).hexdigest()
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "b" * 64, model_identity_map([DefaultedLabel]),
                                   "c" * 64, "applied")
    monkeypatch.setattr(server_default_history, "inspect_server_timeline", lambda *_: (initial,))
    records = [HistoryEntry("initial", "labels", dialect, fingerprint, "applied")]
    monkeypatch.setattr(server_default_history, "list_history", lambda _: tuple(records))
    monkeypatch.setattr(server_default_history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_default_history, "verify_server_initial_schema",
                        lambda *args: "c" * 64)
    connection = type("Server", (), {"dialect": dialect})()
    assert server_default_history.verify_server_default_predecessor(
        connection, [DefaultedLabel], "accounting", proposal)

    records[0] = HistoryEntry("initial", "labels", dialect, "d" * 64, "applied")

    with pytest.raises(RuntimeError, match="CREATE fingerprints"):
        server_default_history.verify_server_default_predecessor(
            connection, [DefaultedLabel], "accounting", proposal)
