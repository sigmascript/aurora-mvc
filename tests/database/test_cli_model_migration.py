"""Six-command model migrations preview, approve, back up and extend history."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from aurora.cli import main
from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.core import Connection
from aurora.database.migration_timeline import inspect_timeline


def _project(tmp_path):
    path = tmp_path / "cms.sqlite3"
    initial = type("Customer", (Model,), {
        "__module__": "sample_cms_models", "__table__": "customers",
        "id": column(int, primary_key=True),
    })

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (initial,))

    (tmp_path / "sample_cms_models.py").write_text(
        'from aurora.database import Model, column\n'
        'class Customer(Model):\n'
        '    __table__ = "customers"\n'
        '    id = column(int, primary_key=True)\n'
        '    email = column(str, nullable=True)\n'
        'class Article(Model):\n'
        '    __table__ = "articles"\n'
        '    id = column(int, primary_key=True)\n', encoding="utf-8")
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["sample_cms_models:Customer", "sample_cms_models:Article"]}
    }), encoding="utf-8")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///cms.sqlite3"}}}), encoding="utf-8")
    return path, config


def _migrate(config, *args):
    return main(["db", "migrate", "--config", str(config), *args])


def test_public_model_migration_preview_and_guarded_apply(tmp_path, capsys, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("AURORA_ENV", raising=False)
    path, config = _project(tmp_path)
    original = path.read_bytes()

    assert _migrate(config, "--dry-run", "--format", "json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "READY_FOR_REVIEW"
    assert {change["kind"] for change in report["operations"]} == {
        "added_column", "added_table",
    }
    assert path.read_bytes() == original

    backup = tmp_path / "before.sqlite3"
    assert _migrate(config, "--apply", "--confirm", "APPLY",
                    "--approve", report["review_fingerprint"],
                    "--backup", str(backup), "--title", "Expand CMS") == 0
    capsys.readouterr()

    with sqlite3.connect(path) as connection:
        timeline = inspect_timeline(connection, "default")
        assert len(timeline) == 2
        assert timeline[-1]["title"] == "Expand CMS"
        assert timeline[-1]["physical_history_consistent"] is True
        assert "email" in [row[1] for row in connection.execute('PRAGMA table_info("customers")')]

    with sqlite3.connect(backup) as connection:
        assert "email" not in [row[1] for row in connection.execute('PRAGMA table_info("customers")')]


def test_public_model_migration_rejects_stale_approval(tmp_path, capsys, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("AURORA_ENV", raising=False)
    path, config = _project(tmp_path)
    assert _migrate(config, "--dry-run", "--format", "json") == 0
    report = json.loads(capsys.readouterr().out)
    backup = tmp_path / "before.sqlite3"

    with pytest.raises(SystemExit) as error:
        _migrate(config, "--apply", "--confirm", "APPLY", "--approve", "0" * 64,
                 "--backup", str(backup))

    assert error.value.code == 1
    assert not backup.exists()
    with sqlite3.connect(path) as connection:
        assert len(inspect_timeline(connection, "default")) == 1


def test_model_review_binds_database_path_and_refuses_preview_backup(tmp_path, capsys,
                                                                     monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    path, config = _project(tmp_path)
    assert _migrate(config, "--dry-run", "--format", "json") == 0
    fingerprint = json.loads(capsys.readouterr().out)["review_fingerprint"]
    other = tmp_path / "elsewhere.sqlite3"
    other.write_bytes(path.read_bytes())
    alternate = tmp_path / "other_databases.json"
    alternate.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///elsewhere.sqlite3"}}}), encoding="utf-8")
    assert _migrate(alternate, "--dry-run", "--format", "json") == 0
    assert json.loads(capsys.readouterr().out)["review_fingerprint"] != fingerprint

    with pytest.raises(SystemExit) as error:
        _migrate(config, "--dry-run", "--backup", str(tmp_path / "unused.sqlite3"))

    assert error.value.code == 1
    assert not (tmp_path / "unused.sqlite3").exists()


def test_simple_interactive_migration_confirms_and_creates_backup(tmp_path, capsys,
                                                                  monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("AURORA_ENV", raising=False)
    path, config = _project(tmp_path)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(("y", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert _migrate(config) == 0
    output = capsys.readouterr().out
    assert "added_column: customers.email" in output
    assert "added_table: articles" in output

    with sqlite3.connect(path) as connection:
        timeline = inspect_timeline(connection, "default")
        assert timeline[-1]["title"] == "Untitled"
        assert timeline[-1]["physical_history_consistent"] is True

    backups = list(tmp_path.glob("cms.sqlite3.before-*.sqlite3"))
    assert len(backups) == 1

    with sqlite3.connect(backups[0]) as connection:
        assert "email" not in [row[1] for row in connection.execute('PRAGMA table_info("customers")')]


def test_simple_interactive_migration_decline_is_read_only(tmp_path, capsys,
                                                           monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    path, config = _project(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert _migrate(config) == 0
    assert "cancelled" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("cms.sqlite3.before-*.sqlite3"))


def test_simple_migration_refuses_noninteractive_without_approval(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    path, config = _project(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))

    with pytest.raises(SystemExit) as error:
        _migrate(config)

    assert error.value.code == 1
    assert path.read_bytes() == before


def test_stable_model_table_rename_preserves_original_journal_and_rows(
        tmp_path, capsys, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("AURORA_ENV", raising=False)
    path = tmp_path / "renaming.sqlite3"
    initial = type("Customer", (Model,), {
        "__module__": "rename_cms_models", "__table__": "customers",
        "id": column(int, primary_key=True),
    })

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (initial,))

    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO customers(id) VALUES (7)")

    (tmp_path / "rename_cms_models.py").write_text(
        'from aurora.database import Model, column\n'
        'class Customer(Model):\n'
        '    __table__ = "clients"\n'
        '    id = column(int, primary_key=True)\n', encoding="utf-8")
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["rename_cms_models:Customer"]}}), encoding="utf-8")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///renaming.sqlite3"}}}), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(("y", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert _migrate(config) == 0
    capsys.readouterr()

    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT id FROM clients').fetchone() == (7,)
        assert connection.execute(
            "SELECT table_name FROM aurorasql_schema_history"
        ).fetchone() == ("customers",)
        timeline = inspect_timeline(connection, "default")
        assert timeline[-1]["model_tables"]["rename_cms_models:Customer"] == "clients"
        assert timeline[-1]["physical_history_consistent"] is True

    assert len(list(tmp_path.glob("renaming.sqlite3.before-*.sqlite3"))) == 1

    from aurora.database.cli_history import run_history
    history_args = SimpleNamespace(config=str(config), database="default",
                                   models_config="aurora_models.json",
                                   verify_models=True, format="json")
    assert run_history(history_args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["timeline"][-1]["verified_current"] is True

    from aurora.database.review_artifacts import create_review
    from aurora.database.snapshots import schema_snapshot
    from aurora.database.sqlite_additive_migration import execute_sqlite_additive_migration
    renamed = type("Customer", (Model,), {
        "__module__": "rename_cms_models", "__table__": "clients",
        "id": column(int, primary_key=True),
    })
    expanded = type("Customer", (Model,), {
        "__module__": "rename_cms_models", "__table__": "clients",
        "id": column(int, primary_key=True),
        "status": column(str, nullable=True, default="active"),
    })
    next_review = create_review(
        schema_snapshot(renamed, dialect="sqlite"),
        schema_snapshot(expanded, dialect="sqlite"), migration_id="AddClientStatus",
    )

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        execute_sqlite_additive_migration(
            raw, next_review, (expanded,), reviewed_sha256=next_review["review_sha256"],
            confirm=True,
        )
        assert raw.execute('SELECT status FROM clients WHERE id=7').fetchone() == ("active",)
        assert len(inspect_timeline(raw, "default")) == 3
