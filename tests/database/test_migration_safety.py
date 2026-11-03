"""Safety contract tests: no migration execution is enabled."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database.migration_safety import inspect_migration_readiness
from aurora.database.review_artifacts import create_review


def snapshot(*tables):
    return {"format": "aurorasql.schema.v1", "dialect": "sqlite", "tables": list(tables)}


def table(name):
    return {"name": name, "columns": []}


def check(monkeypatch, before, after, *, observed=None, entries=(), findings=()):
    import aurora.database.migration_safety as safety
    monkeypatch.setattr(safety, "list_history", lambda connection: entries)
    monkeypatch.setattr(safety, "review_history", lambda connection, *, entries: findings)
    review = create_review(before, after, migration_id="Change001")
    return inspect_migration_readiness(SimpleNamespace(dialect="sqlite"), review,
                                       reviewed_sha256=review["review_sha256"],
                                       observed_snapshot=observed if observed is not None else before)


def test_additive_proposal_is_still_non_executable(monkeypatch):
    result = check(monkeypatch, snapshot(), snapshot(table("articles")))
    assert result.operation_count == 1

    assert result.execution_allowed is False


def test_drift_rejected(monkeypatch):
    with pytest.raises(RuntimeError, match="differs"):
        check(monkeypatch, snapshot(), snapshot(table("articles")),
              observed=snapshot(table("other")))


def test_destructive_change_rejected(monkeypatch):
    with pytest.raises(PermissionError, match="additive"):
        check(monkeypatch, snapshot(table("articles")), snapshot())


def test_empty_plan_rejected(monkeypatch):
    with pytest.raises(ValueError, match="Empty"):
        check(monkeypatch, snapshot(), snapshot())


def test_dirty_journal_rejected(monkeypatch):
    with pytest.raises(RuntimeError, match="journal"):
        check(monkeypatch, snapshot(), snapshot(table("articles")), findings=("unresolved",))


def test_missing_journal_rejected(monkeypatch):
    import aurora.database.migration_safety as safety
    monkeypatch.setattr(safety, "list_history", lambda connection: (_ for _ in ()).throw(RuntimeError("missing journal")))
    review = create_review(snapshot(), snapshot(table("articles")), migration_id="Change001")

    with pytest.raises(RuntimeError, match="missing journal"):
        inspect_migration_readiness(SimpleNamespace(dialect="sqlite"), review,
                                    reviewed_sha256=review["review_sha256"],
                                    observed_snapshot=snapshot())


def test_wrong_acknowledgment_rejected():
    review = create_review(snapshot(), snapshot(table("articles")), migration_id="Change001")

    with pytest.raises(PermissionError):
        inspect_migration_readiness(SimpleNamespace(dialect="sqlite"), review,
                                    reviewed_sha256="0" * 64, observed_snapshot=snapshot())


def test_dialect_mismatch_rejected():
    review = create_review(snapshot(), snapshot(table("articles")), migration_id="Change001")

    with pytest.raises(ValueError, match="dialect"):
        inspect_migration_readiness(SimpleNamespace(dialect="mysql"), review,
                                    reviewed_sha256=review["review_sha256"],
                                    observed_snapshot=snapshot())


def test_modified_review_rejected():
    review = create_review(snapshot(), snapshot(table("articles")), migration_id="Change001")
    tampered = deepcopy(review)
    tampered["plan"]["operations"][0]["table"] = "other"

    with pytest.raises(ValueError):
        inspect_migration_readiness(SimpleNamespace(dialect="sqlite"), tampered,
                                    reviewed_sha256=review["review_sha256"],
                                    observed_snapshot=snapshot())
