"""PostgreSQL single-column removal stays isolated and fail-closed."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_removal as remove
from aurora.database.inspection import ColumnInfo, TableInfo


def _schemas(*, change=None):
    old = [{'name': 'postgres_item', 'create_sql_preview': 'old ddl', 'columns': [
        {'name': 'id', 'primary_key': True, 'unique': False},
        {'name': 'note', 'primary_key': False, 'unique': False,
         'references': None, 'server_default': None},
    ]}]
    new = deepcopy(old)
    new[0]['columns'].pop(1)
    new[0]['create_sql_preview'] = 'after ddl'
    if change:
        new[0]['columns'][0][change] = 'changed'
    return old, new


def test_only_one_missing_column_is_recognized():
    old, new = _schemas()
    assert remove.single_column_removal(old, new)
    assert not remove.single_column_removal(old, _schemas(change='primary_key')[1])
    assert not remove.single_column_removal(old, old)
    assert not remove.single_column_removal(old, [])
    assert not remove.single_column_removal(old, [{'name': 'unrelated', 'columns': []}])
    double = deepcopy(old)
    double[0]['columns'].append({'name': 'other'})
    assert not remove.single_column_removal(double, new)


def test_non_table_preview_fixtures_are_not_removals():
    old, new = _schemas()
    assert not remove.single_column_removal(old, ['changed'])
    assert not remove.single_column_removal(['changed'], new)
    assert not remove.single_column_removal(old, [None])
    assert not remove.single_column_removal(old, {'tables': new})


class FakeConnection:
    dialect = 'postgresql'

    def __init__(self, *, values=False):
        self.values = values
        self.sql = []

    def fetch_first(self, sql, params=None):
        self.sql.append(sql)
        if 'IS NOT NULL LIMIT 1' in sql:
            return {'present': 1} if self.values else None
        return None


def _setup(monkeypatch, *, values=False, dependency=False, indexed=False,
           keyed=False, other_change=False):
    old, new = _schemas(change='primary_key' if other_change else None)
    if keyed:
        old[0]['columns'][1]['unique'] = True
    model = SimpleNamespace(__table_name__='postgres_item')
    monkeypatch.setattr(remove, 'validate_init_models', lambda models, **kw: [model])
    monkeypatch.setattr(remove, 'read_history', lambda *args: ([{'id': 1}],
                          {'models': old, 'physical_schema': [{'table': 'postgres_item'}]}))
    monkeypatch.setattr(remove, 'schema_snapshot', lambda *args, **kw: {'tables': new})
    monkeypatch.setattr(remove, 'inspect_table', lambda *args: TableInfo(
        'postgres_item', True,
        (ColumnInfo('id', 'bigint', False, True), ColumnInfo('note', 'varchar(80)', True, False)),
        (), ('postgres_item_pkey', 'unknown_index') if indexed else ('postgres_item_pkey',),
        True, True, (), ()))
    monkeypatch.setattr(remove, '_foreign_key_dependency', lambda *args: dependency)
    monkeypatch.setattr(remove, '_verify_simple_constraints', lambda *args, **kw: None)
    return FakeConnection(values=values), [model]


def test_empty_column_risk_review_does_not_request_backup(monkeypatch):
    connection, models = _setup(monkeypatch)
    operation, digest, count = remove.review_pg_column_removal(
        connection, models, '.', 'data_postgres', 'identity')
    assert operation == {'kind': 'drop_column', 'table': 'postgres_item',
                         'column': 'note', 'has_meaningful_data': False}
    assert len(digest) == 64 and count == 1
    assert any('IS NOT NULL LIMIT 1' in query for query in connection.sql)


def test_populated_column_is_classified_as_destructive(monkeypatch):
    connection, models = _setup(monkeypatch, values=True)
    operation, digest, _ = remove.review_pg_column_removal(
        connection, models, '.', 'data_postgres', 'identity')
    assert operation['has_meaningful_data'] is True
    empty, models = _setup(monkeypatch)
    assert remove.review_pg_column_removal(empty, models, '.', 'data_postgres', 'identity')[1] != digest


@pytest.mark.parametrize('case,reason', [
    ('dependency', 'foreign-key'),
    ('indexed', 'dependencies'),
    ('keyed', 'Keyed'),
    ('other_change', 'Only one'),
])
def test_refuses_dependencies_and_mixed_changes(monkeypatch, case, reason):
    connection, models = _setup(monkeypatch, **{case: True})
    with pytest.raises(ValueError, match=reason):
        remove.review_pg_column_removal(connection, models, '.', 'data_postgres', 'identity')


def test_refuses_other_backends_before_ddl(monkeypatch):
    connection, models = _setup(monkeypatch)
    connection.dialect = 'mysql'
    with pytest.raises(ValueError, match='PostgreSQL'):
        remove.review_pg_column_removal(connection, models, '.', 'data_mysql', 'identity')


def test_no_confirmation_or_automation_is_allowed(monkeypatch):
    connection, models = _setup(monkeypatch)
    args = SimpleNamespace(confirm='yes', apply=False, approve=None, backup=None, name=None)
    with pytest.raises(PermissionError, match='independent interactive'):
        remove.run_pg_column_removal(args, None, connection, models, '.',
                                     'data_postgres', 'identity', None)
    args.confirm = None
    args.apply = True
    with pytest.raises(PermissionError, match='ordinary interactive'):
        remove.run_pg_column_removal(args, None, connection, models, '.',
                                     'data_postgres', 'identity', None)


class TransactionalConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def context():
            try:
                yield self
                self.commits += 1
            except Exception:
                self.rollbacks += 1
                raise
        return context()

    def fetch_first(self, sql, params=None):
        if 'pg_try_advisory_xact_lock' in sql:
            return {'acquired': True}
        if 'COUNT(*) AS total' in sql:
            return {'total': 1}
        return super().fetch_first(sql, params)

    def execute_write(self, sql, params=None):
        self.statements.append((sql, params))


def _execution_setup(monkeypatch, *, meaningful):
    from aurora.database import cli_init
    conn = TransactionalConnection()
    monkeypatch.setattr(remove.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *args: 'same')
    model = SimpleNamespace(__table_name__='postgres_item')
    def review(*args):
        return ({'kind': 'drop_column', 'table': 'postgres_item',
                 'column': 'note', 'has_meaningful_data': meaningful},
                'a' * 64, 2)
    monkeypatch.setattr(remove, 'review_pg_column_removal', review)
    backup_calls = []
    monkeypatch.setattr(remove, 'verify_server_declared_schema', lambda *args, **kwargs: None)
    monkeypatch.setattr(remove, '_write_snapshot', lambda *args: SimpleNamespace(name='success.json'))
    monkeypatch.setattr(remove, 'history_table', lambda *args: '__migrations__')
    # Last persisted history reflects the record written in this transaction.
    def recorded(*args):
        inserted = [params for sql, params in conn.statements if sql.startswith('INSERT INTO')]
        return ([None, None, inserted[-1]], {})
    monkeypatch.setattr(remove, 'read_history', recorded)
    config = SimpleNamespace(resolve=lambda *args: 'postgresql+psycopg://user@localhost/data_postgres')
    args = SimpleNamespace(config='aurora_databases.json', confirm=None, title='Remove note',
                           apply=False, approve=None, backup=None, name=None)
    return conn, model, args, config, backup_calls


def test_populated_removal_recommends_backup_without_creating_one(monkeypatch, capsys):
    conn, model, args, config, backups = _execution_setup(monkeypatch, meaningful=True)
    approvals = iter(['y', 'DELETE data_postgres.postgres_item.note'])
    monkeypatch.setattr('builtins.input', lambda _: next(approvals))
    assert remove.run_pg_column_removal(
        args, config, conn, [model], '.', 'data_postgres', 'same', None) == 0
    assert not backups
    output = capsys.readouterr().out
    assert 'WARNING' in output and 'aurora db backup' in output
    assert 'will not automatically create or restore' in output
    assert conn.commits == 1 and conn.rollbacks == 0
    assert sum('DROP COLUMN' in sql for sql, _ in conn.statements) == 1
    recorded = [param for sql, param in conn.statements if sql.startswith('INSERT INTO')][0]
    assert recorded['id'] == 3
    assert 'Confirmed permanent data loss' in recorded['warnings']
    assert 'backup not created automatically' in recorded['warnings']


def test_bad_typed_ack_never_starts_transaction_or_backup(monkeypatch):
    conn, model, args, config, backups = _execution_setup(monkeypatch, meaningful=True)
    approvals = iter(['y', 'yes'])
    monkeypatch.setattr('builtins.input', lambda _: next(approvals))
    assert remove.run_pg_column_removal(
        args, config, conn, [model], '.', 'data_postgres', 'same', None) == 0
    assert conn.commits == 0 and not backups and not conn.statements


def test_empty_column_drops_without_backup_or_typed_gate(monkeypatch):
    conn, model, args, config, backups = _execution_setup(monkeypatch, meaningful=False)
    approvals = iter(['y'])
    monkeypatch.setattr('builtins.input', lambda _: next(approvals))
    assert remove.run_pg_column_removal(
        args, config, conn, [model], '.', 'data_postgres', 'same', None) == 0
    assert not backups
    assert sum('DROP COLUMN' in sql for sql, _ in conn.statements) == 1
    recorded = [param for sql, param in conn.statements if sql.startswith('INSERT INTO')][0]
    assert recorded['warnings'] is None


def test_changed_proposal_under_lock_refuses_before_ddl(monkeypatch):
    conn, model, args, config, backups = _execution_setup(monkeypatch, meaningful=True)
    initial = remove.review_pg_column_removal
    calls = 0
    def change(*args):
        nonlocal calls
        calls += 1
        operation, digest, count = initial(*args)
        return operation, (digest if calls == 1 else 'b' * 64), count
    monkeypatch.setattr(remove, 'review_pg_column_removal', change)
    approvals = iter(['y', 'DELETE data_postgres.postgres_item.note'])
    monkeypatch.setattr('builtins.input', lambda _: next(approvals))
    with pytest.raises(RuntimeError, match='changed after approval'):
        remove.run_pg_column_removal(
            args, config, conn, [model], '.', 'data_postgres', 'same', None)
    assert conn.rollbacks == 1 and not backups
    assert not any('DROP COLUMN' in sql for sql, _ in conn.statements)
