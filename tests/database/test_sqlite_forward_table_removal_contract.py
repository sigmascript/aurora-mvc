"""Registered-model removal stays source-only; reviewed table drops are independently safe."""
import json
import sqlite3
import sys

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history
from test_sqlite_coherent_addition_contract import project, seed
from test_sqlite_forward_new_table_contract import add_model


def unregister(root, name):
    path = root / 'aurora_models.json'
    content = json.loads(path.read_text())
    content['databases']['default'].remove(f'models.{name}:{name}')
    path.write_text(json.dumps(content))
    (root / 'models' / f'{name}.py').unlink()


def registered_dummy(root, *, rows=0):
    project(root)
    seed(root)
    add_model(root, 'Dummy', '    id = column(int, primary_key=True)\n')
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    if rows:
        with sqlite3.connect(root / 'data.sqlite3') as db:
            db.executemany('INSERT INTO dummy(id) VALUES (?)', [(i,) for i in range(1, rows + 1)])
    unregister(root, 'Dummy')


def state(root):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        return (db.execute("SELECT name FROM sqlite_master WHERE name='dummy'").fetchone(),
                db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0])


def test_empty_table_removed_with_unrelated_fk_and_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registered_dummy(tmp_path)
    assert state(tmp_path) == (('dummy',), 3)
    assert main(['db', 'migrate', '--confirm', 'yes', '--title', 'Remove empty Dummy']) == 0
    assert state(tmp_path) == (None, 4)
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT visits FROM messages').fetchone() == (5,)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(r[1] == 'message_user_idx' for r in db.execute('PRAGMA index_list(messages)'))
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
    assert not (tmp_path / 'backups').exists()


def test_populated_table_requires_interactive_typed_ack_even_with_confirm_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registered_dummy(tmp_path, rows=2)
    with pytest.raises(SystemExit) as error:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert error.value.code == 1
    assert state(tmp_path) == (('dummy',), 3)
    assert not (tmp_path / 'backups').exists()


class FakeTTY:
    def isatty(self):
        return True


def test_populated_table_backup_and_typed_ack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registered_dummy(tmp_path, rows=2)
    monkeypatch.setattr(sys, 'stdin', FakeTTY())
    replies = iter(['y', 'DELETE default.dummy'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(replies))
    assert main(['db', 'migrate', '--title', 'Remove populated Dummy']) == 0
    assert state(tmp_path) == (None, 4)
    backup_files = list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))
    assert len(backup_files) == 1
    with sqlite3.connect(backup_files[0]) as old:
        assert old.execute('SELECT COUNT(*) FROM dummy').fetchone() == (2,)
        assert old.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT warnings FROM __migrations__ ORDER BY id DESC LIMIT 1').fetchone()[0]
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')


def test_wrong_typed_ack_keeps_table_history_and_verified_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registered_dummy(tmp_path, rows=1)
    monkeypatch.setattr(sys, 'stdin', FakeTTY())
    replies = iter(['y', 'dummy'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(replies))
    assert main(['db', 'migrate']) == 0
    assert state(tmp_path) == (('dummy',), 3)
    assert len(list((tmp_path / 'backups' / 'data').glob('*.sqlite3'))) == 1


def test_referenced_table_refuses_drop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project(tmp_path)
    seed(tmp_path)
    # Keep parent users as physical object but unregister its Python model;
    # messages still physically depends on users.
    unregister(tmp_path, 'Users')
    with pytest.raises(SystemExit) as error:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert error.value.code == 1
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone()
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (2,)


def test_empty_table_with_its_own_ordinary_index_is_removed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project(tmp_path)
    seed(tmp_path)
    add_model(tmp_path, 'Dummy', '    id = column(int, primary_key=True)\n'
              '    label = column(str, length=20, not_null=False)\n')
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    index_change = tmp_path / 'dummy_index.json'
    index_change.write_text(json.dumps({
        'database': 'default', 'create_index': {
            'table': 'dummy', 'name': 'dummy_label_idx', 'columns': ['label']}}))
    assert main(['db', 'migrate', '-c', str(index_change), '--confirm', 'yes']) == 0
    unregister(tmp_path, 'Dummy')
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert main(['db', 'check']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='dummy_label_idx'").fetchone()
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (5,)
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
