"""MySQL FK eligibility, physical index identity and nontransactional failure guard."""
from copy import deepcopy
from types import SimpleNamespace
import pytest

from aurora.database import server_forward_fk_mysql as fk
from aurora.database.inspection import ColumnInfo, TableInfo


def _tables(reference=False):
    return [
        {'name': 'child', 'create_sql_preview': 'ignored', 'columns': [
            {'name': 'id', 'type': 'int', 'primary_key': True, 'unique': False,
             'nullable': False, 'references': None},
            {'name': 'parent_id', 'type': 'int', 'primary_key': False, 'unique': False,
             'nullable': True, 'references': 'parent.id' if reference else None}]},
        {'name': 'parent', 'create_sql_preview': 'ignored', 'columns': [
            {'name': 'id', 'type': 'int', 'primary_key': True, 'unique': False,
             'nullable': False, 'references': None}]},
    ]


class Conn:
    dialect = 'mysql'
    def __init__(self, orphans=False):
        self.orphans = orphans
        self.sql = []
    def fetch_first(self, sql, params=None):
        self.sql.append(sql)
        if 'SELECT DATABASE()' in sql:
            return {'db': 'data_mysql'}
        if 'LEFT JOIN' in sql and self.orphans:
            return {'present': 1}
        return None
    def fetch_all(self, sql, params=None):
        self.sql.append(sql)
        if 'TABLE_CONSTRAINTS' in sql and 'CONSTRAINT_TYPE AS kind' in sql:
            return [{'kind': 'PRIMARY KEY'}]
        return []


def _setup(monkeypatch, *, adding=True, orphans=False, indexes=None, existing=None, support=None):
    before, after = _tables(not adding), _tables(adding)
    models = [SimpleNamespace(__table_name__='child', __columns__={
        'parent_id': SimpleNamespace(primary_key=False, unique=False, not_null=False)}),
        SimpleNamespace(__table_name__='parent', __columns__={
            'id': SimpleNamespace(primary_key=True)})]
    monkeypatch.setattr(fk, 'validate_init_models', lambda *a, **kw: models)
    monkeypatch.setattr(fk, 'read_history', lambda *a: ([{'id':1}],
        {'models': before, 'physical_schema': [{'name':'child'}, {'name':'parent'}]}))
    monkeypatch.setattr(fk, 'schema_snapshot', lambda *a, **kw: {'tables': after})
    monkeypatch.setattr(fk, '_resolved_foreign_keys',
                        lambda *a: {'parent_id': ('parent', 'id', 'NO ACTION', 'NO ACTION')})
    monkeypatch.setattr(fk, '_mysql_constraint', lambda *a: existing)
    monkeypatch.setattr(fk, '_supporting_index', lambda *a: support)
    def inspect(conn, name):
        if name == 'parent':
            return TableInfo('parent', True, (ColumnInfo('id','bigint',False,True),),
                             (), ('PRIMARY',), True, True, (), ())
        return TableInfo('child', True,
            (ColumnInfo('id','bigint',False,True), ColumnInfo('parent_id','bigint',True,False)),
            (), tuple(indexes or (('PRIMARY',) if adding else ('PRIMARY','aurora_fk_child_parent_id'))),
            True, True, () if adding else (('parent_id','parent','id'),),
            () if adding else (('parent_id','RESTRICT','RESTRICT'),))
    monkeypatch.setattr(fk, 'inspect_table', inspect)
    monkeypatch.setattr(fk, '_simple_table', lambda *a, **kw: None)
    return Conn(orphans), models


def test_mysql_fk_add_uses_explicit_owned_supporting_index(monkeypatch):
    conn, models = _setup(monkeypatch)
    operation, digest, count = fk.review_mysql_fk(conn, models, '.', 'data_mysql', 'identity')
    assert (operation['kind'], count, len(digest)) == ('add_foreign_key', 1, 64)
    assert 'ADD INDEX `aurora_fk_child_parent_id`' in operation['sql']
    assert 'ADD CONSTRAINT `aurora_fk_child_parent_id`' in operation['sql']
    assert 'ON UPDATE NO ACTION ON DELETE NO ACTION' in operation['sql']
    assert any('LEFT JOIN' in sql for sql in conn.sql)


def test_mysql_fk_rejects_orphans_and_unrelated_index(monkeypatch):
    conn, models = _setup(monkeypatch, orphans=True)
    with pytest.raises(ValueError, match='Orphan'):
        fk.review_mysql_fk(conn, models, '.', 'data_mysql', 'identity')
    conn, models = _setup(monkeypatch, indexes=['PRIMARY','other'])
    with pytest.raises(ValueError, match='Other indexes'):
        fk.review_mysql_fk(conn, models, '.', 'data_mysql', 'identity')


def test_mysql_fk_drop_only_owned_constraint_and_index(monkeypatch):
    existing = ('parent_id','parent','id')
    conn, models = _setup(monkeypatch, adding=False, existing=existing,
                          support='parent_id')
    operation, _, _ = fk.review_mysql_fk(conn, models, '.', 'data_mysql', 'identity')
    assert operation['kind'] == 'drop_foreign_key'
    assert 'DROP FOREIGN KEY `aurora_fk_child_parent_id`' in operation['sql']
    assert 'DROP INDEX `aurora_fk_child_parent_id`' in operation['sql']
    conn, models = _setup(monkeypatch, adding=False, existing=existing, support=None)
    with pytest.raises(ValueError, match='exact Aurora-owned'):
        fk.review_mysql_fk(conn, models, '.', 'data_mysql', 'identity')


def test_mysql_fk_catalog_requires_exact_non_cascade_single_column():
    class Catalog:
        def fetch_all(self, sql, params=None):
            return [dict(source_column='parent_id', target_table='parent',
                         target_column='id', target_schema='data_mysql', position=1,
                         update_rule='RESTRICT', delete_rule='RESTRICT', kind='FOREIGN KEY')]
        def fetch_first(self, sql, params=None):
            return {'db':'data_mysql'}
    c = Catalog()
    assert fk._mysql_constraint(c, 'child', 'fk') == ('parent_id','parent','id')
    class Bad(Catalog):
        def fetch_all(self, sql, params=None):
            rows = super().fetch_all(sql, params)
            rows[0]['delete_rule'] = 'CASCADE'
            return rows
    with pytest.raises(ValueError, match='supported contract'):
        fk._mysql_constraint(Bad(), 'child', 'fk')


def test_mysql_fk_supporting_index_rejects_prefix_or_composite():
    class Index:
        def fetch_all(self, sql, params=None):
            return [dict(col='parent_id', non_unique=1, method='BTREE',
                         prefix_length=None, direction='A', position=1)]
    assert fk._supporting_index(Index(), 'child','fk') == 'parent_id'
    class Prefix(Index):
        def fetch_all(self, sql, params=None):
            rows = super().fetch_all(sql, params)
            rows[0]['prefix_length'] = 3
            return rows
    with pytest.raises(ValueError, match='supporting index'):
        fk._supporting_index(Prefix(),'child','fk')
