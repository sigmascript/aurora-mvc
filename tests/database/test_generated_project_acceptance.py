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


@pytest.mark.parametrize("layout", ["mvc", "modular"])
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
    result = _run(project, "db", "init", "--check")
    assert result.returncode == 0, result.stderr

    assert "MISSING_DATABASE" in result.stdout

    assert not (project / "data.sqlite3").exists()

@pytest.mark.parametrize("layout", ["mvc", "modular"])
def test_generated_project_forward_migration_preserves_existing_rows(tmp_path, layout):
    """Use the documented CLI on generated apps, not mocked migration handlers."""
    project = create_project(
        "acceptance", directory=tmp_path,
        mvc=layout == "mvc", modular=layout == "modular",
    )
    result = _run(project, "model", "add", "Entry")
    assert result.returncode == 0, result.stderr
    assert not (project / "data.sqlite3").exists()

    for command in (("db", "check"), ("db", "init", "--confirm", "yes"),
                    ("db", "check"), ("db", "history")):
        result = _run(project, *command)
        assert result.returncode == 0, result.stderr

    database = project / "data.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO entry (id) VALUES (7)")

    model = project / "models" / "Entry.py"
    model.write_text(model.read_text(encoding="utf-8") +
                     "    description = column(str, nullable=True)\n", encoding="utf-8")

    result = _run(project, "db", "migrate", "--confirm", "yes")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id, description FROM entry").fetchall() == [(7, None)]
        assert connection.execute("SELECT COUNT(*) FROM migrations").fetchone() == (2,)

    result = _run(project, "db", "migrate", "--confirm", "yes")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM migrations").fetchone() == (2,)


def test_minimal_project_remains_database_free(tmp_path):
    """The default template must not acquire unnecessary database machinery."""
    project = create_project("acceptance", directory=tmp_path)
    assert (project / "app.py").exists()
    assert not (project / "aurora_databases.json").exists()
    assert not (project / "aurora_models.json").exists()
    assert not (project / "models").exists()
    assert not (project / "migrations").exists()
