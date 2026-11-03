"""Batch 1 public CLI default and removed backend flag acceptance."""

import os
import subprocess
import sys
from pathlib import Path


def _run(*args, cwd=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", *args], cwd=cwd, env=env,
                          text=True, capture_output=True)


def test_public_model_defaults_to_aurorasql(tmp_path):
    project = _run("new", "site", "--mvc", cwd=tmp_path)
    assert project.returncode == 0, project.stderr
    site = tmp_path / "site"
    result = _run("model", "add", "Article", cwd=site)
    assert result.returncode == 0, result.stderr
    assert "from aurora.database import Model, column" in (site / "models" / "Article.py").read_text()
    assert (site / "aurora_models.json").exists()


def test_public_backend_flag_is_removed(tmp_path):
    result = _run("db", "plan", "--backend", "legacy", cwd=tmp_path)
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_no_manifest_does_not_fall_back_to_legacy(tmp_path):
    result = _run("db", "migrate", cwd=tmp_path)
    assert result.returncode != 0
    assert "refusing legacy fallback" in result.stderr
