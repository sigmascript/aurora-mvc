"""Bounded recovery: physical additions are evidence, not authorization for DDL."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import initialization as subject


class DB:
    dialect = "mysql"
    _transaction_active = False
    _migration_lock_active = False

    def __init__(self):
        self.writes = []

    def fetch_all(self, sql):
        assert "SELECT id,title,snapshot,date,warnings FROM __migrations__" in sql
        return [
            {
                "id": 1,
                "title": "Initial",
                "snapshot": "20261117-224012.json",
                "date": "2026-11-17T19:10:12+00:00",
                "warnings": None,
            }
        ]

    def execute_write(self, sql, data):
        assert sql.startswith("INSERT INTO __migrations__")
        self.writes.append(data)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    directory = tmp_path / "__migrations__" / "data_mysql"
    directory.mkdir(parents=True)
    old_model = {
        "name": "mysql_item",
        "columns": [
            {"name": "id"},
        ],
        "create_sql_preview": "old",
    }
    new_model = {
        "name": "mysql_item",
        "columns": [
            {"name": "id"},
            {
                "name": "note",
                "nullable": True,
                "primary_key": False,
                "unique": False,
                "references": None,
                "server_default": None,
                "python_default": {"kind": "literal", "value": None},
            },
        ],
        "create_sql_preview": "new",
    }
    old_phys = {
        "table": "mysql_item",
        "columns": [["id", "BIGINT", False, True, None]],
        "indexes": ["PRIMARY"],
        "unique_columns": [],
        "foreign_keys": [],
    }
    new_phys = {
        **old_phys,
        "columns": old_phys["columns"] + [["note", "VARCHAR(80)", True, False, None]],
    }
    (directory / "20261117-224012.json").write_text(
        json.dumps(
            {
                "database": "data_mysql",
                "engine": "mysql",
                "title": "Initial",
                "models": [old_model],
                "physical_schema": [old_phys],
            }
        )
    )
    monkeypatch.setattr(subject, "history_table", lambda _: "__migrations__")
    monkeypatch.setattr(subject, "_verify_table", lambda *_: None)
    monkeypatch.setattr(subject, "_directory", lambda *_: directory)
    monkeypatch.setattr(subject, "_physical", lambda _: [new_phys])
    monkeypatch.setattr(subject, "schema_snapshot", lambda *a, **kw: {"tables": [new_model]})
    monkeypatch.setattr(subject, "verify_server_declared_schema", lambda *a, **kw: None)
    return directory, new_model, new_phys


def test_verified_one_column_preflight_is_read_only(setup, tmp_path):
    db = DB()
    assert (
        len(
            subject.verify_interrupted_addition(
                db, tmp_path, "data_mysql", [object()], "mysql_item.note"
            )
        )
        == 1
    )
    assert db.writes == []


def test_recovery_refuses_unrelated_physical_drift(setup, tmp_path, monkeypatch):
    directory, model, physical = setup
    monkeypatch.setattr(
        subject, "_physical", lambda _: [{**physical, "indexes": ["PRIMARY", "unexpected"]}]
    )
    db = DB()
    with pytest.raises(RuntimeError, match="Physical drift"):
        subject.verify_interrupted_addition(
            db, tmp_path, "data_mysql", [object()], "mysql_item.note"
        )
    assert db.writes == []


def test_recovery_refuses_unrecorded_snapshot(setup, tmp_path):
    directory, _, _ = setup
    (directory / "20261117-224013.json").write_text("{}")
    db = DB()
    with pytest.raises(RuntimeError, match="Unrecorded"):
        subject.verify_interrupted_addition(
            db, tmp_path, "data_mysql", [object()], "mysql_item.note"
        )
    assert db.writes == []


def test_recovery_refuses_incorrect_target(setup, tmp_path):
    db = DB()
    with pytest.raises(RuntimeError, match="Model change"):
        subject.verify_interrupted_addition(
            db, tmp_path, "data_mysql", [object()], "mysql_item.other"
        )
    assert db.writes == []


def test_recovery_writes_history_not_ddl(setup, tmp_path, monkeypatch):
    from aurora.database import cli_init

    directory, _, _ = setup
    db = DB()

    @contextmanager
    def lock(connection):
        yield

    monkeypatch.setattr(subject, "migration_lock", lock)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *a: "verified")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *a: "verified")
    saved = directory / "20261117-224100.json"
    monkeypatch.setattr(subject, "_write_snapshot", lambda *a: saved)
    monkeypatch.setattr(subject, "read_history", lambda *a: ([{"id": 1}, *db.writes], {}))
    monkeypatch.setattr(subject, "_verify_backups", lambda *a: None)
    result = subject.reconcile_interrupted_addition(
        db,
        tmp_path,
        "data_mysql",
        [object()],
        "mysql_item.note",
        "Add note",
        expected_identity="verified",
        parsed=object(),
        configured_url="mysql+pymysql://localhost/data_mysql",
    )
    assert result == saved
    assert len(db.writes) == 1
    assert db.writes[0]["snapshot"] == saved.name
    assert db.writes[0]["title"] == "Add note"


def test_recovery_refuses_changed_identity_before_writes(setup, tmp_path, monkeypatch):
    from aurora.database import cli_init

    db = DB()

    @contextmanager
    def lock(connection):
        yield

    monkeypatch.setattr(subject, "migration_lock", lock)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *a: "different")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *a: "different")
    with pytest.raises(RuntimeError, match="identity changed"):
        subject.reconcile_interrupted_addition(
            db,
            tmp_path,
            "data_mysql",
            [object()],
            "mysql_item.note",
            "Add note",
            expected_identity="verified",
            parsed=object(),
            configured_url="mysql+pymysql://localhost/data_mysql",
        )
    assert db.writes == []


def test_recovery_cli_rejects_unsafe_flags_before_database_access(monkeypatch):
    """The migrate CLI retains its early recovery refusal after ownership move."""
    from aurora.database import cli_migrate

    def unexpected_access(*args, **kwargs):
        raise AssertionError("configuration/database access should not occur")

    monkeypatch.setattr(cli_migrate, "_load_config", unexpected_access)
    args = SimpleNamespace(
        confirm="APPLY",
        apply=False,
        approve=None,
        backup=None,
        name=None,
        changes=None,
        review_file=None,
        schema=None,
        table=None,
        column=None,
    )
    with pytest.raises(ValueError, match="Recovery confirmation accepts only yes or y"):
        cli_migrate.run_interrupted_reconciliation(args)
