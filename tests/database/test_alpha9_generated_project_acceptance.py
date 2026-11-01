"""End-to-end, read-only AuroraSQL acceptance for all generated project layouts."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from aurora.cli import create_project


_REPOSITORY = Path(__file__).resolve().parents[2]


def _run(cwd, *arguments):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPOSITORY) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "aurora", *arguments],
        cwd=cwd, env=environment, text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
def test_generated_project_alias_plan_from_external_directory(tmp_path, layout):
    project = create_project(
        "acceptance", directory=tmp_path,
        mvc=layout == "mvc", modular=layout == "modular",
    )
    assert (project / "aurora_databases.json").is_file()
    assert (project / "aurora_models.json").is_file()
    assert not (project / "data.sqlite3").exists()

    (project / "acceptance_model.py").write_text(
        "from aurora.database import Model, column\n"
        "class Entry(Model):\n"
        "    id = column(int, primary_key=True)\n",
        encoding="utf-8",
    )
    (project / "aurora_databases.json").write_text(json.dumps({
        "databases": {
            "default": {"url": "sqlite:///data.sqlite3"},
            "archive": {"url": "sqlite:///archive.sqlite3"},
        },
    }), encoding="utf-8")
    (project / "aurora_models.json").write_text(json.dumps({
        "databases": {"archive": ["acceptance_model:Entry"]},
    }), encoding="utf-8")

    with sqlite3.connect(project / "archive.sqlite3") as database:
        database.execute("CREATE TABLE entry (id INTEGER PRIMARY KEY)")
    before = (project / "archive.sqlite3").read_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config = str(project / "aurora_databases.json")

    plan = _run(elsewhere, "db", "plan", "--config", config,
                "--database", "archive", "--format", "json")
    assert plan.returncode == 0, plan.stderr
    report = json.loads(plan.stdout)
    assert report["backend"] == "aurorasql"
    assert report["database_alias"] == "archive"
    assert report["advisory_only"] is True
    assert (project / "archive.sqlite3").read_bytes() == before
    assert not (project / "data.sqlite3").exists()

    missing = _run(elsewhere, "db", "plan", "--config", config,
                   "--database", "default", "--format", "json")
    assert missing.returncode != 0
    assert not (project / "data.sqlite3").exists()
    assert (project / "archive.sqlite3").read_bytes() == before


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
def test_generated_project_init_preflight_never_creates_database(tmp_path, layout):
    project = create_project(
        "acceptance", directory=tmp_path,
        mvc=layout == "mvc", modular=layout == "modular",
    )
    (project / "entry.py").write_text(
        "from aurora.database import Model, column\n"
        "class Entry(Model):\n"
        "    id = column(int, primary_key=True)\n",
        encoding="utf-8",
    )
    (project / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["entry:Entry"]},
    }), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run(elsewhere, "db", "init", "--check", "--config",
                  str(project / "aurora_databases.json"))
    assert result.returncode == 0, result.stderr
    assert "MISSING_DATABASE" in result.stdout
    assert not (project / "data.sqlite3").exists()
