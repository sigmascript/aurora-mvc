"""SQLite Initial history is recorded once and never repairs itself on reads."""

import sqlite3
from argparse import Namespace
import json

import pytest

from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.cli_history import _read_history
from aurora.database.cli_history import run_history
from aurora.database.core import Connection
from aurora.database.history import list_history
from aurora.database.migration_timeline import inspect_initial, verify_initial_models
from aurora.database.migration_journal_v2 import record_prepared_intent


class Article(Model):
    __module__ = "tests.database.test_initial_timeline"
    __table__ = "initial_articles"
    id = column(int, primary_key=True)
    title = column(str, default="draft")


def _initialize(path, alias="default"):
    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (Article,), alias)


def test_initial_history_is_one_verified_record_without_schema_writes_on_read(tmp_path):
    path = tmp_path / "initial.sqlite3"
    sqlite3.connect(path).close()
    _initialize(path)

    with sqlite3.connect(path) as raw:
        before = raw.total_changes
        report = inspect_initial(raw, "default")
        assert report["number"] == 1
        assert report["name"] == "Initial"
        assert report["status"] == "applied"
        assert report["physical_schema_matches"] is True
        assert report["physical_history_consistent"] is True
        assert report["verified_current"] is False
        assert raw.total_changes == before

        rows = raw.execute("SELECT migration_id, name, state FROM aurorasql_migration_timeline").fetchall()
        assert rows == [(1, "Initial", "applied")]


def test_history_displays_unpadded_number_and_human_title(tmp_path, capsys):
    path = tmp_path / "named.sqlite3"
    sqlite3.connect(path).close()

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (Article,), title="Create articles")

    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///named.sqlite3"}}}), encoding="utf-8")
    args = Namespace(config=str(config), database="default", verify_models=False,
                     format="text")
    assert run_history(args) == 0
    output = capsys.readouterr().out
    assert "1 Create articles - APPLIED" in output
    assert "001" not in output


def test_drift_blocks_verified_current_without_mutating_history(tmp_path):
    path = tmp_path / "drift.sqlite3"
    sqlite3.connect(path).close()
    _initialize(path)

    with sqlite3.connect(path) as raw:
        raw.execute('ALTER TABLE "initial_articles" ADD COLUMN extra TEXT')
        before = raw.execute('SELECT * FROM "aurorasql_migration_timeline"').fetchall()
        report = inspect_initial(raw, "default")
        assert report["physical_schema_matches"] is False
        assert report["verified_current"] is False
        assert raw.execute('SELECT * FROM "aurorasql_migration_timeline"').fetchall() == before

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        report = _read_history(connection, "default")
        assert report["timeline"][0]["verified_current"] is False
        assert any(f["kind"] == "initial_not_verified" for f in report["findings"])


def test_initial_is_bound_to_database_alias(tmp_path):
    path = tmp_path / "aliases.sqlite3"
    sqlite3.connect(path).close()
    _initialize(path, "accounts")

    with sqlite3.connect(path) as raw:
        assert inspect_initial(raw, "accounts")["physical_history_consistent"] is True

        with pytest.raises(RuntimeError, match="another alias"):
            inspect_initial(raw, "default")


def test_legacy_database_is_not_auto_adopted(tmp_path):
    path = tmp_path / "legacy.sqlite3"

    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE initial_articles (id INTEGER PRIMARY KEY)')
        assert inspect_initial(raw, "default") is None
        assert raw.execute("SELECT name FROM sqlite_master WHERE name='aurorasql_migration_timeline'").fetchone() is None


def test_lookalike_timeline_without_check_constraints_is_refused(tmp_path):
    path = tmp_path / "lookalike.sqlite3"

    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE aurorasql_migration_timeline ('
                    'migration_id INTEGER PRIMARY KEY, name TEXT, database_alias TEXT, '
                    'review_sha256 TEXT, schema_sha256 TEXT, state TEXT)')

        with pytest.raises(RuntimeError, match="definition"):
            inspect_initial(raw, "default")


def test_unintegrated_successor_intent_blocks_current(tmp_path):
    path = tmp_path / "successor.sqlite3"
    sqlite3.connect(path).close()
    _initialize(path)

    with sqlite3.connect(path, isolation_level=None) as raw:
        digest = "a" * 64
        record_prepared_intent(raw, migration_id="SchemaChange1", review_sha256=digest,
                               before_sha256=digest, after_sha256=digest, confirm=True)
        report = inspect_initial(raw, "default")
        assert report["physical_schema_matches"] is True
        assert report["unintegrated_successor_intents"] is True
        assert report["verified_current"] is False


def test_model_verification_requires_exact_declared_schema(tmp_path):
    path = tmp_path / "models.sqlite3"
    sqlite3.connect(path).close()
    _initialize(path)

    class ModifiedArticle(Model):
        __table__ = "initial_articles"
        id = column(int, primary_key=True)
        title = column(str, default="changed")

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        initial = inspect_initial(connection._raw, "default")
        entries = list_history(connection)
        assert verify_initial_models(connection._raw, (Article,), initial, entries) is True
        assert verify_initial_models(connection._raw, (ModifiedArticle,), initial, entries) is False


def test_public_read_only_history_verifies_explicit_model_manifest(tmp_path, capsys):
    path = tmp_path / "public.sqlite3"
    sqlite3.connect(path).close()
    _initialize(path)
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///public.sqlite3"}}
    }), encoding="utf-8")
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["tests.database.test_initial_timeline:Article"]}
    }), encoding="utf-8")
    args = Namespace(config=str(tmp_path / "aurora_databases.json"), database="default",
                     models_config="aurora_models.json", model=[], verify_models=True,
                     format="json")

    assert run_history(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["timeline"][0]["verified_current"] is True

    with sqlite3.connect(path) as raw:
        raw.execute('ALTER TABLE "initial_articles" ADD COLUMN unexpected TEXT')

    assert run_history(args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["timeline"][0]["verified_current"] is False


def test_failed_second_ddl_keeps_initial_prepared(tmp_path, monkeypatch):
    from aurora.database import execution

    class Another(Model):
        __table__ = "initial_more"
        id = column(int, primary_key=True)

    path = tmp_path / "failure.sqlite3"
    sqlite3.connect(path).close()
    original = execution.execute_create
    calls = []

    def interrupted(connection, model, approval, *, confirm=False):
        calls.append(model)

        if len(calls) == 2:
            raise RuntimeError("interrupted after first table")

        return original(connection, model, approval, confirm=confirm)

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        monkeypatch.setattr(execution, "execute_create", interrupted)

        with pytest.raises(RuntimeError, match="interrupted"):
            _execute_batch(connection, (Article, Another))

        report = inspect_initial(connection._raw, "default")
        assert report["status"] == "prepared"
        assert report["verified_current"] is False

        with pytest.raises(RuntimeError, match="Existing migration journal"):
            _execute_batch(connection, (Article, Another))
