"""Fail-closed AuroraSQL CLI planning tests; never change database schema."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aurora.database.cli_plan import _load_config


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_model_free_named_server_plan_is_read_only_and_alias_scoped(monkeypatch, tmp_path,
                                                                   capsys, dialect):
    from contextlib import contextmanager
    from aurora.database import cli_plan

    class ServerConnection:
        def __init__(self):
            self.dialect = dialect
            self.queries = []

        def fetch_all(self, statement):
            self.queries.append(statement)
            return [{"name": "invoices"}]

    class Configuration:
        def resolve(self, alias):
            assert alias == "accounting"
            return f"{dialect}://127.0.0.1/accounting"

        @contextmanager
        def connect(self, alias):
            assert alias == "accounting"
            yield connection

    connection = ServerConnection()
    monkeypatch.setattr(cli_plan, "_load_config", lambda _: (Configuration(), tmp_path))
    monkeypatch.setattr(cli_plan, "_select_models", lambda *args: ([], []))
    args = SimpleNamespace(config="ignored", models_config="aurora_models.json",
                           database="accounting", format="json", check=False)

    assert cli_plan.run_plan(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database_alias"] == "accounting"
    assert report["status"] == "UNVERIFIED"
    assert report["table_count"] == 1
    assert report["advisory_only"] is True
    assert len(connection.queries) == 1


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


def test_public_check_is_available_and_plan_is_retired(tmp_path):
    help_result = _cli(tmp_path, "db", "check", "--help")
    assert help_result.returncode == 0
    assert "--database" in help_result.stdout
    assert "--model" not in help_result.stdout
    retired = _cli(tmp_path, "db", "plan")
    assert retired.returncode == 2
    assert "invalid choice" in retired.stderr


def test_public_check_missing_database_is_read_only(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///missing.sqlite3"}}}))
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["mymodel:Item"]}}))
    (tmp_path / "mymodel.py").write_text(
        "from aurora.database import Model, column\n"
        "class Item(Model):\n"
        "    id = column(int, primary_key=True)\n")
    result = _cli(tmp_path, "db", "check")
    assert result.returncode == 0, result.stderr
    assert "READY" in result.stdout
    assert not (tmp_path / "missing.sqlite3").exists()


def test_bad_config_does_not_leak_password(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "unknown://user:secret@host/db"}}}))
    result = _cli(tmp_path, "db", "check")
    assert result.returncode != 0
    assert "secret" not in result.stderr
