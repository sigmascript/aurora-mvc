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
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)


def test_aurorasql_migrate_write_refused_without_legacy_fallback():
    result = _cli("migrate", "--apply", "--confirm", "APPLY")
    assert result.returncode != 0

    assert "unrecognized arguments: --apply" in result.stderr


def test_aurorasql_migrate_without_models_refuses():
    result = _cli("migrate")
    assert result.returncode != 0

    assert "Model-driven migration refused" in result.stderr


def test_aurorasql_backup_refuses_legacy_fallback():
    result = _cli("backup")
    assert result.returncode != 0

    assert "--to" in result.stderr


def test_aurorasql_restore_refuses_legacy_fallback():
    result = _cli("restore", "--from", "missing.sqlite3")
    assert result.returncode != 0

    assert "restore" in result.stderr.lower()


def test_aurorasql_migrate_dry_run_fails_closed_without_config():
    result = _cli("migrate", "--dry-run")
    assert result.returncode != 0

    assert "unrecognized arguments: --dry-run" in result.stderr


def test_migrate_check_requires_dry_run_before_configuration():
    result = _cli("migrate", "--check")
    assert result.returncode != 0

    assert "unrecognized arguments: --check" in result.stderr


def test_migrate_json_requires_dry_run_before_configuration():
    result = _cli("migrate", "--format", "json")
    assert result.returncode != 0

    assert "unrecognized arguments: --format json" in result.stderr


def test_migrate_explain_requires_dry_run_before_configuration():
    result = _cli("migrate", "--explain")
    assert result.returncode != 0

    assert "unrecognized arguments: --explain" in result.stderr


def test_migrate_write_flags_never_fall_through_to_read_only_preview():
    result = _cli("migrate", "--dry-run", "--apply")
    assert result.returncode != 0

    assert "unrecognized arguments: --dry-run --apply" in result.stderr


def test_public_cli_uses_only_the_retained_database_namespace():
    """The public CLI may import AuroraSQL, but not retired backends."""
    import ast

    source = (ROOT / "aurora" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    assert any(name.startswith("aurora.database") for name in imported)

    assert all(not name.startswith("aurora.aurorasql") for name in imported)

    assert all(not name.startswith("sqlalchemy") for name in imported)

    assert "cli_unified_mysql_migrate" not in source

    assert "cli_recovery" not in source
