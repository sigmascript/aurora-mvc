"""Original CREATE fingerprints must prove the pre-edit nullable flag."""

from hashlib import sha256

import pytest

from aurora.database import Model, column
from aurora.database.history import HistoryEntry
from aurora.database.model_identity import model_identity_map
from aurora.database.schema import create_table_sql
from aurora.database.server_migration_timeline import ServerMigrationEntry
from aurora.database.server_model_review import ServerModelProposal


class RequiredLabel(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    label = column(str, length=80)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_nullability_predecessor_is_separate_and_requires_matching_initial(monkeypatch,
                                                                            dialect):
    from aurora.database import server_nullability_history

    operation = {"kind": "change_nullability", "table": "labels", "column": "label",
                 "from": True, "to": False}
    proposal = ServerModelProposal(dialect, ("ALTER TABLE labels",), (operation,), "a" * 64)
    previous = server_nullability_history.nullability_predecessor_models(
        [RequiredLabel], operation)
    assert previous[0].__columns__["label"].nullable is True
    assert RequiredLabel.__columns__["label"].nullable is False

    fingerprint = sha256(create_table_sql(previous[0], dialect).encode()).hexdigest()
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "b" * 64, model_identity_map([RequiredLabel]),
                                   "c" * 64, "applied")
    monkeypatch.setattr(server_nullability_history, "inspect_server_timeline",
                        lambda *_: (initial,))
    entries = [HistoryEntry("created", "labels", dialect, fingerprint, "applied")]
    monkeypatch.setattr(server_nullability_history, "list_history", lambda _: tuple(entries))
    monkeypatch.setattr(server_nullability_history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_nullability_history, "verify_server_initial_schema",
                        lambda *args: "c" * 64)
    connection = type("Server", (), {"dialect": dialect})()
    assert server_nullability_history.verify_server_nullability_predecessor(
        connection, [RequiredLabel], "accounting", proposal)

    entries[0] = HistoryEntry("created", "labels", dialect, "d" * 64, "applied")

    with pytest.raises(RuntimeError, match="CREATE fingerprints"):
        server_nullability_history.verify_server_nullability_predecessor(
            connection, [RequiredLabel], "accounting", proposal)


def test_nullability_history_rejects_incorrect_transition_and_does_not_mutate_model():
    from aurora.database.server_nullability_history import nullability_predecessor_models

    with pytest.raises(ValueError, match="transition"):
        nullability_predecessor_models([RequiredLabel],
                                       {"kind": "change_nullability", "table": "labels",
                                        "column": "label", "from": "yes", "to": False})

    with pytest.raises(ValueError, match="constrained"):
        nullability_predecessor_models([RequiredLabel],
                                       {"kind": "change_nullability", "table": "labels",
                                        "column": "label", "from": False, "to": True})

    assert RequiredLabel.__columns__["label"].nullable is False
