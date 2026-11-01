"""Fail-closed AuroraSQL CLI planning tests; never change database schema."""
import json
import os
from pathlib import Path
import subprocess
import sys

from aurora.database.cli_plan import _load_config


def _cli(tmp_path, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", *args], cwd=tmp_path,
                          env=env, capture_output=True, text=True)


def test_config_loads_without_sqlalchemy(tmp_path):
    path = tmp_path / "aurora_databases.json"
    path.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///missing.sqlite3"}}}))
    config, base = _load_config(path)
    assert config.resolve() == "sqlite:///missing.sqlite3"
    assert base == tmp_path


def test_opt_in_plan_refuses_missing_sqlite_without_creation(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///missing.sqlite3"}}}))
    (tmp_path / "mymodel.py").write_text("from aurora.database import Model, column\n"
                                       "class Item(Model):\n"
                                       "    id = column(int, primary_key=True)\n")
    result = _cli(tmp_path, "db", "plan", "--model", "mymodel:Item")
    assert result.returncode != 0
    assert not (tmp_path / "missing.sqlite3").exists()
    assert "refused" in result.stderr


def test_bad_config_does_not_leak_password(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "unknown://user:secret@host/db"}}}))
    result = _cli(tmp_path, "db", "plan", "--model", "mymodel:Item")
    assert result.returncode != 0
    assert "secret" not in result.stderr


def test_sqlite_read_only_plan_does_not_change_database(tmp_path):
    import sqlite3
    database = tmp_path / "existing.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE item (id INTEGER PRIMARY KEY)')
    before = database.read_bytes()
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///existing.sqlite3"}}}))
    (tmp_path / "mymodel.py").write_text("from aurora.database import Model, column\n"
                                       "class Item(Model):\n"
                                       "    id = column(int, primary_key=True)\n")
    result = _cli(tmp_path, "db", "plan", "--model", "mymodel:Item", "--format", "json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["backend"] == "aurorasql"
    assert report["advisory_only"] is True
    assert database.read_bytes() == before


def test_manifest_and_model_import_use_selected_config_directory(tmp_path):
    import sqlite3

    project = tmp_path / 'selected_project'
    project.mkdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    with sqlite3.connect(project / 'data.sqlite3') as connection:
        connection.execute('CREATE TABLE item (id INTEGER PRIMARY KEY)')
    (project / 'aurora_databases.json').write_text(json.dumps({
        'databases': {'default': {'url': 'sqlite:///data.sqlite3'}}
    }), encoding='utf-8')
    (project / 'aurora_models.json').write_text(json.dumps({
        'databases': {'default': ['project_models:Item']}
    }), encoding='utf-8')
    (project / 'project_models.py').write_text(
        'from aurora.database import Model, column\n'
        'class Item(Model):\n'
        '    id = column(int, primary_key=True)\n', encoding='utf-8')
    result = _cli(elsewhere, 'db', 'plan', '--config', str(project / 'aurora_databases.json'),
                  '--format', 'json')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['models'][0]['reference'] == 'project_models:Item'
