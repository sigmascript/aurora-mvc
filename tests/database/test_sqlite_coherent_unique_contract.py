"""Single-column UNIQUE transitions use the ordinary verified SQLite table rebuild."""
import sqlite3

import pytest

from aurora.cli import main
from test_sqlite_coherent_table_transition_contract import project, index, assert_history


def change(model, original, replacement):
    text = model.read_text()
    assert original in text
    model.write_text(text.replace(original, replacement))


def seed(root, names):
    with sqlite3.connect(root / 'data.sqlite3') as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('INSERT INTO users(id) VALUES (1)')
        for ident, name in enumerate(names, 1):
            db.execute('INSERT INTO messages(id,user_id,visits,title) VALUES (?,?,?,?)',
                       (ident, 1, 10, name))


def unique_index(db):
    return [r for r in db.execute('PRAGMA index_list("messages")') if r[2] == 1]


def test_create_and_remove_unique_preserves_fk_ordinary_index_rows(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    index(tmp_path)
    seed(tmp_path, ['one', 'two'])
    change(model, 'column(str, length=50)', 'column(str, length=50, unique=True)')
    assert main(['db', 'migrate', '--title', 'Unique message title', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert unique_index(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO messages(id,user_id,visits,title) VALUES (3,1,10,?)', ('one',))
        assert db.execute('SELECT title FROM messages ORDER BY id').fetchall() == [('one',), ('two',)]
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        assert any(r[1] == 'messages_user_idx' for r in db.execute('PRAGMA index_list("messages")'))
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 3)
    change(model, 'column(str, length=50, unique=True)', 'column(str, length=50)')
    assert main(['db', 'migrate', '--title', 'Remove title uniqueness', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert not unique_index(db)
        assert any(r[1] == 'messages_user_idx' for r in db.execute('PRAGMA index_list("messages")'))
        assert db.execute('SELECT title FROM messages ORDER BY id').fetchall() == [('one',), ('two',)]
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 4)


def test_duplicate_values_refuse_unique_without_writes(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    seed(tmp_path, ['same', 'same'])
    change(model, 'column(str, length=50)', 'column(str, length=50, unique=True)')
    with pytest.raises(SystemExit) as ex:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert ex.value.code == 1
    output = capsys.readouterr().err
    assert 'messages.title' in output and 'duplicates' in output
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert not unique_index(db)
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone() == (2,)
    assert_history(tmp_path, 1)


def test_nullable_unique_allows_multiple_nulls(tmp_path, monkeypatch):
    model = project(tmp_path)
    change(model, 'column(str, length=50)', 'column(str, length=50, not_null=False)')
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    seed(tmp_path, [None, None, 'hello'])
    change(model, 'column(str, length=50, not_null=False)',
           'column(str, length=50, not_null=False, unique=True)')
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 2)


def test_unique_and_default_in_one_history_entry(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    seed(tmp_path, ['one', 'two'])
    change(model, 'column(str, length=50)', 'column(str, length=50, unique=True)')
    change(model, 'column(int, default=10)', 'column(int, default=11)')
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert main(['db', 'check']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert unique_index(db)
        assert db.execute('SELECT visits FROM messages ORDER BY id').fetchall() == [(10,), (10,)]
    assert_history(tmp_path, 2)


def test_unique_and_type_conversion_in_one_history_entry(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    seed(tmp_path, ['one', 'two'])
    change(model, 'column(str, length=50)', 'column(str, length=25, unique=True)')
    assert main(['db', 'migrate', '--title', 'Narrow unique title', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert unique_index(db)
        assert db.execute('SELECT title FROM messages ORDER BY id').fetchall() == [('one',), ('two',)]
        assert next(r for r in db.execute('PRAGMA table_info("messages")') if r[1] == 'title')[2] == 'VARCHAR(25)'
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 2)
