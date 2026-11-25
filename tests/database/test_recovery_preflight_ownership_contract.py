"""Recovery preflight is a read-only safety gate, not history execution authority."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora import database
from aurora.database import migration_coordination, migration_history, migration_safety


def test_recovery_preflight_owner_and_public_compatibility():
    assert database.RecoveryPreflight is migration_safety.RecoveryPreflight
    assert database.preflight_recovery is migration_safety.preflight_recovery
    assert migration_history.RecoveryPreflight is migration_safety.RecoveryPreflight
    assert migration_history.preflight_recovery is migration_safety.preflight_recovery
    assert migration_safety.RecoveryPreflight.__module__ == migration_safety.__name__


def test_recovery_preflight_collects_evidence_under_lock_without_authorizing(monkeypatch):
    calls = []

    @contextmanager
    def locked(connection):
        calls.append("enter")
        yield
        calls.append("exit")

    monkeypatch.setattr(migration_coordination, "migration_lock", locked)
    monkeypatch.setattr(
        migration_history, "assess_recovery", lambda *args: calls.append("assess") or "review"
    )
    monkeypatch.setattr(
        migration_history,
        "review_recovery_schema",
        lambda *args: calls.append("schema") or ("finding",),
    )
    monkeypatch.setattr(
        migration_history, "review_history", lambda *args: calls.append("history") or ("warning",)
    )
    report = migration_safety.preflight_recovery(SimpleNamespace(), "operation-1", object())
    assert calls == ["enter", "assess", "schema", "history", "exit"]
    assert report.operation_id == "operation-1"
    assert report.assessment == "review"
    assert report.schema_findings == ("finding",)
    assert report.history_findings == ("warning",)
    assert report.reconciliation_allowed is False


def test_recovery_preflight_propagates_fail_closed_review(monkeypatch):
    @contextmanager
    def locked(connection):
        yield

    monkeypatch.setattr(migration_coordination, "migration_lock", locked)

    def reject(*args):
        raise RuntimeError("unresolved history")

    monkeypatch.setattr(migration_history, "assess_recovery", reject)
    monkeypatch.setattr(
        migration_history, "review_recovery_schema", lambda *args: pytest.fail("continued")
    )
    with pytest.raises(RuntimeError, match="unresolved history"):
        migration_safety.preflight_recovery(SimpleNamespace(), "operation-1", object())
