"""AuroraSQL initialization: reviewed writes and read-only safety gates."""

import json
import sqlite3

import pytest

from aurora.cli import main


MODEL = "tests.aurorasql.test_schema:Entry"


def _project(tmp_path):
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {
        "default": {"url": "sqlite:///main.sqlite3"},
        "analytics": {"url": "sqlite:///analytics.sqlite3"},
    }}), encoding="utf-8")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {
        "default": [MODEL], "analytics": [MODEL],
    }}), encoding="utf-8")
    return config, manifest


def _init(config, manifest, *args):
    return main(["db", "init", "--config", str(config),
                 "--models-config", str(manifest), *args])


def test_missing_database_check_is_read_only(tmp_path, capsys):
    config, manifest = _project(tmp_path)
    assert _init(config, manifest, "--check") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "MISSING_DATABASE"
    assert report["read_only"] is True
    assert not (tmp_path / "main.sqlite3").exists()


def test_missing_database_apply_is_refused(tmp_path):
    config, manifest = _project(tmp_path)
    with pytest.raises(SystemExit):
        _init(config, manifest, "--approve", "wrong", "--confirm", "APPLY")
    assert not (tmp_path / "main.sqlite3").exists()


def test_existing_empty_database_requires_exact_approval(tmp_path, capsys):
    config, manifest = _project(tmp_path)
    sqlite3.connect(tmp_path / "main.sqlite3").close()
    assert _init(config, manifest) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "EMPTY"
    assert report["write_enabled"] is False
    with pytest.raises(SystemExit):
        _init(config, manifest, "--approve", "wrong", "--confirm", "APPLY")
    with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()


def test_approved_creation_is_idempotently_refused_on_replay(tmp_path, capsys):
    config, manifest = _project(tmp_path)
    sqlite3.connect(tmp_path / "main.sqlite3").close()
    assert _init(config, manifest) == 0
    digest = json.loads(capsys.readouterr().out)["approval_sha256"]
    assert _init(config, manifest, "--approve", digest, "--confirm", "APPLY") == 0
    with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "entry" in tables
        assert "aurorasql_schema_history" in tables
    with pytest.raises(SystemExit):
        _init(config, manifest, "--approve", digest, "--confirm", "APPLY")


def test_alias_isolation(tmp_path, capsys):
    config, manifest = _project(tmp_path)
    sqlite3.connect(tmp_path / "analytics.sqlite3").close()
    assert _init(config, manifest, "--database", "analytics") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database_alias"] == "analytics"
    assert not (tmp_path / "main.sqlite3").exists()


def test_unexpected_table_is_not_modified(tmp_path, capsys):
    config, manifest = _project(tmp_path)
    db = tmp_path / "main.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    before = db.read_bytes()
    assert _init(config, manifest, "--check") == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "REFUSED"
    assert "unrelated" in report["unexpected_tables"]
    assert db.read_bytes() == before


def test_missing_manifest_refuses_without_writes(tmp_path):
    config, manifest = _project(tmp_path)
    manifest.unlink()
    with pytest.raises(SystemExit):
        _init(config, manifest)
    assert not (tmp_path / "main.sqlite3").exists()


def test_unknown_alias_refuses_without_writes(tmp_path):
    config, manifest = _project(tmp_path)
    with pytest.raises(SystemExit):
        _init(config, manifest, "--database", "unknown")
    assert not (tmp_path / "main.sqlite3").exists()


def test_invalid_model_refuses_without_writes(tmp_path):
    config, manifest = _project(tmp_path)
    with pytest.raises(SystemExit):
        _init(config, manifest, "--model", "missing_module:Missing")
    assert not (tmp_path / "main.sqlite3").exists()
