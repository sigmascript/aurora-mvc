"""F06: project-wide physical identity is the configured database name."""

import pytest

from aurora.database.configuration import DatabaseConfiguration


def test_distinct_names_across_all_engines_resolve_without_connecting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = DatabaseConfiguration(
        {
            "data": {"engine": "sqlite"},
            "data_mysql": {"engine": "mysql", "username": "root"},
            "data_postgres": {"engine": "postgresql", "username": "postgres"},
        }
    )
    assert config.names() == ("data", "data_mysql", "data_postgres")
    assert config.resolve("data") == "sqlite:///data.sqlite3"
    assert config.resolve("data_mysql").endswith("/data_mysql")
    assert config.resolve("data_postgres").endswith("/data_postgres")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "name,url",
    [
        ("data", "sqlite:///other.sqlite3"),
        ("data", "sqlite:///folder/data.sqlite3"),
        ("data", "sqlite:////absolute/data.sqlite3"),
        ("data_mysql", "mysql+pymysql://root@HOST/another"),
        ("data_postgres", "postgresql+psycopg://postgres@host/another"),
    ],
)
def test_url_based_aliasing_is_refused(name, url):
    with pytest.raises(ValueError, match="configured name|project-relative"):
        DatabaseConfiguration({name: {"url": url}})


def test_env_url_cannot_rebind_database_name(monkeypatch):
    monkeypatch.setenv("AURORA_TEST_F06_URL", "sqlite:///other.sqlite3")
    with pytest.raises(ValueError, match="configured name"):
        DatabaseConfiguration({"data": {"url_env": "AURORA_TEST_F06_URL"}})


def test_names_cannot_be_reused_in_single_mapping():
    # Python dicts cannot contain repeated keys; valid different names do not collide.
    with pytest.raises(ValueError, match="lowercase identifiers"):
        DatabaseConfiguration({"data.sqlite3": {"engine": "sqlite"}})


def test_secret_not_in_configuration_representation():
    config = DatabaseConfiguration(
        {"warehouse": {"engine": "mysql", "username": "root", "password": "private-example"}}
    )
    assert "private-example" not in repr(config)


def test_bad_password_environment_does_not_open_connections(monkeypatch):
    monkeypatch.delenv("AURORA_NONEXISTENT_TEST", raising=False)
    with pytest.raises(ValueError, match="AURORA_NONEXISTENT_TEST"):
        DatabaseConfiguration(
            {
                "warehouse": {
                    "engine": "mysql",
                    "username": "root",
                    "password_env": "AURORA_NONEXISTENT_TEST",
                }
            }
        )
