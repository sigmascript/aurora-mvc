"""One add + one drop server migration; optional backups and forward evidence."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_mixed_columns as mixed
from aurora.database.inspection import ColumnInfo, TableInfo


def schemas():
    old = [
        {'name': 'items', 'columns': [
            {'name': 'id', 'primary_key': True}, {'name': 'old_note', 'primary_key': False}],
         'create_sql_preview': 'old DDL'},
        {'name': 'other', 'columns': [{'name': 'id', 'primary_key': True}],
         'create_sql_preview': 'other'},
    ]
    new = deepcopy(old)
    new[0]['columns'][1] = {'name': 'new_note', 'primary_key': False}
    new[0]['create_sql_preview'] = 'new DDL'
    return old, new


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_single_mixed_delta_requires_exact_other_tables(dialect):
    old, new = schemas()
    assert mixed.single_mixed_column_delta(old, new)
    assert mixed._mixed_delta(old, new)[0] == 'items'
    broken = deepcopy(new)
    broken[1]['columns'].append({'name': 'extra'})
    assert not mixed.single_mixed_column_delta(old, broken)
    broken = deepcopy(new)
    broken[0]['columns'][0]['primary_key'] = False
    assert not mixed.single_mixed_column_delta(old, broken)
    broken = deepcopy(new)
    broken[0]['name'] = 'different'
    assert not mixed.single_mixed_column_delta(old, broken)
    broken = deepcopy(new)
    broken[0]['columns'].append({'name': 'extra'})
    assert not mixed.single_mixed_column_delta(old, broken)
    assert not mixed.single_mixed_column_delta(['invalid'], new)
    assert not mixed.single_mixed_column_delta(old, [])
    assert not mixed.single_mixed_column_delta(old, old)


class Fake:
    def __init__(self, dialect, populated=True):
        self.dialect, self.populated = dialect, populated
        self.statements = []
        self.rows = 1
        self.fail_statement = False

    def fetch_first(self, sql, params=None):
        if 'pg_try_advisory_xact_lock' in sql:
            return {'acquired': True}
        if 'COUNT(*)' in sql:
            return {'total': self.rows}
        if 'IS NOT NULL' in sql:
            return {'present': 1} if self.populated else None
        return None

    def execute_write(self, sql, params=None):
        if self.fail_statement and sql.startswith('ALTER TABLE'):
            raise RuntimeError('simulated DDL failure')
        self.statements.append(sql)

    @contextmanager
    def transaction(self):
        yield


def setup(monkeypatch, dialect, populated=True):
    old, new = schemas()
    connection = Fake(dialect, populated)
    model = SimpleNamespace(__table_name__='items', __columns__={'new_note': object()})
    models = [model]
    monkeypatch.setattr(mixed, 'validate_init_models', lambda *a, **k: models)
    monkeypatch.setattr(mixed, 'read_history', lambda *a: ([{'id': 1}],
                      {'models': old, 'physical_schema': ['known']}))
    monkeypatch.setattr(mixed, 'schema_snapshot', lambda *a, **k: {'tables': new})
    key = 'PRIMARY' if dialect == 'mysql' else 'items_pkey'
    monkeypatch.setattr(mixed, 'inspect_table', lambda *_: TableInfo('items', True,
        (ColumnInfo('id', 'bigint', False, True),
         ColumnInfo('old_note', 'varchar(20)', True, False)),
        (), (key,), True, True, (), ()))
    monkeypatch.setattr(mixed, '_foreign_key_dependency', lambda *a: False)
    monkeypatch.setattr(mixed, '_verify_simple_constraints', lambda *a, **k: None)
    monkeypatch.setattr(mixed, '_add_column_clause', lambda *a: 'ADD COLUMN new_note VARCHAR(20)')
    return connection, models


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
@pytest.mark.parametrize('populated', [False, True])
def test_review_single_alter_optional_backup(monkeypatch, dialect, populated):
    connection, models = setup(monkeypatch, dialect, populated)
    operation, statement, digest, count = mixed.review_mixed_columns(
        connection, models, '.', 'data', 'identity')
    assert operation['has_meaningful_data'] is populated
    assert operation['drop_column'] == 'old_note'
    assert operation['add_column'] == 'new_note'
    assert statement.startswith('ALTER TABLE') and 'DROP COLUMN' in statement
    assert 'ADD COLUMN' in statement and count == 1 and len(digest) == 64
    assert len([x for x in statement.split('ALTER TABLE') if x.strip()]) == 1


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_dependent_columns_refused(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    monkeypatch.setattr(mixed, '_foreign_key_dependency', lambda *_: True)
    with pytest.raises(ValueError, match='foreign-key dependencies'):
        mixed.review_mixed_columns(connection, models, '.', 'data', 'identity')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_cancelled_ack_does_not_execute(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    monkeypatch.setattr(mixed.sys.stdin, 'isatty', lambda: True)
    responses = iter(['y', 'wrong acknowledgment'])
    monkeypatch.setattr('builtins.input', lambda *_: next(responses))
    args = SimpleNamespace(title='mixed', apply=False, approve=None, backup=None,
                           name=None, confirm=None)
    assert mixed.run_mixed_columns(args, connection, models, '.', 'data', 'identity', None) == 0
    assert not connection.statements


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_noninteractive_refused(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    monkeypatch.setattr(mixed.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(PermissionError, match='interactive'):
        mixed.run_mixed_columns(SimpleNamespace(confirm=None), connection, models,
                                '.', 'data', 'identity', None)


def test_dispatch_single_mixed_is_before_single_column_removal():
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / 'aurora/database/cli_server_model_migration.py'
    contents = source.read_text()
    assert contents.index('if single_mixed_column_delta(') < contents.index('if single_column_removal(')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_approved_mixed_operation_writes_one_history_without_backup(monkeypatch, dialect):
    from aurora.database import cli_init
    connection, models = setup(monkeypatch, dialect)
    monkeypatch.setattr(mixed.sys.stdin, 'isatty', lambda: True)
    response = iter(['y', 'DELETE data.items.old_note'])
    monkeypatch.setattr('builtins.input', lambda *_: next(response))
    monkeypatch.setattr(mixed, 'history_table', lambda *_: '__migrations__')
    monkeypatch.setattr(mixed, 'verify_server_declared_schema', lambda *a, **k: None)
    monkeypatch.setattr(mixed, '_write_snapshot', lambda *a: SimpleNamespace(name='snapshot.json'))
    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *a: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *a: 'identity')
    monkeypatch.setattr(mixed, 'read_history', lambda *a: (
        ([{'id': 1}, {'id': 2, 'title': 'Mixed', 'snapshot': 'snapshot.json',
                      'date': getattr(connection, 'inserted_date', None),
                      'warnings': getattr(connection, 'inserted_warning', None)}], {})
        if hasattr(connection, 'inserted_date') else
        ([{'id': 1}], {'models': schemas()[0], 'physical_schema': ['known']})))
    original_write = connection.execute_write

    def write(sql, params=None):
        original_write(sql, params)
        if sql.startswith('INSERT INTO'):
            connection.inserted_date = params['date']
            connection.inserted_warning = params['warnings']
    connection.execute_write = write
    @contextmanager
    def lock(_):
        yield
    monkeypatch.setattr(mixed, 'migration_lock', lock)
    args = SimpleNamespace(title='Mixed', apply=False, approve=None, backup=None,
                           name=None, confirm=None)
    assert mixed.run_mixed_columns(args, connection, models, '.', 'data',
                                   'identity', None) == 0
    assert len([s for s in connection.statements if s.startswith('ALTER TABLE')]) == 1
    assert len([s for s in connection.statements if s.startswith('INSERT INTO')]) == 1
    assert 'backup was optional' in connection.inserted_warning


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_changed_review_refused_before_ddl(monkeypatch, dialect):
    from aurora.database import cli_init
    connection, models = setup(monkeypatch, dialect)
    monkeypatch.setattr(mixed.sys.stdin, 'isatty', lambda: True)
    responses = iter(['y', 'DELETE data.items.old_note'])
    monkeypatch.setattr('builtins.input', lambda *_: next(responses))
    original = mixed.review_mixed_columns
    calls = {'count': 0}

    def drift(*args):
        calls['count'] += 1
        result = original(*args)
        if calls['count'] > 1:
            return result[:2] + ('0' * 64, result[3])
        return result
    monkeypatch.setattr(mixed, 'review_mixed_columns', drift)
    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *a: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *a: 'identity')
    @contextmanager
    def lock(_):
        yield
    monkeypatch.setattr(mixed, 'migration_lock', lock)
    args = SimpleNamespace(title='Mixed', apply=False, approve=None, backup=None,
                           name=None, confirm=None)
    with pytest.raises(RuntimeError, match='changed after approval'):
        mixed.run_mixed_columns(args, connection, models, '.', 'data', 'identity', None)
    assert not any(x.startswith('ALTER TABLE') for x in connection.statements)


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_execution_error_has_backend_specific_behavior(monkeypatch, dialect):
    from aurora.database import cli_init
    connection, models = setup(monkeypatch, dialect)
    connection.fail_statement = True
    monkeypatch.setattr(mixed.sys.stdin, 'isatty', lambda: True)
    answers = iter(['y', 'DELETE data.items.old_note'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *a: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *a: 'identity')
    @contextmanager
    def lock(_):
        yield
    monkeypatch.setattr(mixed, 'migration_lock', lock)
    args = SimpleNamespace(title='Mixed', apply=False, approve=None, backup=None,
                           name=None, confirm=None)
    message = ('outcome uncertain' if dialect == 'mysql' else 'simulated DDL failure')
    with pytest.raises(RuntimeError, match=message):
        mixed.run_mixed_columns(args, connection, models, '.', 'data', 'identity', None)
