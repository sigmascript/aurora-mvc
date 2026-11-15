"""AuroraSQL generated models become discoverable without scanning arbitrary code."""
import json
import os
import subprocess
import sys
from pathlib import Path


def _run(root, *args):
    env = dict(os.environ)
    env.pop("AURORA_ENV", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", *args], cwd=root,
                          env=env, capture_output=True, text=True)


def test_generated_aurorasql_model_registered_without_import_or_database(tmp_path):
    assert _run(tmp_path, "new", "site", "--mvc").returncode == 0

    root = tmp_path / "site"
    result = _run(root, "model", "add", "Article")
    assert result.returncode == 0, result.stderr

    manifest = json.loads((root / "aurora_models.json").read_text())
    assert manifest == {"databases": {"default": ["models.Article:Article"]}}

    assert not (root / "data.sqlite3").exists()


def test_default_model_registers(tmp_path):
    assert _run(tmp_path, "new", "site", "--mvc").returncode == 0

    root = tmp_path / "site"
    result = _run(root, "model", "add", "Article")
    assert result.returncode == 0, result.stderr

    assert json.loads((root / "aurora_models.json").read_text()) == {"databases": {"default": ["models.Article:Article"]}}


def test_modular_model_registration(tmp_path):
    assert _run(tmp_path, "new", "site", "--modular").returncode == 0

    root = tmp_path / "site"
    result = _run(root, "model", "add", "Post", "--app", "blog")
    assert result.returncode == 0, result.stderr

    assert json.loads((root / "aurora_models.json").read_text()) == {
        "databases": {"default": ["apps.blog.models.Post:Post"]}}
