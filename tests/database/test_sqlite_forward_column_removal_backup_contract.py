"""Guard populated column removal with real pre-migration recovery evidence."""
import json
import sqlite3
import sys

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history


class Terminal:
    def isatty(self):
        return True


def setup_project(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    models = tmp_path / 'models'
    models.mkdir()
    (models / '__init__.py').write_text('')
    source = models / 'Records.py'
    original = ('from aurora.database import Model, column\n'
                'class Records(Model):\n'
                '    id = column(int, primary_key=True)\n'
                '    note = column(str, length=50, not_null=False)\n')
    source.write_text(original)
    (tmp_path / 'aurora_models.json').write_text(json.dumps({
        'databases': {'default': ['models.Records:Records']}}))
    (tmp_path / 'aurora_databases.json').write_text(json.dumps({
        'databases': {'default': {'engine': 'sqlite', 'database': 'data.sqlite3'}}}))
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        db.execute('INSERT INTO records (id,note) VALUES (1,?)', (value,))
    source.write_text(original.replace('    note = column(str, length=50, not_null=False)\n', ''))


def state(root):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        cols = [row[1] for row in db.execute('PRAGMA table_info(records)')]
        history = db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0]
    return cols, history


def test_empty_column_drops_without_backup(tmp_path, monkeypatch):
    setup_project(tmp_path, monkeypatch, None)
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert state(tmp_path) == (['id'], 2)
    assert not (tmp_path / 'backups').exists()
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0


def test_populated_column_refuses_noninteractive_confirmation(tmp_path, monkeypatch, capsys):
    setup_project(tmp_path, monkeypatch, 'important')
    with pytest.raises(SystemExit) as exc:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code == 1
    assert 'verified backup' in capsys.readouterr().err
    assert state(tmp_path) == (['id', 'note'], 1)
    assert not (tmp_path / 'backups').exists()


def test_populated_column_backup_and_target_ack(tmp_path, monkeypatch):
    setup_project(tmp_path, monkeypatch, 'important')
    monkeypatch.setattr(sys, 'stdin', Terminal())
    replies = iter(['y', 'DELETE default.records.note'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(replies))
    assert main(['db', 'migrate', '--title', 'Remove note']) == 0
    assert state(tmp_path) == (['id'], 2)
    copies = list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))
    assert len(copies) == 1
    with sqlite3.connect(copies[0]) as db:
        assert db.execute('SELECT note FROM records').fetchone() == ('important',)
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert 'verified backup:' in db.execute('SELECT warnings FROM __migrations__ ORDER BY id DESC LIMIT 1').fetchone()[0]
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
    assert main(['db', 'check']) == 0


def test_incorrect_ack_retains_database_and_backup(tmp_path, monkeypatch):
    setup_project(tmp_path, monkeypatch, 'important')
    monkeypatch.setattr(sys, 'stdin', Terminal())
    replies = iter(['y', 'DELETE other.records.note'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(replies))
    assert main(['db', 'migrate']) == 0
    assert state(tmp_path) == (['id', 'note'], 1)
    assert len(list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))) == 1


def test_cancel_before_backup(tmp_path, monkeypatch):
    setup_project(tmp_path, monkeypatch, 'important')
    monkeypatch.setattr(sys, 'stdin', Terminal())
    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')
    assert main(['db', 'migrate']) == 0
    assert state(tmp_path) == (['id', 'note'], 1)
    assert not (tmp_path / 'backups').exists()
