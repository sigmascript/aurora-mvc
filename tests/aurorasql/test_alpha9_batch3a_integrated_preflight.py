"""Integration boundary tests: catalog evidence never authorizes execution."""

from types import SimpleNamespace

import pytest

from aurora.aurorasql.live_migration_preflight import inspect_live_migration_readiness
from aurora.aurorasql.review_artifacts import create_review


def snapshot(*tables, dialect="sqlite"):
    return {"format": "aurorasql.schema.v1", "dialect": dialect,
            "tables": [{"name": name, "columns": []} for name in tables]}


def check(monkeypatch, *, found=True, dialect="sqlite", findings=(), journal=()):
    import aurora.aurorasql.live_schema_evidence as live
    import aurora.aurorasql.migration_safety as safety
    from aurora.aurorasql.inspection import TableInfo

    def inspect(_connection, name):
        return TableInfo(name, found, ())

    monkeypatch.setattr(live, "inspect_table", inspect)
    monkeypatch.setattr(safety, "list_history", lambda connection: journal)
    monkeypatch.setattr(safety, "review_history", lambda connection, *, entries: findings)
    review = create_review(snapshot("existing"), snapshot("existing", "new"),
                           migration_id="AddNew")
    return inspect_live_migration_readiness(
        SimpleNamespace(dialect=dialect), review,
        reviewed_sha256=review["review_sha256"])


def test_consistent_structure_is_still_not_authorized(monkeypatch):
    result = check(monkeypatch)
    assert result.checked_tables == ("existing",)
    assert result.operation_count == 1
    assert result.execution_allowed is False
    assert result.complete_schema_verified is False
    assert result.coordination_verified is False


def test_missing_live_table_blocks_preflight(monkeypatch):
    with pytest.raises(RuntimeError, match="Missing table"):
        check(monkeypatch, found=False)


def test_journal_inconsistency_blocks_preflight(monkeypatch):
    with pytest.raises(RuntimeError, match="journal"):
        check(monkeypatch, findings=("unresolved",))


def test_wrong_connection_dialect_blocks_preflight(monkeypatch):
    with pytest.raises(ValueError, match="dialect"):
        check(monkeypatch, dialect="mysql")


def test_invalid_review_acknowledgment_blocks_preflight(monkeypatch):
    import aurora.aurorasql.live_schema_evidence as live
    from aurora.aurorasql.inspection import TableInfo
    monkeypatch.setattr(live, "inspect_table", lambda conn, name: TableInfo(name, True, ()))
    review = create_review(snapshot("existing"), snapshot("existing", "new"), migration_id="AddNew")
    with pytest.raises(PermissionError):
        inspect_live_migration_readiness(SimpleNamespace(dialect="sqlite"), review,
                                         reviewed_sha256="0" * 64)
