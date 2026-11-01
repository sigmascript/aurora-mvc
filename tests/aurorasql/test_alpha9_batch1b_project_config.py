"""Project-root configuration and model selection regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    (root / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///test.sqlite3"}}
    }), encoding="utf-8")
    (root / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["models:Item"]}
    }), encoding="utf-8")
    (root / "models.py").write_text(
        "from aurora.aurorasql import Model, column\n"
        "class Item(Model):\n    id = column(int, primary_key=True)\n",
        encoding="utf-8",
    )
    return root


def _run(tmp_path, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "aurora", "db", *args],
        cwd=tmp_path, env=env, text=True, capture_output=True,
    )


def test_init_check_uses_config_directory_for_manifest_and_imports(tmp_path):
    root = _project(tmp_path)
    result = _run(tmp_path, "init", "--check", "--config", str(root / "aurora_databases.json"))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "MISSING_DATABASE"
    assert not (root / "test.sqlite3").exists()


def test_init_preflight_does_not_create_database_or_manifest(tmp_path):
    root = _project(tmp_path)
    manifest = root / "aurora_models.json"
    before = manifest.read_bytes()
    result = _run(tmp_path, "init", "--check", "--config", str(root / "aurora_databases.json"))
    assert result.returncode == 0, result.stderr
    assert manifest.read_bytes() == before
    assert not (root / "test.sqlite3").exists()


def test_missing_manifest_does_not_fallback_to_cwd(tmp_path):
    root = _project(tmp_path)
    (root / "aurora_models.json").unlink()
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["models:Item"]}
    }), encoding="utf-8")
    result = _run(tmp_path, "init", "--check", "--config", str(root / "aurora_databases.json"))
    assert result.returncode != 0
    assert not (root / "test.sqlite3").exists()
