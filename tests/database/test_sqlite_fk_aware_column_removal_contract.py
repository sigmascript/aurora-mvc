"""Column removal is restricted by actual dependencies, not unrelated FKs/indexes."""
import sqlite3
import sys

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history
from test_sqlite_coherent_addition_contract import project, seed


class Terminal:
    def isatty(self):
        return True


def state(root):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        return ([row[1] for row in db.execute('PRAGMA table_info(messages)')],
                db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0])


def test_empty_column_with_unrelated_fk_and_index(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    model.write_text(model.read_text().replace(
        '    title = column(str, length=50)\n',
        '    title = column(str, length=50)\n'
        '    note = column(str, length=20, not_null=False)\n'))
    seed(tmp_path)
    model.write_text(model.read_text().replace(
        '    note = column(str, length=20, not_null=False)\n', ''))
    assert main(['db', 'migrate', '--confirm', 'yes', '--title', 'Drop empty note']) == 0
    assert state(tmp_path) == (['id', 'user_id', 'visits', 'title'], 3)
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT visits,user_id FROM messages').fetchone() == (5, 1)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(row[1] == 'message_user_idx' for row in db.execute('PRAGMA index_list(messages)'))
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')


def test_populated_column_with_fk_and_index_no_forced_backup(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    model.write_text(model.read_text().replace('    title = column(str, length=50)\n', ''))
    monkeypatch.setattr(sys, 'stdin', Terminal())
    replies = iter(['y', 'DELETE data.messages.title'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(replies))
    assert main(['db', 'migrate', '--title', 'Drop populated title']) == 0
    assert state(tmp_path) == (['id', 'user_id', 'visits'], 3)
    assert not list((tmp_path / '__backups__' / 'data').glob('*.sqlite3'))
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(row[1] == 'message_user_idx' for row in db.execute('PRAGMA index_list(messages)'))
        assert 'backup recommended' in db.execute(
            'SELECT warnings FROM __migrations__ ORDER BY id DESC LIMIT 1').fetchone()[0]
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')


def test_dependent_fk_column_refused_without_changes(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path, indexed=False)
    model.write_text(model.read_text().replace('    user_id = column(int, reference="Users.id")\n', ''))
    with pytest.raises(SystemExit) as exc:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code == 1
    assert 'foreign-key dependency' in capsys.readouterr().err
    assert state(tmp_path) == (['id', 'user_id', 'visits', 'title'], 1)


def test_indexed_target_column_refused_without_changes(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Build an ordinary indexed field with no foreign key.
    model.write_text(model.read_text().replace('reference="Users.id"', 'not_null=True'))
    seed(tmp_path)
    model.write_text(model.read_text().replace('    user_id = column(int, not_null=True)\n', ''))
    with pytest.raises(SystemExit) as exc:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code == 1
    assert 'index message_user_idx' in capsys.readouterr().err
    assert state(tmp_path) == (['id', 'user_id', 'visits', 'title'], 2)
