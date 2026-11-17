"""Explicit SQLite renames preserve forward history and existing relationships."""
import json
import sqlite3

from aurora.cli import main
from test_sqlite_coherent_table_transition_contract import project, index, insert, assert_history


def _changes(root, operation):
    change = root / 'rename.json'
    change.write_text(json.dumps({'database': 'default', **operation}))
    return change


def test_rename_fk_column_with_ordinary_index_preserves_rows(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    index(tmp_path)
    insert(tmp_path, 12)
    change = _changes(tmp_path, {'rename_columns': {'messages': {'user_id': 'owner_id'}}})
    assert main(['db', 'migrate', '-c', str(change), '--title', 'Rename message owner', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT owner_id FROM messages').fetchone() == (1,)
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchall()[0][3] == 'owner_id'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert any(row[1] == 'messages_user_idx' for row in db.execute('PRAGMA index_list("messages")'))
        assert db.execute('PRAGMA index_info("messages_user_idx")').fetchone()[2] == 'owner_id'
        assert db.execute('SELECT title FROM "__migrations__" ORDER BY id DESC LIMIT 1').fetchone() == ('Rename message owner',)
    assert_history(tmp_path, 3)
    assert main(['db', 'check']) == 0
    assert main(['db', 'migrate', '--confirm', 'yes']) == 0


def test_rename_referenced_parent_table_updates_fk_without_dropping_data(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    insert(tmp_path, 1)
    change = _changes(tmp_path, {'rename_table': {'users': 'accounts'}})
    assert main(['db', 'migrate', '-c', str(change), '--title', 'Rename users table', '--confirm', 'yes']) == 0
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT id FROM accounts').fetchone() == (1,)
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchone()[2] == 'accounts'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert_history(tmp_path, 2)
    assert main(['db', 'check']) == 0


def test_rename_refused_when_model_source_is_ambiguous(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    insert(tmp_path, 1)
    model.write_text(model.read_text().replace('user_id = column(int,', 'user_id = column_alias(int,').replace('from aurora.database import Model, column', 'from aurora.database import Model, column, column as column_alias'))
    change = _changes(tmp_path, {'rename_columns': {'messages': {'user_id': 'owner_id'}}})
    import pytest
    with pytest.raises(SystemExit) as error:
        main(['db', 'migrate', '-c', str(change), '--confirm', 'yes'])
    assert error.value.code == 1
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT user_id FROM messages').fetchone() == (1,)
        assert not any(row[1] == 'owner_id' for row in db.execute('PRAGMA table_info("messages")'))
    assert_history(tmp_path, 1)


def test_json_column_rename_updates_python_model_without_manual_edit(tmp_path, monkeypatch):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    insert(tmp_path, 3)
    before = model.read_text()
    change = _changes(tmp_path, {'rename_columns': {'messages': {'user_id': 'owner_id'}}})
    assert main(['db', 'migrate', '-c', str(change), '--confirm', 'yes']) == 0
    assert model.read_text() == before.replace('user_id = column', 'owner_id = column')
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 2)


def test_json_table_rename_updates_physical_mapping_and_keeps_model_identity(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    insert(tmp_path, 3)
    original = (tmp_path / 'models' / 'Users.py').read_text()
    manifest = (tmp_path / 'aurora_models.json').read_text()
    change = _changes(tmp_path, {'rename_table': {'users': 'accounts'}})
    assert main(['db', 'migrate', '-c', str(change), '--confirm', 'yes']) == 0
    users = (tmp_path / 'models' / 'Users.py').read_text()
    assert "__table__ = 'accounts'" in users
    assert original.replace('class Users(Model):\n', "class Users(Model):\n    __table__ = 'accounts'\n") == users
    assert (tmp_path / 'aurora_models.json').read_text() == manifest
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 2)


def test_declining_rename_keeps_python_model_and_db_untouched(tmp_path, monkeypatch):
    from io import StringIO
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    before = model.read_bytes()
    change = _changes(tmp_path, {'rename_columns': {'messages': {'user_id': 'owner_id'}}})
    monkeypatch.setattr('sys.stdin', StringIO('n\n'))
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    assert main(['db', 'migrate', '-c', str(change)]) == 0
    assert model.read_bytes() == before
    assert_history(tmp_path, 1)


def test_rename_rollback_restores_source_when_history_record_fails(tmp_path, monkeypatch):
    from aurora.database import sqlite_forward_history
    import pytest
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    original = model.read_bytes()
    change = _changes(tmp_path, {'rename_columns': {'messages': {'user_id': 'owner_id'}}})
    monkeypatch.setattr(sqlite_forward_history, '_new_record',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('simulated history failure')))
    with pytest.raises(SystemExit) as failed:
        main(['db', 'migrate', '-c', str(change), '--confirm', 'yes'])
    assert failed.value.code == 1
    assert model.read_bytes() == original
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert 'user_id' in [r[1] for r in db.execute('PRAGMA table_info(messages)')]
    assert_history(tmp_path, 1)


def test_model_only_rename_like_edit_is_not_inferred(tmp_path, monkeypatch, capsys):
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    model.write_text(model.read_text().replace('user_id = column', 'owner_id = column'))
    assert main(['db', 'check']) == 2
    result = capsys.readouterr().out
    assert '[missing_column] owner_id' in result
    assert '[extra_column] user_id' in result


def test_source_edited_after_review_is_not_overwritten(tmp_path, monkeypatch):
    import builtins
    import pytest
    model = project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    change = _changes(tmp_path, {'rename_columns': {'messages': {'user_id': 'owner_id'}}})
    original = model.read_text()

    def edit_during_confirmation(prompt):
        assert 'Apply' in prompt
        model.write_text(original + '\n# developer concurrent edit\n')
        return 'y'

    monkeypatch.setattr(builtins, 'input', edit_during_confirmation)
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    with pytest.raises(SystemExit) as failed:
        main(['db', 'migrate', '-c', str(change)])
    assert failed.value.code == 1
    assert model.read_text().endswith('# developer concurrent edit\n')
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert 'user_id' in [r[1] for r in db.execute('PRAGMA table_info(messages)')]
    assert_history(tmp_path, 1)


def test_table_rename_preserves_model_class_docstring(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    users = tmp_path / 'models' / 'Users.py'
    users.write_text(users.read_text().replace('class Users(Model):\n',
                                                'class Users(Model):\n    """Project user model."""\n'))
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    change = _changes(tmp_path, {'rename_table': {'users': 'accounts'}})
    assert main(['db', 'migrate', '-c', str(change), '--confirm', 'yes']) == 0
    assert main(['db', 'check']) == 0
    import importlib
    import sys
    # The loader reimports fresh project source; docstring remains in place.
    from aurora.database.cli_plan import _select_models
    from types import SimpleNamespace
    refs, models = _select_models(SimpleNamespace(models_config='aurora_models.json', database='default'), tmp_path)
    assert next(model for model in models if model.__name__ == 'Users').__doc__ == 'Project user model.'


def test_rename_referenced_primary_key_updates_dependent_model_literal(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    insert(tmp_path, 5)
    change = _changes(tmp_path, {'rename_columns': {'users': {'id': 'account_id'}}})
    assert main(['db', 'migrate', '-c', str(change), '--confirm', 'yes']) == 0
    users = (tmp_path / 'models' / 'Users.py').read_text()
    messages = (tmp_path / 'models' / 'Messages.py').read_text()
    assert 'account_id = column(int, primary_key=True)' in users
    assert "reference='Users.account_id'" in messages
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('SELECT account_id FROM users').fetchone() == (1,)
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchone()[4] == 'account_id'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 2)


def test_rename_table_updates_physical_foreign_key_reference_literals(tmp_path, monkeypatch):
    model = project(tmp_path)
    model.write_text(model.read_text().replace("reference='Users.id'", "reference='users.id'"))
    monkeypatch.chdir(tmp_path)
    assert main(['db', 'init', '--confirm', 'yes']) == 0
    insert(tmp_path, 2)
    change = _changes(tmp_path, {'rename_table': {'users': 'accounts'}})
    assert main(['db', 'migrate', '-c', str(change), '--confirm', 'yes']) == 0
    assert "reference='accounts.id'" in model.read_text()
    with sqlite3.connect(tmp_path / 'data.sqlite3') as db:
        assert db.execute('PRAGMA foreign_key_list("messages")').fetchone()[2] == 'accounts'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert main(['db', 'check']) == 0
    assert_history(tmp_path, 2)
