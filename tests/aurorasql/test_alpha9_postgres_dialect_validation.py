"""Dialect-specific model validation before any PostgreSQL catalog access."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aurora.aurorasql import cli_init


@pytest.mark.parametrize("scheme,dialect", [
    ("sqlite", "sqlite"),
    ("sqlite+pysqlite", "sqlite"),
    ("postgresql+psycopg", "postgresql"),
    ("mysql+pymysql", "mysql"),
    ("mysql+mysqlconnector", "mysql"),
])
def test_configured_driver_selects_matching_ddl_dialect(scheme, dialect):
    assert cli_init._dialect_for_init_scheme(scheme) == dialect


def test_unknown_driver_fails_closed():
    with pytest.raises(ValueError, match="Unsupported database driver"):
        cli_init._dialect_for_init_scheme("postgresql+unknown")


def test_postgres_validation_occurs_before_connection(monkeypatch):
    config = Mock()
    config.resolve.return_value = "postgresql+psycopg://localhost/unused"
    monkeypatch.setattr(cli_init, "_load_config", lambda path: (config, None))
    monkeypatch.setattr(cli_init, "_select_models", lambda args: (["a:Model"], [object()]))
    seen = []

    def validate(models, dialect="sqlite"):
        seen.append(dialect)
        raise ValueError("PostgreSQL DDL rejected")

    import aurora.aurorasql.init_validation as validation
    monkeypatch.setattr(validation, "validate_init_models", validate)
    with pytest.raises(ValueError, match="PostgreSQL DDL rejected"):
        cli_init.run_init_check(SimpleNamespace(config="unused", database="default"))
    assert seen == ["postgresql"]
    config.connect.assert_not_called()
