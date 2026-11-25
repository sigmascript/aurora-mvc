"""Native successor sequence review is read-only and refuses unproven history."""

from types import SimpleNamespace

import pytest

from aurora.database import migration_review


class _Model:
    pass


def _entry(status, mapping):
    return SimpleNamespace(status=status, model_tables=mapping)


def test_successor_number_review_rejects_unresolved_history_without_writes(monkeypatch):
    class ReadOnlyConnection:
        def execute_write(self, *args, **kwargs):
            raise AssertionError("Read-only predecessor review must not write")

    monkeypatch.setattr(
        migration_review, "inspect_server_timeline", lambda *args: [_entry("uncertain", {})]
    )
    with pytest.raises(RuntimeError, match="Complete applied server history"):
        migration_review._reviewed_server_successor_number(
            ReadOnlyConnection(),
            [_Model],
            "primary",
            None,
            None,
            lambda *args: pytest.fail("Unverified history cannot reach predecessor proof"),
        )


def test_first_successor_requires_matching_model_identity_and_verifier(monkeypatch):
    from aurora.database import model_discovery

    mapping = {f"{_Model.__module__}:{_Model.__qualname__}": "_model"}
    monkeypatch.setattr(model_discovery, "model_identity_map", lambda models: mapping)
    monkeypatch.setattr(
        migration_review, "inspect_server_timeline", lambda *args: [_entry("applied", mapping)]
    )
    seen = []

    number = migration_review._reviewed_server_successor_number(
        object(),
        [_Model],
        "primary",
        "identity",
        "review",
        lambda connection, models, alias, review: seen.append((alias, review)),
    )

    assert number == 2
    assert seen == [("primary", "review")]
