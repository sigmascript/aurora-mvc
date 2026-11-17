"""One guarded SQLite transition for column removal and compatible modifications."""
import json
import sqlite3
import sys

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history
from test_sqlite_coherent_addition_contract import project, seed


class Terminal:
    def isatty(self):
        return True


def arrange(root, monkeypatch, *, populated=True, unsafe=False):
    model = project(root)
    monkeypatch.chdir(root)
    seed(root)
    if not populated:
        with sqlite3.connect(root / 'data.sqlite3') as db:
            db.execute('DELETE FROM messages')
    # Drop title; simultaneously change the visits database default and add a field.
    text = model.read_text().replace('    title = column(str, length=50)\n', '')
    text = text.replace('visits = column(int, default=10)',
                        'visits = column(int, default=20)\n'
                        '    score = column(int, default=7)')
    if unsafe:
        text = text.replace('    user_id = column(int, reference="Users.id")\n', '')
    model.write_text(text)


def details(root):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        columns = [r[1] for r in db.execute('PRAGMA table_info(messages)')]
        history = db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0]
        return columns, history


def assert_valid(root, expected):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(r[1] == 'message_user_idx' for r in db.execute('PRAGMA index_list(messages)'))
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0] == expected
        read_history(db, root, root / 'data.sqlite3')


def test_empty_column_mixed_change_one_migration(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch, populated=False)
    assert main(['db', 'check']) == 2
    assert main(['db', 'migrate', '--confirm', 'yes', '--title', 'Mixed change']) == 0
    assert details(tmp_path) == (['id', 'user_id', 'visits', 'score'], 3)
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert not (tmp_path / 'backups').exists()
    assert_valid(tmp_path, 3)
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert next(row for row in db.execute('PRAGMA table_info(messages)') if row[1] == 'visits')[4] == '20'


def test_populated_column_mixed_change_one_backup_one_history(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'stdin', Terminal())
    answers = iter(['y', 'DELETE default.messages.title'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert main(['db', 'migrate', '--title', 'Mixed destructive']) == 0
    assert details(tmp_path) == (['id', 'user_id', 'visits', 'score'], 3)
    backups = list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT title FROM messages').fetchone() == ('hello',)
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT visits,score FROM messages').fetchone() == (5, 7)
        warnings = db.execute('SELECT warnings FROM __migrations__ ORDER BY id DESC LIMIT 1').fetchone()[0]
        warning_entries = json.loads(warnings)
        assert any('messages.title' in entry and str(backups[0]) in entry
                   for entry in warning_entries)
    assert main(['db', 'check']) == 0
    assert_valid(tmp_path, 3)


def test_mixed_change_cancellation_preserves_everything(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'stdin', Terminal())
    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')
    assert main(['db', 'migrate']) == 0
    assert details(tmp_path) == (['id', 'user_id', 'visits', 'title'], 2)
    assert not (tmp_path / 'backups').exists()
    assert_valid(tmp_path, 2)


def test_mixed_change_wrong_ack_preserves_everything(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'stdin', Terminal())
    answers = iter(['y', 'DELETE default.messages.visits'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert main(['db', 'migrate']) == 0
    assert details(tmp_path) == (['id', 'user_id', 'visits', 'title'], 2)
    assert len(list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))) == 1
    assert_valid(tmp_path, 2)


def test_mixed_change_noninteractive_danger_refused(tmp_path, monkeypatch, capsys):
    arrange(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as error:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert error.value.code == 1
    assert 'verified backup' in capsys.readouterr().err
    assert details(tmp_path) == (['id', 'user_id', 'visits', 'title'], 2)
    assert not (tmp_path / 'backups').exists()


def test_mixed_change_fk_dependency_refused(tmp_path, monkeypatch, capsys):
    arrange(tmp_path, monkeypatch, unsafe=True)
    with pytest.raises(SystemExit) as error:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert error.value.code == 1
    assert 'message_user_idx' in capsys.readouterr().err or 'foreign-key dependency' in capsys.readouterr().err
    assert details(tmp_path) == (['id', 'user_id', 'visits', 'title'], 2)
    assert_valid(tmp_path, 2)


def test_mixed_type_conversion_and_populated_drop_preserves_values(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    model.write_text(model.read_text().replace(
        '    visits = column(int, default=10)\n',
        '    visits = column(str, length=10)\n').replace(
        '    title = column(str, length=50)\n', ''))
    monkeypatch.setattr(sys, 'stdin', Terminal())
    answers = iter(['y', 'DELETE default.messages.title'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert main(['db', 'migrate', '--title', 'Convert and remove']) == 0
    assert details(tmp_path) == (['id', 'user_id', 'visits'], 3)
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT visits,typeof(visits) FROM messages').fetchone() == ('5', 'text')
    assert main(['db', 'check']) == 0
    assert_valid(tmp_path, 3)
