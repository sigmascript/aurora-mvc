"""Acceptance inventory never probes arbitrary or production databases."""

from contextlib import contextmanager
import pytest


class Connection:
    def __init__(self, dialect, username="aurorasql_test", owner="aurorasql_test"):
        self.dialect = dialect
        self.username = username
        self.owner = owner
        self.queries = []

    def fetch_first(self, query, params=None):
        self.queries.append(query)

        if self.dialect == "mysql":
            return {"user_name": self.username + "@localhost"}

        if "pg_namespace" in query:
            return {"owner_name": self.owner, "can_use": True, "can_create": True}

        return {"user_name": self.username, "owner_name": self.owner}

    def fetch_all(self, query, params=None):
        self.queries.append(query)
        return []


@pytest.mark.parametrize("dialect,scheme", [("mysql", "mysql+pymysql"),
                                              ("postgresql", "postgresql+psycopg")])
def test_acceptance_inventory_checks_configured_and_authenticated_test_identity(monkeypatch,
                                                                                dialect, scheme):
    from aurora.database import cli_init, server_live_inventory

    connection = Connection(dialect)

    class Config:
        def resolve(self, alias):
            assert alias == "accounting"
            return f"{scheme}://aurorasql_test:secret@localhost/aurorasql_test"

        @contextmanager
        def connect(self, alias):
            yield connection

    monkeypatch.setattr(server_live_inventory, "_load_config", lambda *_: (Config(), "/test"))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "mysql-test-identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "pg-test-identity")
    report = server_live_inventory.inspect_server_acceptance_target("ignored", "accounting")
    assert report["write_authorized"] is False
    assert report["authenticated_test_user"] == "aurorasql_test"
    assert report["existing_objects"] == []
    assert report["ownership_confirmed"] is (dialect == "postgresql")
    assert report["requires_manual_review"] is (dialect == "mysql")
    assert not any("INSERT " in query or "ALTER " in query for query in connection.queries)
    assert "secret" not in str(report)


def test_acceptance_inventory_refuses_production_config_without_connecting(monkeypatch):
    from aurora.database import server_live_inventory

    class Config:
        def resolve(self, alias):
            return "mysql+pymysql://production_user:secret@db.example/production"

        def connect(self, alias):
            pytest.fail("Production database must never be connected")

    monkeypatch.setattr(server_live_inventory, "_load_config", lambda *_: (Config(), "/test"))

    with pytest.raises(PermissionError, match="dedicated test"):
        server_live_inventory.inspect_server_acceptance_target("ignored", "default")


@pytest.mark.parametrize("dialect,scheme", [("mysql", "mysql+pymysql"),
                                              ("postgresql", "postgresql+psycopg")])
def test_acceptance_inventory_refuses_wrong_login_or_owner(monkeypatch, dialect, scheme):
    from aurora.database import cli_init, server_live_inventory

    connection = Connection(dialect, username="other", owner="other")

    class Config:
        def resolve(self, alias):
            return f"{scheme}://aurorasql_test@localhost/aurorasql_test"

        @contextmanager
        def connect(self, alias):
            yield connection

    monkeypatch.setattr(server_live_inventory, "_load_config", lambda *_: (Config(), "/test"))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "mysql-test-identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "pg-test-identity")

    with pytest.raises(PermissionError, match="test user"):
        server_live_inventory.inspect_server_acceptance_target("ignored", "default")


def test_acceptance_inventory_flags_existing_objects_for_manual_review(monkeypatch):
    from aurora.database import cli_init, server_live_inventory

    connection = Connection("postgresql")

    def existing(query, params=None):
        return [{"name": "old_table", "kind": "r", "owner_name": "aurorasql_test"}]

    connection.fetch_all = existing

    class Config:
        def resolve(self, alias):
            return "postgresql+psycopg://aurorasql_test@localhost/aurorasql_test"

        @contextmanager
        def connect(self, alias):
            yield connection

    monkeypatch.setattr(server_live_inventory, "_load_config", lambda *_: (Config(), "/test"))
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "pg-test-identity")
    report = server_live_inventory.inspect_server_acceptance_target("ignored", "accounting")
    assert report["existing_objects"] == [{"name": "old_table", "kind": "r"}]
    assert report["requires_manual_review"] is True
    assert report["write_authorized"] is False


def test_acceptance_inventory_accepts_verified_schema_grants_without_ownership(monkeypatch):
    from aurora.database import cli_init, server_live_inventory

    connection = Connection("postgresql")
    original_fetch = connection.fetch_first

    def postgres14_public(query, params=None):
        if "pg_namespace" in query:
            return {"owner_name": "postgres", "can_use": True, "can_create": True}

        return original_fetch(query, params)

    connection.fetch_first = postgres14_public

    class Config:
        def resolve(self, alias):
            return "postgresql+psycopg://aurorasql_test@localhost/aurorasql_test"

        @contextmanager
        def connect(self, alias):
            yield connection

    monkeypatch.setattr(server_live_inventory, "_load_config", lambda *_: (Config(), "/test"))
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "pg-test-identity")
    result = server_live_inventory.inspect_server_acceptance_target("ignored", "postgres_test")
    assert result["ownership_confirmed"] is True
    assert result["requires_manual_review"] is False
    assert result["write_authorized"] is False

    def no_create(query, params=None):
        if "pg_namespace" in query:
            return {"owner_name": "postgres", "can_use": True, "can_create": False}

        return original_fetch(query, params)

    connection.fetch_first = no_create

    with pytest.raises(PermissionError, match="active-schema access"):
        server_live_inventory.inspect_server_acceptance_target("ignored", "postgres_test")
