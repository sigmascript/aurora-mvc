"""Server constraint removal requires approval, never backup artifacts."""

from pathlib import Path
from types import SimpleNamespace
import builtins
import sys

import pytest

from aurora.database import server_forward_unique as unique
from aurora.database import server_forward_fk as postgres_fk
from aurora.database import server_forward_fk_mysql as mysql_fk


@pytest.mark.parametrize('module,runner,review,executor,kind,phrase,dialect', [
    (unique, 'run_unique_change', 'review_unique_change', 'execute_unique_change',
     'drop_unique', 'DROP UNIQUE data_mysql.child.parent_id', 'mysql'),
    (unique, 'run_unique_change', 'review_unique_change', 'execute_unique_change',
     'drop_unique', 'DROP UNIQUE data_postgres.child.parent_id', 'postgresql'),
    (postgres_fk, 'run_fk', 'review_fk', 'execute_fk',
     'drop_foreign_key', 'DROP FK data_postgres.child.parent_id', 'postgresql'),
    (mysql_fk, 'run_mysql_fk', 'review_mysql_fk', 'execute_mysql_fk',
     'drop_foreign_key', 'DROP FK data_mysql.child.parent_id', 'mysql'),
])
def test_constraint_drop_is_reviewed_without_any_backup_catalog(
        monkeypatch, capsys, module, runner, review, executor, kind, phrase, dialect):
    alias = 'data_mysql' if dialect == 'mysql' else 'data_postgres'
    operation = {'kind': kind, 'table': 'child', 'column': 'parent_id'}
    calls = []
    monkeypatch.setattr(module, review, lambda *args: (operation, 'digest', 1))
    def apply(*args):
        calls.append(args)
        return (Path('success.json'), 2) if kind == 'drop_foreign_key' else Path('success.json')
    monkeypatch.setattr(module, executor, apply)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    values = iter(('y', phrase))
    monkeypatch.setattr(builtins, 'input', lambda prompt='': next(values))
    connection = SimpleNamespace(dialect=dialect)
    args = SimpleNamespace(confirm=None, apply=None, approve=None, backup=None,
                           name=None, title='test')
    status = getattr(module, runner)(args, connection, [], '.', alias, 'identity',
                                      {}, 'no-available-backups')
    assert status == 0 and len(calls) == 1
    output = capsys.readouterr().out
    assert 'backup is optional' in output
    assert 'will not automatically create' in output


@pytest.mark.parametrize('module,runner,review,executor,kind,dialect', [
    (unique, 'run_unique_change', 'review_unique_change', 'execute_unique_change', 'drop_unique', 'mysql'),
    (postgres_fk, 'run_fk', 'review_fk', 'execute_fk', 'drop_foreign_key', 'postgresql'),
    (mysql_fk, 'run_mysql_fk', 'review_mysql_fk', 'execute_mysql_fk', 'drop_foreign_key', 'mysql'),
])
def test_constraint_drop_wrong_acknowledgment_cannot_execute(
        monkeypatch, module, runner, review, executor, kind, dialect):
    monkeypatch.setattr(module, review, lambda *args: (
        {'kind': kind, 'table': 'child', 'column': 'parent_id'}, 'digest', 1))
    monkeypatch.setattr(module, executor, lambda *args: pytest.fail('DDL executed on refusal'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers = iter(('y', 'WRONG'))
    monkeypatch.setattr(builtins, 'input', lambda prompt='': next(answers))
    args = SimpleNamespace(confirm=None, apply=None, approve=None, backup=None,
                           name=None, title='test')
    alias = 'data_mysql' if dialect == 'mysql' else 'data_postgres'
    assert getattr(module, runner)(args, SimpleNamespace(dialect=dialect), [], '.',
                                   alias, 'identity', {}, 'no-available-backups') == 0
