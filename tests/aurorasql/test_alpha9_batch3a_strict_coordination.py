"""Safety regression: conservative catalog diagnostics never authorize DDL."""

from types import SimpleNamespace

import pytest

from aurora.aurorasql.strict_schema_evidence import assess_strict_schema
from aurora.aurorasql.inspection import ColumnInfo, TableInfo
from aurora.aurorasql.coordinated_readonly_preflight import inspect_coordinated_readiness


def snapshot():
    return {"format": "aurorasql.schema.v1", "dialect": "sqlite", "tables": [{
        "name": "items", "columns": [{
            "name": "id", "type": "int", "length": None, "primary_key": True,
            "nullable": False, "unique": False, "references": None,
            "python_default": {"kind": "literal", "value": None},
            "server_default": None,
        }],
    }]}


def test_matching_catalog_still_cannot_certify_full_equivalence(monkeypatch):
    import aurora.aurorasql.strict_schema_evidence as strict
    monkeypatch.setattr(strict, "inspect_table", lambda conn, name: TableInfo(
        name, True, (ColumnInfo("id", "INTEGER", False, True),),
        constraints_verified=True, uniqueness_verified=True,
    ))
    result = assess_strict_schema(SimpleNamespace(dialect="sqlite"), snapshot())
    assert not result.complete_schema_verified
    assert not result.execution_allowed
    assert any("Full catalog equivalence" in item for item in result.findings)


def test_missing_table_reported(monkeypatch):
    import aurora.aurorasql.strict_schema_evidence as strict
    monkeypatch.setattr(strict, "inspect_table", lambda conn, name: TableInfo(name, False, ()))
    result = assess_strict_schema(SimpleNamespace(dialect="sqlite"), snapshot())
    assert "Missing table: items" in result.findings


def test_dialect_mismatch_rejected():
    with pytest.raises(ValueError, match="dialect"):
        assess_strict_schema(SimpleNamespace(dialect="mysql"), snapshot())


def test_lock_failure_prevents_all_checks(monkeypatch):
    import aurora.aurorasql.coordinated_readonly_preflight as coordinated
    from contextlib import contextmanager
    @contextmanager
    def failed_lock(connection):
        raise RuntimeError("lock unavailable")
        yield
    monkeypatch.setattr(coordinated, "migration_lock", failed_lock)
    with pytest.raises(RuntimeError, match="lock unavailable"):
        inspect_coordinated_readiness(SimpleNamespace(dialect="sqlite"), {}, reviewed_sha256="x")


def test_coordinated_diagnostic_never_grants_permission(monkeypatch):
    import aurora.aurorasql.coordinated_readonly_preflight as coordinated
    from contextlib import contextmanager
    @contextmanager
    def lock(connection):
        yield
    monkeypatch.setattr(coordinated, "migration_lock", lock)
    monkeypatch.setattr(coordinated, "inspect_live_migration_readiness", lambda *a, **k: SimpleNamespace(migration_id="M1", dialect="sqlite"))
    monkeypatch.setattr(coordinated, "assess_strict_schema", lambda *a: SimpleNamespace(findings=("catalog incomplete",)))
    result = inspect_coordinated_readiness(SimpleNamespace(dialect="sqlite"), {"before": snapshot()}, reviewed_sha256="x")
    assert result.coordination_attempted
    assert not result.execution_allowed
    assert not result.complete_schema_verified
