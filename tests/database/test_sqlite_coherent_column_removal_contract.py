"""Multiple removed columns share one guarded forward SQLite migration."""
import sqlite3
import sys

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history
from test_sqlite_coherent_addition_contract import project, seed


class Terminal:
    def isatty(self):
        return True


def arrange(root, monkeypatch, *, populated=True, indexed=True):
    model = project(root)
    monkeypatch.chdir(root)
    model.write_text(model.read_text().replace(
        '    title = column(str, length=50)\n',
        '    title = column(str, length=50)\n'
        '    comment = column(str, length=50, not_null=False)\n'))
    seed(root, indexed=indexed)
    if populated:
        with sqlite3.connect(root / 'data.sqlite3') as db:
            db.execute('UPDATE messages SET comment=? WHERE id=1', ('important',))
    model.write_text(model.read_text().replace(
        '    title = column(str, length=50)\n', '').replace(
        '    comment = column(str, length=50, not_null=False)\n', ''))


def state(root):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        return ([r[1] for r in db.execute('PRAGMA table_info(messages)')],
                db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0])


def test_two_empty_columns_one_migration_without_backup(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch, populated=False)
    # "title" is populated by seed(), so clear it as well.
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        db.execute('DELETE FROM messages')
    assert main(['db', 'check']) == 2
    assert main(['db', 'migrate', '--confirm', 'yes', '--title', 'Remove unused fields']) == 0
    assert state(tmp_path) == (['id', 'user_id', 'visits'], 3)
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert not (tmp_path / 'backups').exists()
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(r[1] == 'message_user_idx' for r in db.execute('PRAGMA index_list(messages)'))
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')


def test_two_populated_columns_one_backup_and_two_target_acks(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'stdin', Terminal())
    answers = iter(['y', 'DELETE default.messages.comment', 'DELETE default.messages.title'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert main(['db', 'migrate', '--title', 'Remove title and comment']) == 0
    assert state(tmp_path) == (['id', 'user_id', 'visits'], 3)
    backups = list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT title,comment FROM messages').fetchone() == ('hello', 'important')
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        warnings = db.execute('SELECT warnings FROM __migrations__ ORDER BY id DESC LIMIT 1').fetchone()[0]
        assert 'messages.title' in warnings and 'messages.comment' in warnings
        assert any(r[1] == 'message_user_idx' for r in db.execute('PRAGMA index_list(messages)'))
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
    assert main(['db', 'check']) == 0


def test_incorrect_second_ack_preserves_both_columns_and_history(tmp_path, monkeypatch):
    arrange(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'stdin', Terminal())
    answers = iter(['y', 'DELETE default.messages.comment', 'DELETE default.messages.other'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert main(['db', 'migrate']) == 0
    assert state(tmp_path) == (['id', 'user_id', 'visits', 'title', 'comment'], 2)
    assert len(list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))) == 1


def test_noninteractive_multi_drop_refuses_without_backup(tmp_path, monkeypatch, capsys):
    arrange(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code == 1
    assert 'verified backup' in capsys.readouterr().err
    assert state(tmp_path) == (['id', 'user_id', 'visits', 'title', 'comment'], 2)
    assert not (tmp_path / 'backups').exists()


def test_multi_drop_refuses_dependent_column_before_writes(tmp_path, monkeypatch, capsys):
    arrange(tmp_path, monkeypatch)
    model = tmp_path / 'models' / 'Messages.py'
    model.write_text(model.read_text().replace('    user_id = column(int, reference="Users.id")\n', ''))
    with pytest.raises(SystemExit) as exc:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code == 1
    assert 'index message_user_idx' in capsys.readouterr().err or 'foreign-key dependency' in capsys.readouterr().err
    assert state(tmp_path) == (['id', 'user_id', 'visits', 'title', 'comment'], 2)
