"""Prove PostgreSQL's ambiguous startup failure cannot bypass catalog verification."""

from types import SimpleNamespace

import pytest

from aurora.database import cli_init
from aurora.database import cli_init as provisioning

URL = "postgresql+psycopg://reader:placeholder@localhost:5432/aurora_mvc_app"


class PGOperationalError(Exception):
    __module__ = "psycopg"
    sqlstate = None


class PGInvalidPassword(PGOperationalError):
    sqlstate = "28P01"


class Conn:
    dialect = "postgresql"

    def __init__(self, present=False):
        self.present = present

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def fetch_first(self, sql, params=None):
        if params is None:
            assert "current_database()" in sql
            return {"name": "postgres"}
        assert params == {"name": "aurora_mvc_app"}
        return {"name": "aurora_mvc_app"} if self.present else None


def test_ambiguous_postgres_error_requires_catalog_proof(monkeypatch):
    calls = []

    def connect(url):
        calls.append(url)
        return Conn()

    monkeypatch.setattr(provisioning, "connect", connect)
    assert provisioning.is_verified_missing_postgres(PGOperationalError("startup error"), URL)
    assert calls == ["postgresql+psycopg://reader:placeholder@localhost:5432/postgres"]


def test_existing_target_never_treated_as_missing(monkeypatch):
    monkeypatch.setattr(provisioning, "connect", lambda _: Conn(present=True))
    assert not provisioning.is_verified_missing_postgres(PGOperationalError(), URL)


def test_authentication_and_known_codes_never_trigger_fallback(monkeypatch):
    monkeypatch.setattr(
        provisioning, "connect", lambda _: pytest.fail("no catalog connection allowed")
    )
    assert not provisioning.is_verified_missing_postgres(PGInvalidPassword(), URL)
    assert not provisioning.is_verified_missing_postgres(ValueError("bad URL"), URL)


def test_unreachable_bootstrap_fails_closed(monkeypatch):
    def disconnected(_):
        raise PGOperationalError("unreachable")

    monkeypatch.setattr(provisioning, "connect", disconnected)
    assert not provisioning.is_verified_missing_postgres(PGOperationalError(), URL)


def test_init_reuses_catalog_evidence_and_one_approval(monkeypatch, tmp_path):
    calls = []

    def existing(args):
        calls.append(("existing", args.confirm))
        if len(calls) == 1:
            raise PGOperationalError("startup failure")
        return 0

    monkeypatch.setattr(cli_init, "_run_init_apply_existing", existing)
    monkeypatch.setattr(
        cli_init, "_load_config", lambda _: (SimpleNamespace(resolve=lambda _: URL), tmp_path)
    )
    monkeypatch.setattr(cli_init, "_select_models", lambda *_: ((), ()))
    monkeypatch.setattr("aurora.database.schema.validate_init_models", lambda *a, **k: ())
    monkeypatch.setattr(provisioning, "is_verified_missing_postgres", lambda *a: True)
    monkeypatch.setattr(
        provisioning,
        "provision_database",
        lambda *a, **kw: calls.append(("create", kw["confirm"])) or True,
    )
    args = SimpleNamespace(
        config=tmp_path / "aurora_databases.json", database="default", confirm=None, approve=None
    )
    assert cli_init.run_init_apply(args) == 0
    assert calls == [("existing", None), ("create", None), ("existing", "yes")]


def test_init_does_not_create_when_catalog_cannot_prove_absence(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli_init, "_run_init_apply_existing", lambda _: (_ for _ in ()).throw(PGOperationalError())
    )
    monkeypatch.setattr(
        cli_init, "_load_config", lambda _: (SimpleNamespace(resolve=lambda _: URL), tmp_path)
    )
    monkeypatch.setattr(provisioning, "is_verified_missing_postgres", lambda *a: False)
    monkeypatch.setattr(
        provisioning, "provision_database", lambda *a, **kw: pytest.fail("must not create")
    )
    with pytest.raises(PGOperationalError):
        cli_init.run_init_apply(
            SimpleNamespace(config=tmp_path / "config.json", database="default")
        )
