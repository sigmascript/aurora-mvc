"""Bounded PostgreSQL foreign-key successors and explicit refusal boundaries."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from aurora.database import server_forward_fk as fk
from aurora.database.inspection import ColumnInfo, TableInfo


def _tables(reference=False):
    parent = {'name': 'parent', 'create_sql_preview': 'parent', 'columns': [
        {'name': 'id', 'type': 'int', 'primary_key': True, 'unique': False,
         'nullable': False, 'references': None}]}
    child = {'name': 'child', 'create_sql_preview': 'child', 'columns': [
        {'name': 'id', 'type': 'int', 'primary_key': True, 'unique': False,
         'nullable': False, 'references': None},
        {'name': 'parent_id', 'type': 'int', 'primary_key': False, 'unique': False,
         'nullable': True, 'references': 'parent.id' if reference else None}]}
    return [child, parent]


def test_fk_delta_requires_one_existing_column_only():
    before, after = _tables(), _tables(True)
    assert fk.single_fk_delta(before, after)
    assert fk.single_fk_delta(after, before)
    assert not fk.single_fk_delta(after, after)
    modified = deepcopy(after)
    modified[0]['columns'][1]['nullable'] = False
    assert not fk.single_fk_delta(before, modified)
    modified = deepcopy(after)
    modified[0]['columns'].append({'name': 'new_col'})
    assert not fk.single_fk_delta(before, modified)
    modified = deepcopy(after)
    modified[0]['columns'][1]['references'] = 'other.id'
    assert not fk.single_fk_delta(after, modified)


def test_fk_names_refuse_overlong_names():
    assert fk._fk_name('child', 'parent_id') == 'aurora_fk_child_parent_id'
    with pytest.raises(ValueError, match='portable'):
        fk._fk_name('a' * 60, 'x')


class FakeConnection:
    dialect = 'postgresql'
    def __init__(self, orphans=False):
        self.orphans = orphans
        self.queries = []
    def fetch_first(self, sql, params=None):
        self.queries.append(sql)
        if 'LEFT JOIN' in sql and self.orphans:
            return {'present': 1}
        return None


def _setup(monkeypatch, *, adding=True, orphans=False, unrelated_index=False, existing=None):
    before, after = _tables(not adding), _tables(adding)
    parent_field = SimpleNamespace(primary_key=True)
    child_field = SimpleNamespace(primary_key=False, unique=False)
    models = [SimpleNamespace(__table_name__='child', __columns__={'parent_id': child_field}),
              SimpleNamespace(__table_name__='parent', __columns__={'id': parent_field})]
    monkeypatch.setattr(fk, 'validate_init_models', lambda *a, **k: models)
    monkeypatch.setattr(fk, 'read_history', lambda *a: ([{'id': 1}],
        {'models': before, 'physical_schema': [{'table':'child'}, {'table':'parent'}]}))
    monkeypatch.setattr(fk, 'schema_snapshot', lambda *a, **k: {'tables': after})
    monkeypatch.setattr(fk, 'inspect_table', lambda *a: TableInfo(
        'child', True, (ColumnInfo('id','bigint',False,True),
                        ColumnInfo('parent_id','bigint',True,False)), (),
        ('child_pkey','other') if unrelated_index else ('child_pkey',),
        True, True,
        (('parent_id','parent','id'),) if not adding else (),
        (('parent_id','NO ACTION','NO ACTION'),) if not adding else ()))
    monkeypatch.setattr(fk, '_resolved_foreign_keys',
                        lambda *a: {'parent_id': ('parent','id','NO ACTION','NO ACTION')})
    monkeypatch.setattr(fk, '_fk_catalog', lambda *a: existing)
    return FakeConnection(orphans), models


def test_fk_add_preflights_orphan_records_and_verifies_target(monkeypatch):
    connection, models = _setup(monkeypatch)
    operation, digest, count = fk.review_fk(connection, models, '.', 'data', 'identity')
    assert operation['kind'] == 'add_foreign_key'
    assert operation['target_table'] == 'parent'
    assert 'ON DELETE NO ACTION' in operation['sql']
    assert len(digest) == 64 and count == 1
    assert any('LEFT JOIN' in q for q in connection.queries)
    connection, models = _setup(monkeypatch, orphans=True)
    with pytest.raises(ValueError, match='Orphan'):
        fk.review_fk(connection, models, '.', 'data', 'identity')


def test_fk_rejects_extra_index(monkeypatch):
    connection, models = _setup(monkeypatch, unrelated_index=True)
    with pytest.raises(ValueError, match='unsupported indexes'):
        fk.review_fk(connection, models, '.', 'data', 'identity')


def test_fk_drop_requires_exact_aurora_constraint(monkeypatch):
    connection, models = _setup(monkeypatch, adding=False,
        existing=('parent_id','parent','id'))
    operation, _, _ = fk.review_fk(connection, models, '.', 'data', 'identity')
    assert operation['kind'] == 'drop_foreign_key'
    assert 'DROP CONSTRAINT' in operation['sql']
    monkeypatch.setattr(fk, '_fk_catalog', lambda *a: None)
    with pytest.raises(ValueError, match='exact Aurora-owned'):
        fk.review_fk(connection, models, '.', 'data', 'identity')


def test_mysql_fk_uses_independent_guarded_reviewer(monkeypatch):
    connection, models = _setup(monkeypatch)
    connection.dialect = 'mysql'
    from aurora.database import server_forward_fk_mysql as mysql_fk
    monkeypatch.setattr(mysql_fk, 'review_mysql_fk', lambda *args: ('mysql', 'digest', 1))
    assert fk.review_fk(connection, models, '.', 'data', 'identity') == ('mysql', 'digest', 1)
