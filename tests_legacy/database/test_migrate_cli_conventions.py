"""Convention-first public migration options preserve guarded legacy execution."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _cli(*arguments):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "aurora", "db", "migrate", *arguments],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15,
    )


def test_migration_help_uses_project_conventions():
    result = _cli("--help")
    assert result.returncode == 0
    assert "--config" not in result.stdout
    assert "--models-config" not in result.stdout
    assert "--database" in result.stdout
    assert "--change" in result.stdout and "-c CHANGES" in result.stdout
    assert "--title" in result.stdout


@pytest.mark.parametrize("option", ["--config", "--models-config"])
def test_removed_overrides_are_not_accepted(option):
    result = _cli(option, "untrusted.json")
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_unavailable_database_does_not_fall_back_to_another_engine():
    result = _cli()
    assert result.returncode != 0
    assert "Model-driven migration refused" in result.stderr


def test_obsolete_plural_changes_spelling_is_rejected():
    result = _cli("--changes=old.json")
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


@pytest.mark.parametrize("option", [
    "--approve", "--review-file", "--name", "--output-dir", "--backup",
    "--schema", "--table", "--column", "--apply", "--approve-review",
    "--approve-sql", "--dry-run", "--model", "--format",
])
def test_retired_migration_execution_options_rejected_at_public_cli(option):
    result = _cli(option)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_public_migration_help_contains_only_agreed_arguments():
    result = _cli("--help")
    assert result.returncode == 0
    for option in ("--database", "--title", "--confirm", "--change"):
        assert option in result.stdout
    for option in ("--approve", "--backup", "--apply", "--schema", "--review-file"):
        assert option not in result.stdout
