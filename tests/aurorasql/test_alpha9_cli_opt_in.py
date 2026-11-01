"""CLI opt-in does not change default database behavior."""
import os
import subprocess
import sys
from pathlib import Path


def _run(*args, cwd):
    env = dict(os.environ)
    env.pop("AURORA_ENV", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", *args],
                          cwd=cwd, env=env, capture_output=True, text=True)


def test_shared_model_cli_opt_in_and_automatic_followup(tmp_path):
    created = _run("new", "site", "--mvc", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    site = tmp_path / "site"
    modern = _run("model", "add", "Article", cwd=site)
    assert modern.returncode == 0, modern.stderr
    assert "from aurora.aurorasql import Model, column" in (site / "models" / "Article.py").read_text()
    followup = _run("model", "add", "NextArticle", cwd=site)
    assert followup.returncode == 0, followup.stderr
    assert "from aurora.aurorasql import Model, column" in (site / "models" / "NextArticle.py").read_text()
    legacy = _run("model", "add", "LegacyArticle", "--backend", "legacy", cwd=site)
    assert legacy.returncode != 0
    assert not (site / "models" / "LegacyArticle.py").exists()


def test_modular_model_cli_opt_in(tmp_path):
    created = _run("new", "site", "--modular", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    site = tmp_path / "site"
    modern = _run("model", "add", "Post", "--app", "blog", cwd=site)
    assert modern.returncode == 0, modern.stderr
    assert "from aurora.aurorasql import Model, column" in (site / "apps" / "blog" / "models" / "Post.py").read_text()


def test_bad_backend_rejected_without_creating_model(tmp_path):
    created = _run("new", "site", "--mvc", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    site = tmp_path / "site"
    invalid = _run("model", "add", "BadModel", "--backend", "invalid", cwd=site)
    assert invalid.returncode != 0
    assert not (site / "models" / "BadModel.py").exists()
