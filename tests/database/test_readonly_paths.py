"""Read-only database inspection with project-relative and absolute SQLite paths."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run(root, *args):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, '-m', 'aurora', *args], cwd=root,
                          env=env, text=True, capture_output=True)


def _setup(root, url):
    (root / 'aurora_databases.json').write_text(json.dumps({'databases': {'default': {'url': url}}}), encoding='utf-8')


def _journal(db):
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE aurorasql_schema_history (operation_id VARCHAR(36) PRIMARY KEY, table_name VARCHAR(128) NOT NULL, dialect VARCHAR(16) NOT NULL, fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)')


def test_absolute_sqlite_plan_without_models_is_read_only(tmp_path):
    db = tmp_path / 'absolute.sqlite3'
    _journal(db)
    before = db.read_bytes()
    _setup(tmp_path, 'sqlite:///' + db.as_posix())
    result = _run(tmp_path, 'db', 'plan', '--format', 'json')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['table_count'] == 1
    assert db.read_bytes() == before


def test_absolute_sqlite_history_is_read_only(tmp_path):
    db = tmp_path / 'absolute.sqlite3'
    _journal(db)
    before = db.read_bytes()
    _setup(tmp_path, 'sqlite:///' + db.as_posix())
    result = _run(tmp_path, 'db', 'history', '--format', 'json')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['entries'] == []
    assert db.read_bytes() == before


def test_missing_absolute_sqlite_file_never_created(tmp_path):
    db = tmp_path / 'absent.sqlite3'
    _setup(tmp_path, 'sqlite:///' + db.as_posix())
    for action in ('plan', 'history'):
        result = _run(tmp_path, 'db', action)
        assert result.returncode != 0
        assert not db.exists()


def test_relative_sqlite_history_unchanged(tmp_path):
    db = tmp_path / 'relative.sqlite3'
    _journal(db)
    before = db.read_bytes()
    _setup(tmp_path, 'sqlite:///relative.sqlite3')
    result = _run(tmp_path, 'db', 'history', '--format', 'json')
    assert result.returncode == 0, result.stderr
    assert db.read_bytes() == before
