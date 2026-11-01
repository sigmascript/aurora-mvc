"""Fail-closed routing while AuroraSQL migration and recovery are unfinished."""
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", "db", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


def test_aurorasql_migrate_write_refused_without_legacy_fallback():
    result = _cli("migrate", "--apply", "--confirm", "APPLY")
    assert result.returncode != 0
    assert "refusing legacy fallback" in result.stderr


def test_aurorasql_migrate_requires_dry_run():
    result = _cli("migrate")
    assert result.returncode != 0
    assert "not yet integrated" in result.stderr


def test_aurorasql_backup_refuses_legacy_fallback():
    result = _cli("backup")
    assert result.returncode != 0
    assert "refusing legacy fallback" in result.stderr


def test_aurorasql_restore_refuses_legacy_fallback():
    result = _cli("restore", "--from", "missing.sqlite3")
    assert result.returncode != 0
    assert "refusing legacy fallback" in result.stderr


def test_aurorasql_migrate_dry_run_fails_closed_without_config():
    result = _cli("migrate", "--dry-run",
                  "--config", "nonexistent-alpha9-config.json")
    assert result.returncode != 0
    assert "preview refused" in result.stderr


def test_migrate_check_requires_dry_run_before_configuration():
    result = _cli("migrate", "--check", "--config", "missing-config.json")
    assert result.returncode == 1
    assert "--check requires --dry-run" in result.stderr


def test_migrate_json_requires_dry_run_before_configuration():
    result = _cli("migrate", "--format", "json", "--config", "missing-config.json")
    assert result.returncode == 1
    assert "--format json requires --dry-run" in result.stderr


def test_migrate_explain_requires_dry_run_before_configuration():
    result = _cli("migrate", "--explain", "--config", "missing-config.json")
    assert result.returncode == 1
    assert "--explain requires --dry-run" in result.stderr


def test_migrate_write_flags_never_fall_through_to_read_only_preview():
    result = _cli("migrate", "--dry-run", "--apply")
    assert result.returncode == 1
    assert "refusing legacy fallback" in result.stderr


def test_public_cli_has_no_legacy_database_imports():
    """Legacy database dispatch must not return as an alternate public path."""
    import ast

    source = (ROOT / "aurora" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("aurora.database"), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("aurora.database"), alias.name
    assert "aurora.database." not in source
