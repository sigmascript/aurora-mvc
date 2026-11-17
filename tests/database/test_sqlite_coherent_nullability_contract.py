"""Data-aware nullability changes through the verified forward-history rebuild."""
import sqlite3

import pytest

from aurora.cli import main
from test_sqlite_coherent_table_transition_contract import project, index, assert_history


def set_field(path, old, new):
    data = path.read_text()
    assert old in data
    path.write_text(data.replace(old, new))


def seed(root, value):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('INSERT INTO users(id) VALUES (1)')
        db.execute('INSERT INTO messages(id,user_id,visits,title) VALUES (1,1,10,?)', (value,))


def test_nullable_to_required_with_data_fk_and_index(tmp_path, monkeypatch):
    model = project(tmp_path)
    set_field(model, 'column(str, length=50)', 'column(str, length=50, not_null=False)')
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    index(tmp_path)
    seed(tmp_path, 'hello')
    set_field(model, 'column(str, length=50, not_null=False)', 'column(str, length=50)')
    assert main(['db','migrate','--title','Require title','--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        title = next(r for r in db.execute('PRAGMA table_info("messages")') if r[1]=='title')
        assert title[3] == 1
        assert db.execute('SELECT title FROM messages').fetchone() == ('hello',)
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert any(r[1]=='messages_user_idx' for r in db.execute('PRAGMA index_list("messages")'))
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert_history(tmp_path, 3)
    assert main(['db','check']) == 0


def test_required_to_nullable_and_repeated_noop(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    seed(tmp_path, 'hello')
    set_field(model, 'column(str, length=50)', 'column(str, length=50, not_null=False)')
    assert main(['db','migrate','--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        title = next(r for r in db.execute('PRAGMA table_info("messages")') if r[1]=='title')
        assert title[3] == 0
        db.execute('UPDATE messages SET title=NULL WHERE id=1')
    assert main(['db','check']) == 0
    assert main(['db','migrate','--confirm','yes']) == 0
    assert_history(tmp_path, 2)


def test_required_rejected_when_null_data_present(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    set_field(model, 'column(str, length=50)', 'column(str, length=50, not_null=False)')
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    seed(tmp_path, None)
    set_field(model, 'column(str, length=50, not_null=False)', 'column(str, length=50)')
    with pytest.raises(SystemExit) as failure:
        main(['db','migrate','--confirm','yes'])
    assert failure.value.code == 1
    error = capsys.readouterr().err
    assert 'messages.title' in error and 'NULL' in error
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT title FROM messages').fetchone() == (None,)
    assert_history(tmp_path, 1)


def test_type_and_nullability_coherent_one_migration(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    seed(tmp_path, 'hello')
    set_field(model, 'column(str, length=50)', 'column(str, length=25, not_null=False)')
    assert main(['db','migrate','--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        title = next(r for r in db.execute('PRAGMA table_info("messages")') if r[1]=='title')
        assert title[2].upper()=='VARCHAR(25)' and title[3]==0
        assert db.execute('SELECT title FROM messages').fetchone()==('hello',)
    assert_history(tmp_path, 2)
    assert main(['db','check']) == 0


def test_nullability_and_default_changed_together(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    seed(tmp_path, 'hello')
    set_field(model, 'column(int, default=10)',
              'column(int, default=11, not_null=False)')
    assert main(['db','migrate','--title','Change visits','--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        visits = next(r for r in db.execute('PRAGMA table_info("messages")') if r[1]=='visits')
        assert visits[3] == 0 and visits[4] == '11'
        assert db.execute('SELECT visits FROM messages').fetchone() == (10,)
    assert main(['db','check']) == 0
    assert_history(tmp_path, 2)


def test_empty_nullable_column_can_become_required(tmp_path, monkeypatch):
    model = project(tmp_path)
    set_field(model, 'column(str, length=50)', 'column(str, length=50, not_null=False)')
    monkeypatch.chdir(tmp_path)
    assert main(['db','init','--confirm','yes']) == 0
    set_field(model, 'column(str, length=50, not_null=False)', 'column(str, length=50)')
    assert main(['db','migrate','--confirm','yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        title = next(r for r in db.execute('PRAGMA table_info("messages")') if r[1]=='title')
        assert title[3] == 1
    assert main(['db','check']) == 0
    assert_history(tmp_path, 2)
