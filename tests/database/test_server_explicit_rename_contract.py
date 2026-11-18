"""Bounded PostgreSQL JSON-only column rename and source-sync contracts."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aurora.database import server_forward_rename as rename
from aurora.database import cli_explicit_operations as cli


OP = {'kind': 'rename_columns', 'table': 'items', 'columns': {'note': 'description'}}


class FakeConnection:
    dialect = 'postgresql'

    def __init__(self, *, inbound=False):
        self.inbound = inbound

    def fetch_first(self, sql, params=None):
        assert 'pg_catalog.pg_constraint' in sql
        assert params == {'table': 'items'}
        return {'present': 1} if self.inbound else None


def _models():
    old = SimpleNamespace(__table_name__='items', __columns__={
        'note': SimpleNamespace(primary_key=False, unique=False, references=None, default=None)})
    new = SimpleNamespace(__table_name__='items', __columns__={
        'description': SimpleNamespace(primary_key=False, unique=False, references=None, default=None)})
    return [old], [new]


def _snapshot(models):
    key = 'note' if 'note' in models[0].__columns__ else 'description'
    columns = [{'name': key, 'type': 'str'}]
    if 'extra' in models[0].__columns__:
        columns.append({'name': 'extra', 'type': 'str'})
    columns.sort(key=lambda field: field['name'])
    return {'tables': [{'name': 'items', 'columns': columns,
                        'create_sql_preview': 'CREATE TABLE items (...)'}]}


def _review(*, inbound=False, foreign_keys=(), indexes=('items_pkey',),
            operation=None, alter_models=None):
    before, after = _models()
    if alter_models:
        alter_models(before, after)
    previous = _snapshot(before)['tables']
    recorded = {'models': deepcopy(previous), 'physical_schema': [{'table': 'items'}]}
    witness = SimpleNamespace(exists=True, constraints_verified=True,
                              foreign_key_actions=(), indexes=indexes,
                              unique_columns=(), foreign_keys=foreign_keys,
                              columns=[SimpleNamespace(name='id'), SimpleNamespace(name='note')])
    with (patch.object(rename, 'validate_init_models', side_effect=lambda models, **kw: models),
          patch.object(rename, 'read_history', return_value=([{'id': 1}], recorded)),
          patch.object(rename, 'schema_snapshot', side_effect=lambda *models, **kw: _snapshot(models)),
          patch.object(rename, 'inspect_table', return_value=witness),
          patch.object(rename, 'verify_server_declared_schema'),
          patch.object(rename, 'history_table', return_value='__migrations__')):
        return rename._review(FakeConnection(inbound=inbound), before, after,
                              '/project', 'data_postgres', 'selected-db', operation or OP)


def test_review_accepts_exact_unindexed_nullable_field_rename():
    proposed, digest, count = _review()
    assert proposed == {'table': 'items', 'old': 'note', 'new': 'description'}
    assert len(digest) == 64 and count == 1


@pytest.mark.parametrize('kind', ['primary_key', 'unique', 'references', 'default'])
def test_review_refuses_keyed_referenced_or_defaulted_source(kind):
    def change(before, after):
        setattr(before[0].__columns__['note'], kind,
                'parent.id' if kind == 'references' else 'x' if kind == 'default' else True)
    with pytest.raises(ValueError, match='Dependent|keyed|defaulted'):
        _review(alter_models=change)


@pytest.mark.parametrize('condition', [
    {'inbound': True},
    {'foreign_keys': (('note', 'parent', 'id'),)},
    {'indexes': ('items_pkey', 'extra_idx')},
])
def test_review_refuses_unverified_dependencies(condition):
    with pytest.raises(ValueError, match='dependenc|constraint|index'):
        _review(**condition)


def test_review_refuses_mixed_changes():
    def change(before, after):
        after[0].__columns__['extra'] = SimpleNamespace()
    with pytest.raises(ValueError, match='only model-schema change'):
        _review(alter_models=change)


def test_review_refuses_multiple_columns():
    op = dict(OP, columns={'note': 'description', 'other': 'elsewhere'})
    with pytest.raises(ValueError, match='exactly one'):
        _review(operation=op)


def test_explicit_operations_dispatches_postgresql_rename_only():
    from pathlib import Path
    from types import SimpleNamespace
    args = SimpleNamespace(review_file=None, name=None, schema=None, table=None,
        column=None, approve_review=None, approve_sql=None, output_dir='migrations',
        model=None, explain=None, models_config='aurora_models.json', dry_run=True,
        apply=False, approve=None, confirm=None, backup=None, check=False,
        format='text', config='aurora_databases.json', changes='rename.json',
        database='data_postgres')
    config = SimpleNamespace(resolve=lambda name: 'postgresql://localhost/db')
    with (patch.object(cli, '_load_config', return_value=(config, Path('/project'))),
          patch.object(cli, 'load_explicit_operations', return_value={
              'database': 'data_postgres', 'operation': OP}),
          patch('aurora.database.server_forward_rename.run_server_rename_operation', return_value=7) as run):
        assert cli.run_explicit_operations(args) == 7
        assert run.call_args.args[1]['operation'] == OP


class FakeTransactionConnection:
    dialect = 'postgresql'

    def __init__(self):
        self.writes = []
        self.transactions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    from contextlib import contextmanager

    @contextmanager
    def transaction(self):
        self.transactions.append('begin')
        try:
            yield
        except BaseException:
            self.transactions.append('rollback')
            raise
        else:
            self.transactions.append('commit')

    def fetch_first(self, sql, params=None):
        assert 'pg_try_advisory_xact_lock' in sql
        return {'acquired': True}

    def execute_write(self, sql, params=None):
        self.writes.append((sql, params))


def _run_rename(*, dry_run=False, confirm='yes', fail_snapshot=False):
    from contextlib import nullcontext
    from pathlib import Path
    db = FakeTransactionConnection()
    root = Path('/project')
    edits = {root / 'models' / 'Item.py': (b'note', b'description')}
    args = SimpleNamespace(apply=False, approve=None, backup=None,
                           dry_run=dry_run, format='text', check=False,
                           database='data_postgres', title='Rename note')
    config = SimpleNamespace(resolve=lambda name: 'postgresql://localhost/db',
                             connect=lambda name: db)
    renamed = {'table': 'items', 'old': 'note', 'new': 'description'}
    mocks = (
        patch('aurora.database.cli_plan._select_models', return_value=(['models.Item:Item'], ['model'])),
        patch.object(rename, 'source_changes', return_value=edits),
        patch.object(rename, 'staged_model_source', side_effect=lambda changes: nullcontext()),
        patch('aurora.database.cli_init._postgres_database_identity', return_value='identity'),
        patch.object(rename, '_review', return_value=(renamed, 'a' * 64, 1)),
        patch.object(rename, 'verify_server_declared_schema'),
        patch.object(rename, 'history_table', return_value='__migrations__'),
        patch.object(rename, 'apply_source_changes', return_value=[('applied',)]),
        patch.object(rename, 'restore_source_changes'),
        patch.object(rename, '_write_snapshot', side_effect=RuntimeError('snapshot rejected')
                     if fail_snapshot else None,
                     return_value=Path('next.json') if not fail_snapshot else None),
        patch.object(rename, 'read_history', return_value=([{'id': 1}, dict(
            id=2, title='Rename note', snapshot='next.json', warnings=None,
            date='2026-11-18T00:00:00+00:00')], {})),
    )
    from contextlib import ExitStack
    with ExitStack() as stack:
        captures = [stack.enter_context(mock) for mock in mocks]
        # Keep date comparison independent of wall-clock time.
        if not dry_run and not fail_snapshot:
            original_read = rename.read_history
            def history(*a, **kw):
                from datetime import datetime, timezone
                return ([{'id': 1}, dict(id=2, title='Rename note', snapshot='next.json',
                    warnings=None, date=datetime.now(timezone.utc).isoformat(timespec='seconds'))], {})
            captures[-1].side_effect = history
        if fail_snapshot:
            with pytest.raises(RuntimeError, match='snapshot rejected'):
                rename.run_server_rename_operation(args, {'operation': OP}, config, root,
                                                    simple_confirm=not dry_run, interactive=False)
        else:
            assert rename.run_server_rename_operation(args, {'operation': OP}, config, root,
                                                      simple_confirm=not dry_run, interactive=False) == 0
        return db, captures


def test_readonly_rename_preview_has_no_writes_or_source_changes():
    db, mocks = _run_rename(dry_run=True)
    assert not db.writes and not db.transactions
    mocks[7].assert_not_called()


def test_postgresql_rename_records_one_successor_after_ddl():
    db, mocks = _run_rename()
    assert db.transactions == ['begin', 'commit']
    assert len(db.writes) == 2
    assert 'RENAME COLUMN "note" TO "description"' in db.writes[0][0]
    assert 'INSERT INTO __migrations__' in db.writes[1][0]
    mocks[7].assert_called_once()
    mocks[8].assert_not_called()


def test_failed_history_snapshot_rolls_back_ddl_and_source_edit():
    db, mocks = _run_rename(fail_snapshot=True)
    assert db.transactions == ['begin', 'rollback']
    assert len(db.writes) == 1
    mocks[7].assert_called_once()
    mocks[8].assert_called_once()


def test_real_model_snapshot_sorting_still_recognizes_one_rename():
    from aurora.database import Model, column
    from aurora.database.snapshots import schema_snapshot as real_snapshot
    class Previous(Model):
        __table__ = 'items'
        id = column(int, primary_key=True)
        note = column(str, length=80, not_null=False)
    class Updated(Model):
        __table__ = 'items'
        id = column(int, primary_key=True)
        description = column(str, length=80, not_null=False)
    old = real_snapshot(Previous, dialect='postgresql', allow_v2=True)['tables']
    observed = SimpleNamespace(exists=True, constraints_verified=True,
                               foreign_key_actions=(), indexes=('items_pkey',),
                               unique_columns=(), foreign_keys=(),
                               columns=[SimpleNamespace(name='id'), SimpleNamespace(name='note')])
    with (patch.object(rename, 'read_history', return_value=([{'id': 1}],
              {'models': old, 'physical_schema': [{'table': 'items'}]})),
          patch.object(rename, 'inspect_table', return_value=observed),
          patch.object(rename, 'verify_server_declared_schema'),
          patch.object(rename, 'history_table', return_value='__migrations__')):
        operation, _, count = rename._review(FakeConnection(), [Previous], [Updated],
                                               '/project', 'data_postgres', 'identity', OP)
        assert operation['old'] == 'note' and count == 1
