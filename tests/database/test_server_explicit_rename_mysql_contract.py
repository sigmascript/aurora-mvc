"""Single-column MySQL JSON rename cannot bypass physical or source preflight."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aurora.database import server_forward_rename_mysql as rename
from aurora.database import cli_explicit_operations as cli

OP = {'kind': 'rename_columns', 'table': 'items', 'columns': {'note': 'description'}}


class FakeConnection:
    dialect = 'mysql'

    def __init__(self, *, inbound=False, triggers=False):
        self.inbound = inbound
        self.triggers = triggers

    def fetch_first(self, sql, params=None):
        assert params == {'table': 'items'}
        if 'KEY_COLUMN_USAGE' in sql:
            return {'present': 1} if self.inbound else None
        if 'TRIGGERS' in sql:
            return {'present': 1} if self.triggers else None
        raise AssertionError(sql)


def models():
    before = SimpleNamespace(__table_name__='items', __columns__={
        'note': SimpleNamespace(primary_key=False, unique=False, references=None, default=None)})
    after = SimpleNamespace(__table_name__='items', __columns__={
        'description': SimpleNamespace(primary_key=False, unique=False, references=None, default=None)})
    return [before], [after]


def snapshot(items):
    fields = [{'name': key, 'type': 'str'} for key in items[0].__columns__]
    fields.sort(key=lambda item: item['name'])
    return {'tables': [{'name': 'items', 'columns': fields,
                        'create_sql_preview': 'CREATE TABLE ...'}]}


def reviewed(*, indexes=('PRIMARY',), inbound=False, triggers=False, fk=(), change=None, op=None):
    before, after = models()
    if change:
        change(before, after)
    previous = deepcopy(snapshot(before)['tables'])
    witness = SimpleNamespace(exists=True, constraints_verified=True,
                              foreign_key_actions=(), indexes=indexes,
                              unique_columns=(), foreign_keys=fk,
                              columns=[SimpleNamespace(name='id'), SimpleNamespace(name='note')])
    with (patch.object(rename, 'validate_init_models', side_effect=lambda objects, **kw: objects),
          patch.object(rename, 'read_history', return_value=([{'id': 1}],
                                                             {'models': previous, 'physical_schema': []})),
          patch.object(rename, 'schema_snapshot', side_effect=lambda *items, **kw: snapshot(items)),
          patch.object(rename, 'inspect_table', return_value=witness),
          patch.object(rename, 'verify_server_declared_schema'),
          patch.object(rename, 'history_table', return_value='__migrations__')):
        return rename.review_mysql_rename(FakeConnection(inbound=inbound, triggers=triggers),
                                          before, after, '/project', 'data_mysql', 'identity', op or OP)


def test_single_plain_rename_ready():
    operation, digest, count = reviewed()
    assert operation == {'table': 'items', 'old': 'note', 'new': 'description'}
    assert len(digest) == 64 and count == 1


@pytest.mark.parametrize('property_', ('primary_key', 'unique', 'references', 'default'))
def test_unsafe_source_field_refused(property_):
    def change(before, after):
        setattr(before[0].__columns__['note'], property_,
                'other.id' if property_ == 'references' else True)
    with pytest.raises(ValueError, match='Dependent|keyed|defaulted'):
        reviewed(change=change)


@pytest.mark.parametrize('kwargs', ({'indexes': ('PRIMARY', 'extra')},
                                     {'inbound': True}, {'triggers': True},
                                     {'fk': (('note', 'other', 'id'),)}))
def test_dependencies_refused(kwargs):
    with pytest.raises(ValueError, match='dependenc|index|constraint|triggers'):
        reviewed(**kwargs)


def test_multi_column_refused():
    with pytest.raises(ValueError, match='Exactly one'):
        reviewed(op=dict(OP, columns={'note': 'description', 'other': 'extra'}))


def test_mixed_source_model_changes_refused():
    def change(before, after):
        after[0].__columns__['extra'] = SimpleNamespace()
    with pytest.raises(ValueError, match='only model-schema change'):
        reviewed(change=change)


def test_mysql_dispatch_routes_only_explicit_rename():
    import inspect
    source = inspect.getsource(cli.run_explicit_operations) if hasattr(cli, 'run_explicit_operations') else inspect.getsource(cli)
    assert 'run_mysql_rename_operation' in source
    assert 'dialect == "mysql"' in source


def test_mysql_rename_uses_migration_lock_and_uncertain_outcome_failure():
    import inspect
    source = inspect.getsource(rename.run_mysql_rename_operation)
    assert 'with migration_lock(connection):' in source
    assert 'review_mysql_rename(' in source
    assert 'outcome uncertain after DDL began' in source
    assert 'restore_source_changes' not in source
