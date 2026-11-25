"""The standard MySQL/PostgreSQL init creates missing targets without extra settings."""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from aurora.database import cli_init
from aurora.database import cli_init as provision


@pytest.mark.parametrize(
    "url,bootstrap,ddl",
    [
        (
            "mysql+pymysql://root:placeholder@localhost:3306/aurora_mvc_app",
            "mysql",
            "CREATE DATABASE `aurora_mvc_app`",
        ),
        (
            "postgresql+psycopg://tester:placeholder@localhost:5432/aurora_mvc_app",
            "postgres",
            'CREATE DATABASE "aurora_mvc_app"',
        ),
    ],
)
def test_missing_database_uses_existing_configured_credentials_without_extra_setting(
    monkeypatch, url, bootstrap, ddl
):

    class Conn:
        dialect = "mysql" if bootstrap == "mysql" else "postgresql"

        def __init__(self):
            self.created = False
            self.writes = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def fetch_first(self, sql, params=None):
            if params is None:
                return {"name": bootstrap}
            assert params == {"name": "aurora_mvc_app"}
            return {"name": "aurora_mvc_app"} if self.created else None

        def execute_write(self, sql):
            self.writes.append(sql)
            self.created = True

    connection = Conn()

    def check_url(candidate):
        configured, changed = urlsplit(url), urlsplit(candidate)
        assert configured.scheme == changed.scheme
        assert configured.netloc == changed.netloc  # includes same username/password/host/port
        assert changed.path == "/" + bootstrap
        return connection

    monkeypatch.setattr(provision, "connect", check_url)
    models = (SimpleNamespace(__table_name__="users"),)
    assert provision.provision_database(url, models, alias="default", confirm="yes")
    assert connection.writes == [ddl]


def test_init_missing_database_needs_no_provision_env_and_uses_one_approval(monkeypatch, tmp_path):
    mysql_error = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    calls = []

    def existing(args):
        calls.append(("init", args.confirm))
        if len(calls) == 1:
            raise mysql_error(1049, "private URL")
        return 0

    monkeypatch.setattr(cli_init, "_run_init_apply_existing", existing)
    monkeypatch.setattr(
        cli_init,
        "_load_config",
        lambda *_: (
            SimpleNamespace(
                resolve=lambda _: "mysql+pymysql://root:placeholder@localhost:3306/app_db",
            ),
            tmp_path,
        ),
    )
    monkeypatch.setattr(cli_init, "_select_models", lambda *_: ((), ()))
    monkeypatch.setattr("aurora.database.schema.validate_init_models", lambda *a, **k: ())
    monkeypatch.setattr(
        provision,
        "provision_database",
        lambda *a, **kw: calls.append(("create", kw["confirm"], kw["alias"])) or True,
    )
    args = SimpleNamespace(
        config=tmp_path / "aurora_databases.json", database="default", confirm=None, approve=None
    )
    assert cli_init.run_init_apply(args) == 0
    assert calls == [("init", None), ("create", None, "default"), ("init", "yes")]
    assert args.confirm is None


def test_missing_database_refuses_noncreation_connection_errors(monkeypatch, tmp_path):
    mysql_error = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    monkeypatch.setattr(
        cli_init,
        "_run_init_apply_existing",
        lambda _: (_ for _ in ()).throw(mysql_error(1045, "private")),
    )
    monkeypatch.setattr(
        cli_init,
        "_load_config",
        lambda *_: (
            SimpleNamespace(
                resolve=lambda _: "mysql+pymysql://root:placeholder@localhost:3306/app_db",
            ),
            tmp_path,
        ),
    )
    monkeypatch.setattr(
        provision, "provision_database", lambda *a, **kw: pytest.fail("must not attempt creation")
    )
    with pytest.raises(mysql_error):
        cli_init.run_init_apply(SimpleNamespace(config=tmp_path / "db.json", database="default"))


@pytest.mark.parametrize(
    "bad",
    [
        "mysql+pymysql://root:placeholder@localhost:3306/mysql",
        "postgresql+psycopg://user:placeholder@localhost:5432/postgres",
        "mysql+pymysql://root:placeholder@localhost:3306/contains-dash",
        "mysql+pymysql://root:placeholder@localhost:3306/app_db?charset=utf8",
    ],
)
def test_invalid_bootstrap_target_refused_before_connect(monkeypatch, bad):
    monkeypatch.setattr(provision, "connect", lambda _: pytest.fail("unexpected connection"))
    with pytest.raises(ValueError):
        provision.provision_database(bad, (), alias="default", confirm="yes")


def test_create_requires_explicit_confirmation_before_connection(monkeypatch):
    monkeypatch.setattr(provision, "connect", lambda _: pytest.fail("unexpected connection"))
    with pytest.raises(ValueError, match="--confirm yes"):
        provision.provision_database(
            "mysql+pymysql://root:placeholder@localhost:3306/app_db",
            (),
            alias="default",
            confirm="APPLY",
        )
