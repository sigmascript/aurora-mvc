"""Historical widening proof refuses untrusted CREATE evidence."""

from hashlib import sha256

import pytest

from aurora.database import Model, column
from aurora.database.history import HistoryEntry
from aurora.database.model_identity import model_identity_map
from aurora.database.schema import create_table_sql
from aurora.database.server_migration_timeline import ServerMigrationEntry
from aurora.database.server_model_review import ServerModelProposal


class ExpandedProduct(Model):
    __table__ = "products"
    id = column(int, primary_key=True)
    name = column(str, length=200)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_widening_predecessor_matches_original_without_model_mutation(monkeypatch, dialect):
    from aurora.database import server_widening_history

    operation = {"kind": "widen_column", "table": "products", "column": "name",
                 "from": "varchar(100)", "to": "varchar(200)"}
    proposal = ServerModelProposal(dialect, ("ALTER TABLE products",), (operation,), "a" * 64)
    predecessor = server_widening_history.widening_predecessor_models([ExpandedProduct], operation)
    original = sha256(create_table_sql(predecessor[0], dialect).encode()).hexdigest()
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "b" * 64, model_identity_map([ExpandedProduct]),
                                   "c" * 64, "applied")
    monkeypatch.setattr(server_widening_history, "inspect_server_timeline", lambda *_: (initial,))
    monkeypatch.setattr(server_widening_history, "list_history", lambda _: (
        HistoryEntry("created", "products", dialect, original, "applied"),))
    monkeypatch.setattr(server_widening_history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_widening_history, "verify_server_initial_schema",
                        lambda conn, models: "c" * 64)
    conn = type("Server", (), {"dialect": dialect})()

    assert server_widening_history.verify_server_widening_predecessor(
        conn, [ExpandedProduct], "accounting", proposal)
    assert ExpandedProduct.__columns__["name"].length == 200
    assert predecessor[0].__columns__["name"].sql_type == "VARCHAR(100)"

    monkeypatch.setattr(server_widening_history, "verify_server_initial_schema",
                        lambda *args: "d" * 64)

    with pytest.raises(RuntimeError, match="Initial schema fingerprint"):
        server_widening_history.verify_server_widening_predecessor(
            conn, [ExpandedProduct], "accounting", proposal)
