"""Fail-closed composition tests for the non-executable migration gate."""

from types import SimpleNamespace

import pytest

from aurora.database import migration_gate as gate


def _review(dialect="sqlite"):
    return {"plan": {"id": "MigrationOne", "dialect": dialect}, "review_sha256": "a" * 64}


def _scope(dialect="sqlite"):
    return SimpleNamespace(migration_id="MigrationOne", dialect=dialect,
                           review_sha256="a" * 64, blockers=("Scope pending",))


def _coordinated():
    return SimpleNamespace(migration_id="MigrationOne", findings=("Incomplete catalog",))


def _setup(monkeypatch, dialect="sqlite"):
    monkeypatch.setattr(gate, "assess_execution_scope", lambda *a, **kw: _scope(dialect))
    monkeypatch.setattr(gate, "assess_backend_readiness", lambda *a: SimpleNamespace(blockers=("Backend pending",)))
    monkeypatch.setattr(gate, "inspect_coordinated_readiness", lambda *a, **kw: _coordinated())


def test_sqlite_missing_intent_journal_blocks(monkeypatch):
    _setup(monkeypatch)
    result = gate.inspect_migration_gate(SimpleNamespace(dialect="sqlite"), _review(), reviewed_sha256="a" * 64)
    assert result.execution_allowed is False

    assert result.complete_schema_verified is False

    assert result.execution_coordination_verified is False

    assert any("not inspected" in b for b in result.blockers)

    assert any("Incomplete catalog" in b for b in result.blockers)


def test_sqlite_unresolved_intent_blocks(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(gate, "assess_sqlite_migration_readiness", lambda *a, **kw: SimpleNamespace(
        migration_id="MigrationOne", blockers=("Unresolved intent",)))
    result = gate.inspect_migration_gate(SimpleNamespace(dialect="sqlite"), _review(),
                                         reviewed_sha256="a" * 64, sqlite_intent_connection=object())
    assert "Unresolved intent" in result.blockers

    assert result.execution_allowed is False


def test_mysql_requires_its_own_journal(monkeypatch):
    _setup(monkeypatch, "mysql")
    result = gate.inspect_migration_gate(SimpleNamespace(dialect="mysql"), _review("mysql"),
                                         reviewed_sha256="a" * 64)
    assert any("mysql durable" in b for b in result.blockers)

    assert result.execution_allowed is False


def test_postgresql_requires_its_own_journal(monkeypatch):
    _setup(monkeypatch, "postgresql")
    result = gate.inspect_migration_gate(SimpleNamespace(dialect="postgresql"), _review("postgresql"),
                                         reviewed_sha256="a" * 64)
    assert any("postgresql durable" in b for b in result.blockers)


def test_dialect_mismatch_fails_before_catalog(monkeypatch):
    _setup(monkeypatch)

    with pytest.raises(ValueError, match="dialect"):
        gate.inspect_migration_gate(SimpleNamespace(dialect="mysql"), _review(),
                                    reviewed_sha256="a" * 64)


def test_cross_dialect_journal_refused(monkeypatch):
    _setup(monkeypatch, "mysql")

    with pytest.raises(ValueError, match="SQLite journal"):
        gate.inspect_migration_gate(SimpleNamespace(dialect="mysql"), _review("mysql"),
                                    reviewed_sha256="a" * 64, sqlite_intent_connection=object())


def test_mismatched_live_evidence_fails(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(gate, "inspect_coordinated_readiness", lambda *a, **kw: SimpleNamespace(
        migration_id="Other", findings=()))

    with pytest.raises(RuntimeError, match="identity"):
        gate.inspect_migration_gate(SimpleNamespace(dialect="sqlite"), _review(),
                                    reviewed_sha256="a" * 64)


def test_missing_sqlite_journal_fails_closed(monkeypatch):
    _setup(monkeypatch)
    def missing(*args, **kwargs):
        raise RuntimeError("Migration intent journal is missing")
    monkeypatch.setattr(gate, "assess_sqlite_migration_readiness", missing)

    with pytest.raises(RuntimeError, match="missing"):
        gate.inspect_migration_gate(SimpleNamespace(dialect="sqlite"), _review(),
                                    reviewed_sha256="a" * 64, sqlite_intent_connection=object())
