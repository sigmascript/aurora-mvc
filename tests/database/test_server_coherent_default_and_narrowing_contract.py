"""Bounded MySQL/PostgreSQL default edits and data-aware VARCHAR narrowing."""

from types import SimpleNamespace

import pytest

from aurora.database.models import Model, column
from aurora.database.planning import SchemaChange, SchemaPlan
from aurora.database import server_model_review as review
from aurora.database import planning
from aurora.database.server_initial_verification import server_default_matches


@pytest.fixture
def catalog(monkeypatch):
    state = {'type': 'VARCHAR(500)', 'default': '7', 'max_length': None,
             'kinds': [('default_mismatch', 'visits')]}
    def plan(connection, model):
        return SchemaPlan(model.__table_name__, True,
                          tuple(SchemaChange(kind, col, '') for kind, col in state['kinds']), ())
    def reflect(connection, name):
        primary = 'PRIMARY' if connection.dialect == 'mysql' else f'{name}_pkey'
        return SimpleNamespace(
            exists=True, uniqueness_verified=True, constraints_verified=True,
            indexes=(primary,), unique_columns=(),
            columns=(SimpleNamespace(name='id', sql_type='BIGINT', nullable=False,
                                     primary_key=True, default_sql=None),
                     SimpleNamespace(name='visits', sql_type='BIGINT', nullable=False,
                                     primary_key=False, default_sql=state['default']),
                     SimpleNamespace(name='title', sql_type=state['type'], nullable=True,
                                     primary_key=False, default_sql=None)))
    monkeypatch.setattr(review, 'plan_schema', plan)
    monkeypatch.setattr(review, 'inspect_table', reflect)
    return state


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_replace_existing_literal_default_in_one_table_transition(catalog, dialect):
    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)
        title = column(str, length=500, not_null=False)
    con = SimpleNamespace(dialect=dialect)
    result = review.review_server_model_changes(con, (Messages,), 'default', 'identity')
    assert result.operations[0]['kind'] == 'alter_columns'
    assert result.operations[0]['edits'][0]['before']['default'] == '7'
    assert result.operations[0]['edits'][0]['after']['default'] == '9'
    if dialect == 'postgresql':
        assert 'ALTER COLUMN "visits" SET DEFAULT 9' in result.statements[0]
    else:
        assert '`visits` BIGINT NOT NULL DEFAULT 9' in result.statements[0]
    catalog['default'] = '6'
    assert result.review_sha256 != review.review_server_model_changes(
        con, (Messages,), 'default', 'identity').review_sha256


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_remove_existing_literal_default(catalog, dialect):
    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int)
        title = column(str, length=500, not_null=False)
    result = review.review_server_model_changes(SimpleNamespace(dialect=dialect),
                                                (Messages,), 'default', 'identity')
    if dialect == 'postgresql':
        assert 'ALTER COLUMN "visits" DROP DEFAULT' in result.statements[0]
    else:
        assert '`visits` BIGINT NOT NULL' in result.statements[0]
        assert 'DEFAULT' not in result.statements[0]


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_narrowing_and_default_replacement_coherently(catalog, dialect):
    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)
        title = column(str, length=100, not_null=False)
    catalog['kinds'] = [('default_mismatch', 'visits'), ('type_mismatch', 'title')]
    inspected = []
    def fetch(sql):
        inspected.append(sql)
        return {'max_length': catalog['max_length']}
    con = SimpleNamespace(dialect=dialect, fetch_first=fetch)
    catalog['max_length'] = 100
    result = review.review_server_model_changes(con, (Messages,), 'default', 'identity')
    assert len(result.statements) == 1
    assert 'VARCHAR(100)' in result.statements[0]
    assert len(result.operations[0]['edits']) == 2
    assert 'MAX(CHAR_LENGTH' in inspected[0]
    catalog['max_length'] = 101
    with pytest.raises(ValueError, match='existing value length 101 exceeds'):
        review.review_server_model_changes(con, (Messages,), 'default', 'identity')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_narrowing_empty_column_is_safe(catalog, dialect):
    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)
        title = column(str, length=100, not_null=False)
    catalog['kinds'] = [('type_mismatch', 'title')]
    catalog['default'] = '9'
    con = SimpleNamespace(dialect=dialect, fetch_first=lambda sql: {'max_length': None})
    proposal = review.review_server_model_changes(con, (Messages,), 'default', 'identity')
    assert 'VARCHAR(100)' in proposal.statements[0]


def test_planning_server_literal_default_comparison(monkeypatch):
    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)
    from aurora.database import inspection
    from aurora.database import planning as plan_module
    def table(connection, name):
        return SimpleNamespace(name=name, exists=True, columns=(
            SimpleNamespace(name='id', sql_type='BIGINT', nullable=False,
                            primary_key=True, default_sql=None),
            SimpleNamespace(name='visits', sql_type='BIGINT', nullable=False,
                            primary_key=False, default_sql=state['default'])),
            uniqueness_verified=True, indexes=('PRIMARY',),
            constraints_verified=True, foreign_key_actions=(),
            foreign_keys=(), unique_columns=())
    state = {'default': '9'}
    monkeypatch.setattr(plan_module, 'inspect_table', table)
    con = SimpleNamespace(dialect='mysql')
    assert not plan_module.plan_schema(con, Messages).changes
    state['default'] = '7'
    assert [c.kind for c in plan_module.plan_schema(con, Messages).changes] == ['default_mismatch']


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_unverified_expression_rejected(catalog, dialect):
    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)
        title = column(str, length=500, not_null=False)
    catalog['default'] = 'some_function()'
    with pytest.raises(ValueError, match='not a verified literal'):
        review.review_server_model_changes(SimpleNamespace(dialect=dialect),
                                           (Messages,), 'default', 'identity')

@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_narrowing_rechecks_live_data_before_any_ddl(catalog, monkeypatch, tmp_path, dialect):
    from contextlib import contextmanager
    from aurora.database import cli_init
    from aurora.database import server_forward_migration as executor

    class Messages(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)
        title = column(str, length=100, not_null=False)

    catalog['kinds'] = [('type_mismatch', 'title')]
    catalog['default'] = '9'
    state = {'max': 80, 'ddl': 0}
    def fetch(sql, *args):
        if 'MAX(CHAR_LENGTH' in sql:
            return {'max_length': state['max']}
        return {'acquired': True}

    class Connection:
        _transaction_active = False
        _migration_lock_active = False
        def __init__(self):
            self.dialect = dialect
        fetch_first = staticmethod(fetch)
        def execute_write(self, *args):
            state['ddl'] += 1
        @contextmanager
        def transaction(self):
            yield

    @contextmanager
    def lock(conn):
        yield

    connection = Connection()
    proposal = review.review_server_model_changes(connection, (Messages,), 'default', 'identity')
    state['max'] = 101
    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(executor, 'read_history', lambda *_: ([{'id': 1}], {}))
    monkeypatch.setattr(executor, 'history_table', lambda *_: '__migrations__')
    monkeypatch.setattr(executor, 'migration_lock', lock)
    with pytest.raises(ValueError, match='existing value length 101 exceeds'):
        executor.execute_server_forward_migration(
            connection, (Messages,), tmp_path, 'default', identity='identity',
            parsed=object(), review=proposal, approval=proposal.review_sha256,
            title='Narrow', backup_path=None, config_path='dummy')
    assert state['ddl'] == 0
