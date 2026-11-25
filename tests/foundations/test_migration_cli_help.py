"""CLI help promises remain contract tests, not version checks."""

import subprocess
import sys


def test_migration_help_describes_supported_model_changes():
    result = subprocess.run(
        [sys.executable, "-m", "aurora", "db", "migrate", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Update your database to match supported model changes" in result.stdout
    assert "JSON changes file" in result.stdout
    assert "asks for confirmation" in result.stdout
    assert "Unsupported or unsafe changes are refused" in result.stdout
