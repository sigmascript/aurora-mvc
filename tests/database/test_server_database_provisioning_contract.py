"""Missing-server-database provisioning is explicit and fail-closed."""

from types import SimpleNamespace

import pytest

from aurora.database import cli_init
from aurora.database import cli_init as provision

APP_MYSQL = "mysql+pymysql://app:secret@localhost:3306/app_db"
MAINT_MYSQL = "mysql+pymysql://admin:secret@localhost:3306/mysql"
APP_POSTGRES = "postgresql+psycopg://app:secret@localhost:5432/app_db"
MAINT_POSTGRES = "postgresql+psycopg://admin:secret@localhost:5432/postgres"


class FakeConnection:
    def __init__(self, dialect, maintenance_db):
        self.dialect, self.maintenance_db = dialect, maintenance_db
        self.created = False
        self.sql = []
        self.lookups = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def fetch_first(self, sql, params=None):
        self.sql.append(sql)
        if params is not None:
            self.lookups += 1
            assert params == {"name": "app_db"}
            return {"name": "app_db"} if self.created else None
        return {"name": self.maintenance_db}

    def execute_write(self, sql, params=None):
        self.sql.append(sql)
        assert sql.startswith("CREATE DATABASE ")
        self.created = True


@pytest.mark.parametrize(
    "app,dialect,database,ddl",
    [
        (APP_MYSQL, "mysql", "mysql", "CREATE DATABASE `app_db`"),
        (APP_POSTGRES, "postgresql", "postgres", 'CREATE DATABASE "app_db"'),
    ],
)
def test_init_creates_with_configured_credentials_once(monkeypatch, app, dialect, database, ddl):
    from urllib.parse import urlsplit

    connection = FakeConnection(dialect, database)

    def opening(url):
        configured, actual = urlsplit(app), urlsplit(url)
        assert configured.netloc == actual.netloc
        assert actual.path == "/" + database
        return connection

    monkeypatch.setattr(provision, "connect", opening)
    assert provision.provision_database(
        app, [SimpleNamespace(__table_name__="users")], alias="default", confirm="yes"
    )
    assert connection.lookups == 3
    assert connection.sql.count(ddl) == 1


def test_unsupported_auto_approval_is_refused_before_connection(monkeypatch):
    monkeypatch.setattr(provision, "connect", lambda *_: pytest.fail("no connection allowed"))
    with pytest.raises(ValueError, match="requires --confirm yes"):
        provision.provision_database(APP_MYSQL, (), alias="default", confirm="APPLY")


def test_ddl_uncertainty_preserves_evidence_and_never_retries(monkeypatch):
    conn = FakeConnection("mysql", "mysql")
    writes = []

    def failure(sql, *_):
        writes.append(sql)
        raise RuntimeError("private target server information")

    conn.execute_write = failure
    monkeypatch.setattr(provision, "connect", lambda *_: conn)
    with pytest.raises(provision.ProvisioningUncertain):
        provision.provision_database(APP_MYSQL, (), alias="default", confirm="yes")
    assert len(writes) == 1
    from aurora.cli import _database_failure

    diagnostic = _database_failure(
        provision.ProvisioningUncertain("private"),
        SimpleNamespace(config="aurora_databases.json"),
        "initialization",
    )
    assert "private" not in diagnostic
    assert "uncertain" in diagnostic


def test_race_before_ddl_is_refused(monkeypatch):
    conn = FakeConnection("mysql", "mysql")
    original = conn.fetch_first

    def raced(sql, params=None):
        if params is not None and conn.lookups == 1:
            conn.created = True
        return original(sql, params)

    conn.fetch_first = raced
    monkeypatch.setattr(provision, "connect", lambda *_: conn)
    with pytest.raises(RuntimeError, match="appeared before creation"):
        provision.provision_database(APP_MYSQL, (), alias="default", confirm="yes")
    assert not any(s.startswith("CREATE DATABASE") for s in conn.sql)


def test_missing_database_recognition_rejects_other_driver_errors():
    mysql = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    postgres = type(
        "InvalidCatalogName", (Exception,), {"__module__": "psycopg.errors", "sqlstate": "3D000"}
    )
    assert provision.is_missing_database(mysql(1049, "secret"), "mysql+pymysql")
    assert not provision.is_missing_database(mysql(1045, "secret"), "mysql+pymysql")
    assert provision.is_missing_database(postgres("secret"), "postgresql+psycopg")
    assert not provision.is_missing_database(mysql(1049), "postgresql+psycopg")


def test_init_uses_provisioning_only_after_missing_database(monkeypatch, tmp_path):
    mysql_error = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    calls = []

    def init_existing(args):
        calls.append("init")
        if len(calls) == 1:
            raise mysql_error(1049, "private")
        return 0

    monkeypatch.setattr(cli_init, "_run_init_apply_existing", init_existing)
    monkeypatch.setattr(
        cli_init,
        "_load_config",
        lambda *_: (SimpleNamespace(resolve=lambda _: APP_MYSQL), tmp_path),
    )
    monkeypatch.setattr(cli_init, "_select_models", lambda *_: ((), ()))
    monkeypatch.setattr("aurora.database.schema.validate_init_models", lambda *a, **kw: ())
    monkeypatch.setattr(
        provision, "provision_database", lambda *a, **kw: calls.append("provision") or True
    )
    args = SimpleNamespace(
        config=tmp_path / "aurora_databases.json", database="default", confirm="yes"
    )
    assert cli_init.run_init_apply(args) == 0
    assert calls == ["init", "provision", "init"]


def test_no_missing_database_no_provisioning(monkeypatch):
    monkeypatch.setattr(cli_init, "_run_init_apply_existing", lambda *_: 0)
    monkeypatch.setattr(
        provision, "provision_database", lambda *_a, **_k: pytest.fail("unexpected creation")
    )
    assert cli_init.run_init_apply(SimpleNamespace()) == 0
