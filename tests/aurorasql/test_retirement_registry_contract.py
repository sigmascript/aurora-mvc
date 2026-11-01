"""Behavioral compatibility gates for AuroraSQL alias and registry ownership."""

import pytest

from aurora.aurorasql.configuration import DatabaseConfiguration
from aurora.aurorasql.database_registry import DatabaseRegistry


def configuration(tmp_path):
    return DatabaseConfiguration({
        "default": {"url": f"sqlite:///{tmp_path / 'primary.db'}"},
        "audit": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
    })


def test_registry_is_lazy_and_alias_scoped(tmp_path):
    registry = DatabaseRegistry(configuration(tmp_path))
    assert registry.initialized_aliases() == ()
    with registry:
        primary = registry.get()
        assert registry.initialized_aliases() == ("default",)
        assert registry.get("default") is primary
        audit = registry.get("audit")
        assert audit is not primary
        primary.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY)")
        primary.execute("INSERT INTO ledger (id) VALUES (1)")
        with pytest.raises(Exception):
            audit.execute("SELECT id FROM ledger")
    assert registry.initialized_aliases() == ()
    with pytest.raises(RuntimeError, match="closed"):
        registry.get()


def test_registry_rollback_does_not_affect_another_alias(tmp_path):
    with DatabaseRegistry(configuration(tmp_path)) as registry:
        primary = registry.get("default")
        audit = registry.get("audit")
        for connection in (primary, audit):
            connection.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY)")
        with audit.transaction():
            audit.execute("INSERT INTO ledger (id) VALUES (7)")
        with pytest.raises(ValueError, match="rollback"):
            with primary.transaction():
                primary.execute("INSERT INTO ledger (id) VALUES (9)")
                raise ValueError("rollback")
        assert primary.execute("SELECT COUNT(*) AS n FROM ledger")[0]["n"] == 0
        assert audit.execute("SELECT COUNT(*) AS n FROM ledger")[0]["n"] == 1


def test_close_attempts_all_aliases_even_if_one_fails(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    registry = DatabaseRegistry(config)
    closed = []

    class Connection:
        def __init__(self, alias, fails=False):
            self.alias = alias
            self.fails = fails

        def close(self):
            closed.append(self.alias)
            if self.fails:
                raise OSError("close failed")

    def connect(alias="default"):
        return Connection(alias, fails=(alias == "default"))

    monkeypatch.setattr(config, "connect", connect)
    registry.get("default")
    registry.get("audit")
    with pytest.raises(OSError, match="close failed"):
        registry.close()
    assert closed == ["default", "audit"]
    assert registry.initialized_aliases() == ()
    registry.close()
    with pytest.raises(RuntimeError, match="closed"):
        registry.get("audit")


def test_unknown_alias_never_opens_connection(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    registry = DatabaseRegistry(config)

    def unexpected_connect(alias="default"):
        pytest.fail("Unknown alias attempted a connection")

    monkeypatch.setattr(config, "connect", unexpected_connect)
    with pytest.raises(ValueError, match="Unknown database alias"):
        registry.get("unknown")
    assert registry.initialized_aliases() == ()
    registry.close()
