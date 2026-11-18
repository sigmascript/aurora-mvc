"""Cross-engine one-table destructive contract, no compulsory backups."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_table_removal as removal
from aurora.database.inspection import ColumnInfo, TableInfo


def schemas():
    old = [
        {'name': 'items', 'columns': [{'name': 'id', 'primary_key': True}], 'create_sql_preview': 'old'},
        {'name': 'child', 'columns': [{'name': 'id', 'primary_key': True}], 'create_sql_preview': 'old'},
    ]
    new = deepcopy(old[:1]); new[0]['create_sql_preview'] = 'new'
    return old, new


def test_only_one_removed_table_without_unrelated_edit():
    old, new = schemas()
    assert removal.single_table_removal(old, new)
    assert not removal.single_table_removal(old, [])
    assert not removal.single_table_removal(old, [{'name': 'other', 'columns': []}])
    assert not removal.single_table_removal(old, [old[0], {'name': 'child', 'columns': []}])
    assert not removal.single_table_removal(['changed'], new)
    assert not removal.single_table_removal(old, new + [{'name': 'other', 'columns': []}])


class Fake:
    def __init__(self, dialect, rows):
        self.dialect, self.rows = dialect, rows
        self.statements = []

    def fetch_first(self, sql, params=None):
        if 'COUNT(*) AS total' in sql:
            return {'total': self.rows}
        if 'pg_try_advisory_xact_lock' in sql:
            return {'acquired': True}
        return None

    def execute_write(self, sql, params=None):
        self.statements.append((sql, params))

    @contextmanager
    def transaction(self):
        yield


@pytest.fixture(params=['mysql', 'postgresql'])
def dialect(request):
    return request.param


def setup(monkeypatch, dialect, rows=2, dependencies=False):
    connection = Fake(dialect, rows)
    old, new = schemas()
    models = [SimpleNamespace(__table_name__='items')]
    monkeypatch.setattr(removal, 'validate_init_models', lambda *a, **k: models)
    monkeypatch.setattr(removal, 'read_history', lambda *a: ([{'id': 1}],
                        {'models': old, 'physical_schema': [{'table': 'child'}]}))
    monkeypatch.setattr(removal, 'schema_snapshot', lambda *a, **k: {'tables': new})
    expected_index = 'PRIMARY' if dialect == 'mysql' else 'child_pkey'
    monkeypatch.setattr(removal, 'inspect_table', lambda *_: TableInfo('child', True,
            (ColumnInfo('id', 'bigint', False, True),), (), (expected_index,), True, True, (), ()))
    monkeypatch.setattr(removal, '_foreign_key_dependency', lambda *a: dependencies)
    monkeypatch.setattr(removal, '_verify_simple_constraints', lambda *a, **k: None)
    return connection, models


def test_review_recognizes_populated_and_empty_tables(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    operation, digest, count = removal.review_table_removal(connection, models, '.', 'data', 'identity')
    assert operation == {'kind': 'drop_table', 'table': 'child', 'rows': 2}
    assert count == 1 and len(digest) == 64
    connection.rows = 0
    assert removal.review_table_removal(connection, models, '.', 'data', 'identity')[0]['rows'] == 0


def test_review_refuses_fk_dependents(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect, dependencies=True)
    with pytest.raises(ValueError, match='Foreign-key'):
        removal.review_table_removal(connection, models, '.', 'data', 'identity')


def test_review_refuses_other_indexes(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    current = removal.inspect_table(connection, 'child')
    monkeypatch.setattr(removal, 'inspect_table', lambda *_: TableInfo('child', True,
        current.columns, (), current.indexes + ('extra',), True, True, (), ()))
    with pytest.raises(ValueError, match='verified ordinary table'):
        removal.review_table_removal(connection, models, '.', 'data', 'identity')


def execution(monkeypatch, dialect, rows=2):
    connection, models = setup(monkeypatch, dialect, rows)
    operation = {'kind': 'drop_table', 'table': 'child', 'rows': rows}
    monkeypatch.setattr(removal, 'review_table_removal', lambda *a: (operation, 'a' * 64, 2))
    monkeypatch.setattr(removal.sys.stdin, 'isatty', lambda: True)
    from aurora.database import cli_init
    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *a: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *a: 'identity')
    monkeypatch.setattr(removal, 'inspect_table', lambda *a: TableInfo('child', False, ()))
    monkeypatch.setattr(removal, 'verify_server_declared_schema', lambda *a, **k: None)
    monkeypatch.setattr(removal, '_write_snapshot', lambda *a: SimpleNamespace(name='success.json'))
    monkeypatch.setattr(removal, 'history_table', lambda *a: '__migrations__')
    monkeypatch.setattr(removal, 'read_history', lambda *a: (
        [None, None, next(params for sql, params in connection.statements if sql.startswith('INSERT INTO'))], {}))
    @contextmanager
    def lock(conn):
        yield
    monkeypatch.setattr(removal, 'migration_lock', lock)
    args = SimpleNamespace(title='Remove child', confirm=None, apply=False, approve=None,
                           backup=None, name=None)
    return connection, models, args


def test_populated_drop_without_backup_after_exact_consent(monkeypatch, dialect, capsys):
    connection, models, args = execution(monkeypatch, dialect)
    answers = iter(['y', 'DELETE TABLE data.child'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    assert removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None) == 0
    output = capsys.readouterr().out
    assert 'WARNING' in output and 'aurora db backup' in output
    assert sum('DROP TABLE' in sql for sql, _ in connection.statements) == 1
    record = next(params for sql, params in connection.statements if sql.startswith('INSERT INTO'))
    assert 'Confirmed permanent data loss' in record['warnings']


def test_cancellation_does_not_drop(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect)
    answers = iter(['y', 'wrong acknowledgement'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    assert removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None) == 0
    assert not any('DROP TABLE' in sql for sql, _ in connection.statements)


def test_empty_table_needs_only_ordinary_approval(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect, rows=0)
    monkeypatch.setattr('builtins.input', lambda *_: 'y')
    assert removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None) == 0
    assert sum('DROP TABLE' in sql for sql, _ in connection.statements) == 1


def test_noninteractive_refusal(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect)
    monkeypatch.setattr(removal.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(PermissionError, match='interactive'):
        removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None)


def test_mysql_history_failure_is_reported_as_uncertain(monkeypatch):
    connection, models, args = execution(monkeypatch, 'mysql')
    answers = iter(['y', 'DELETE TABLE data.child'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    monkeypatch.setattr(removal, '_write_snapshot', lambda *a: (_ for _ in ()).throw(OSError('snapshot failed')))
    with pytest.raises(RuntimeError, match='outcome uncertain'):
        removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None)
    assert any('DROP TABLE' in sql for sql, _ in connection.statements)


def test_postgres_history_failure_does_not_claim_success(monkeypatch):
    connection, models, args = execution(monkeypatch, 'postgresql')
    answers = iter(['y', 'DELETE TABLE data.child'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    monkeypatch.setattr(removal, '_write_snapshot', lambda *a: (_ for _ in ()).throw(OSError('snapshot failed')))
    with pytest.raises(OSError, match='snapshot failed'):
        removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None)
    assert any('DROP TABLE' in sql for sql, _ in connection.statements)


def test_changed_review_refuses_ddl(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect)
    attempts = [0]
    def changing(*_):
        attempts[0] += 1
        return {'kind': 'drop_table', 'table': 'child', 'rows': 2}, ('a' * 64 if attempts[0] == 1 else 'b' * 64), 2
    monkeypatch.setattr(removal, 'review_table_removal', changing)
    answers = iter(['y', 'DELETE TABLE data.child'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    with pytest.raises(RuntimeError, match='proposal changed'):
        removal.run_table_removal(args, connection, models, '.', 'data', 'identity', None)
    assert not any('DROP TABLE' in sql for sql, _ in connection.statements)
