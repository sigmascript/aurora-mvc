"""PostgreSQL proposal fingerprints are advisory and cannot authorize DDL."""

from types import SimpleNamespace

import pytest

from aurora.database.cli_init import _postgres_approval_digest


class _ModelA:
    __table_name__ = "alpha"


class _ModelB:
    __table_name__ = "beta"


def test_postgres_proposal_is_deterministic_and_order_sensitive(monkeypatch):
    from aurora.database import schema

    monkeypatch.setattr(schema, "create_table_sql", lambda model, dialect: f"CREATE {model.__table_name__} {dialect}")
    a = _postgres_approval_digest([_ModelA, _ModelB], "default", "localhost:5432/test")
    assert len(a) == 64
    assert a == _postgres_approval_digest([_ModelA, _ModelB], "default", "localhost:5432/test")
    assert a != _postgres_approval_digest([_ModelB, _ModelA], "default", "localhost:5432/test")
    assert a != _postgres_approval_digest([_ModelA, _ModelB], "other", "localhost:5432/test")
    assert a != _postgres_approval_digest([_ModelA, _ModelB], "default", "localhost:5432/other")


def test_postgres_proposal_refuses_missing_identity():
    with pytest.raises(ValueError, match="identity"):
        _postgres_approval_digest([], "default", "")
