"""Ordinary SQLite indexes use one inspected forward migration, not an old journal."""
import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history


def _project(tmp_path):
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models' / '__init__.py').write_text('')
    (tmp_path / 'models' / 'Notes.py').write_text(
        'from aurora.database import Model, column\n'
        'class Notes(Model):\n'
        '    id = column(int, primary_key=True)\n'
        '    title = column(str, length=100)\n'
        '    amount = column(int)\n')
    (tmp_path / 'aurora_databases.json').write_text(json.dumps({
        'databases': {'default': {'engine': 'sqlite', 'database': 'data.sqlite3'}}}))
    (tmp_path / 'aurora_models.json').write_text(json.dumps({
        'databases': {'default': ['models.Notes:Notes']}}))


def _change(tmp_path, name, operation):
    file = tmp_path / name
    file.write_text(json.dumps({'database': 'default', **operation}))
    return file.name


def test_create_drop_index_full_history_and_noop(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    capsys.readouterr()
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        db.execute('INSERT INTO notes(id,title,amount) VALUES (1,?,2)', ('keep',))
    spec = _change(tmp_path, 'create.json', {'create_index': {
        'table': 'notes', 'name': 'notes_amount_idx', 'columns': ['amount']}})
    assert main(['db', 'migrate', '-c', spec, '--confirm', 'yes']) == 0
    assert 'Applied SQLite create_index' in capsys.readouterr().out
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall() == [('notes_amount_idx',)]
        assert db.execute('SELECT title FROM notes').fetchone() == ('keep',)
        assert db.execute('SELECT count(*) FROM __migrations__').fetchone()[0] == 2
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
    assert main(['db','check']) == 0
    assert 'UP_TO_DATE' in capsys.readouterr().out
    assert main(['db','migrate','--confirm','yes']) == 0
    assert 'no migration needed' in capsys.readouterr().out
    spec = _change(tmp_path, 'drop.json', {'drop_index': {
        'table': 'notes', 'name': 'notes_amount_idx'}})
    assert main(['db','migrate','-c',spec,'--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='notes_amount_idx'").fetchone()
        assert db.execute('SELECT count(*) FROM __migrations__').fetchone()[0] == 3
        assert db.execute('SELECT title FROM notes').fetchone() == ('keep',)
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
    assert main(['db', 'check']) == 0
    assert 'UP_TO_DATE' in capsys.readouterr().out


def test_index_refuses_wrong_table_missing_column_unique_and_unowned_drop(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    for operation in [
        {'create_index': {'table':'other','name':'idx','columns':['title']}},
        {'create_index': {'table':'notes','name':'idx','columns':['missing']}},
        {'create_index': {'table':'notes','name':'idx','columns':['title'],'unique':True}},
        {'drop_index': {'table':'notes','name':'missing_idx'}},
    ]:
        spec = _change(tmp_path, 'invalid.json', operation)
        with pytest.raises(SystemExit) as exc:
            main(['db','migrate','-c',spec,'--confirm','yes'])
        assert exc.value.code == 1
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM __migrations__').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE type='index'").fetchone()


def test_index_catalog_tampering_fails_closed(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    spec = _change(tmp_path, 'create.json', {'create_index': {
        'table':'notes','name':'notes_title_idx','columns':['title']}})
    assert main(['db','migrate','-c',spec,'--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        db.execute('DROP INDEX notes_title_idx')
    with pytest.raises(SystemExit) as exc:
        main(['db','migrate','-c',spec,'--confirm','yes'])
    assert exc.value.code == 1


def test_foreign_key_column_index_preserves_relationship(tmp_path, monkeypatch, capsys):
    folder = tmp_path / 'models'
    folder.mkdir()
    (folder / '__init__.py').write_text('')
    (folder / 'Users.py').write_text(
        'from aurora.database import Model, column\n'
        'class Users(Model):\n    id = column(int, primary_key=True)\n')
    (folder / 'Messages.py').write_text(
        'from aurora.database import Model, column\n'
        'class Messages(Model):\n'
        '    id = column(int, primary_key=True)\n'
        '    user_id = column(int, reference="Users.id")\n')
    (tmp_path / 'aurora_databases.json').write_text(json.dumps({
        'databases': {'default': {'engine': 'sqlite', 'database': 'data.sqlite3'}}}))
    (tmp_path / 'aurora_models.json').write_text(json.dumps({
        'databases': {'default': ['models.Users:Users','models.Messages:Messages']}}))
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    spec = _change(tmp_path, 'create.json', {'create_index': {
        'table': 'messages', 'name': 'messages_user_idx', 'columns': ['user_id']}})
    assert main(['db','migrate','-c',spec,'--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('PRAGMA foreign_key_list(messages)').fetchall()
        assert db.execute('PRAGMA index_list(messages)').fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO messages(id,user_id) VALUES (1,999)')
        assert db.execute('SELECT count(*) FROM __migrations__').fetchone()[0] == 2
    assert main(['db','check']) == 0
    assert 'UP_TO_DATE' in capsys.readouterr().out


def test_repeated_index_operations_explain_existing_or_missing_index(tmp_path, monkeypatch, capsys):
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    capsys.readouterr()
    create = _change(tmp_path, 'create.json', {'create_index': {
        'table': 'notes', 'name': 'notes_amount_idx', 'columns': ['amount']}})
    drop = _change(tmp_path, 'drop.json', {'drop_index': {
        'table': 'notes', 'name': 'notes_amount_idx'}})
    assert main(['db', 'migrate', '-c', create, '--confirm', 'yes']) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as failure:
        main(['db', 'migrate', '-c', create, '--confirm', 'yes'])
    assert failure.value.code == 1
    output = capsys.readouterr().err
    assert 'already uses this name' in output
    assert 'No database changes were made' in output
    assert main(['db', 'migrate', '-c', drop, '--confirm', 'yes']) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as failure:
        main(['db', 'migrate', '-c', drop, '--confirm', 'yes'])
    assert failure.value.code == 1
    output = capsys.readouterr().err
    assert 'does not exist on the selected table' in output
    assert 'No database changes were made' in output
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone()[0] == 3
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='notes_amount_idx'").fetchone()


def test_explicit_index_title_is_saved_in_sqlite_forward_history(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    spec = _change(tmp_path, 'create.json', {'create_index': {
        'table': 'notes', 'name': 'notes_title_idx', 'columns': ['title']}})
    assert main(['db', 'migrate', '-c', spec, '--title', 'Index notes titles',
                 '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT title FROM __migrations__ ORDER BY id DESC LIMIT 1').fetchone() == (
            'Index notes titles',)
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
