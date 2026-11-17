"""New model tables are ordinary reviewed forward migrations, including FK projects."""
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.sqlite_forward_history import read_history
from test_sqlite_coherent_addition_contract import project, seed


def add_model(root, name, body):
    (root / 'models' / f'{name}.py').write_text(
        'from aurora.database import Model, column\n'
        f'class {name}(Model):\n{body}')
    registry = root / 'aurora_models.json'
    import json
    data = json.loads(registry.read_text())
    data['databases']['default'].append(f'models.{name}:{name}')
    registry.write_text(json.dumps(data))


def test_add_new_model_to_existing_fk_index_project(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    add_model(tmp_path, 'Dummy', '    id = column(int, primary_key=True)\n')
    assert main(['db', 'check']) == 2
    assert main(['db', 'migrate', '--confirm', 'yes', '--title', 'Added table dummy']) == 0
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT visits FROM messages').fetchone() == (5,)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dummy'").fetchone()
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (3,)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(row[1] == 'message_user_idx' for row in db.execute('PRAGMA index_list(messages)'))
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')


def test_new_referencing_table_and_new_table_with_existing_column_addition(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    add_model(tmp_path, 'Reaction',
              '    id = column(int, primary_key=True)\n'
              '    message_id = column(int, reference="Messages.id")\n')
    model.write_text(model.read_text() + '    score = column(int, default=7)\n')
    assert main(['db', 'migrate', '--confirm', 'yes', '--title', 'Add reactions and score']) == 0
    assert main(['db', 'check']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT score FROM messages').fetchone() == (7,)
        assert db.execute('PRAGMA foreign_key_list(reaction)').fetchall()
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (3,)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')


def test_missing_fk_parent_is_refused_and_history_preserved(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    add_model(tmp_path, 'Bad',
              '    id = column(int, primary_key=True)\n'
              '    user_id = column(int, reference="Unknown.id")\n')
    with pytest.raises(SystemExit) as exc:
        main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code == 1
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='bad'").fetchone() is None
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (2,)


def test_cancel_new_table_migration_changes_nothing(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    add_model(tmp_path, 'Dummy', '    id = column(int, primary_key=True)\n')
    class FakeTTY:
        def isatty(self):
            return True
    import sys
    monkeypatch.setattr(sys, 'stdin', FakeTTY())
    monkeypatch.setattr('builtins.input', lambda *_: 'n')
    assert main(['db', 'migrate']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='dummy'").fetchone() is None
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (2,)


def test_multiple_new_fk_tables_created_in_dependency_order(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seed(tmp_path)
    # Deliberately register child before parent to exercise dependency ordering.
    add_model(tmp_path, 'Reply',
              '    id = column(int, primary_key=True)\n'
              '    category_id = column(int, reference="Category.id")\n')
    add_model(tmp_path, 'Category',
              '    id = column(int, primary_key=True)\n'
              '    name = column(str, length=50, unique=True)\n')
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0
    assert main(['db', 'check']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        db.execute('INSERT INTO category(id, name) VALUES (1, ?)', ('General',))
        db.execute('INSERT INTO reply(id, category_id) VALUES (1, 1)')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT COUNT(*) FROM __migrations__').fetchone() == (3,)
        read_history(db, tmp_path, tmp_path / 'data.sqlite3')
