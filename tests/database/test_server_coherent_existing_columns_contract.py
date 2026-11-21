"""Server F08: one safe table transition, never one DDL per column."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database.models import Model, column
from aurora.database.planning import SchemaChange, SchemaPlan
from aurora.database import server_model_review as reviewer
from aurora.database import server_forward_migration as executor


class Messages(Model):
    __table__ = 'server_coherent_messages'
    id = column(int, primary_key=True)
    title = column(str, length=250, not_null=False)
    visits = column(int, default=7)
    note = column(str, length=80, not_null=False)


@pytest.fixture
def observed(monkeypatch):
    state = {'kinds': [('type_mismatch', 'title'), ('default_mismatch', 'visits'),
                       ('nullability_mismatch', 'note')],
             'type': 'VARCHAR(100)', 'nullable': True,
             'visits_default': None, 'note_nullable': False, 'null_row': None,
             'warnings': (), 'index': None}

    def planning(connection, model):
        return SchemaPlan(model.__table_name__, True, tuple(
            SchemaChange(kind, name, 'change') for kind, name in state['kinds']),
            state['warnings'])

    def catalog(connection, name):
        idx = state['index'] or ('PRIMARY' if connection.dialect == 'mysql'
                                  else 'server_coherent_messages_pkey')
        cols = [SimpleNamespace(name='id', sql_type='INTEGER', nullable=False,
                                primary_key=True, default_sql=None),
                SimpleNamespace(name='title', sql_type=state['type'], nullable=state['nullable'],
                                primary_key=False, default_sql=None),
                SimpleNamespace(name='visits', sql_type='BIGINT', nullable=False,
                                primary_key=False, default_sql=state['visits_default']),
                SimpleNamespace(name='note', sql_type='VARCHAR(80)',
                                nullable=state['note_nullable'], primary_key=False,
                                default_sql=None)]
        return SimpleNamespace(exists=True, uniqueness_verified=True,
                               constraints_verified=True, indexes=(idx,),
                               unique_columns=(), columns=cols)

    monkeypatch.setattr(reviewer, 'plan_schema', planning)
    monkeypatch.setattr(reviewer, 'inspect_table', catalog)
    return state


@pytest.mark.parametrize('dialect,token', [('mysql', '`'), ('postgresql', '"')])
def test_coherent_widen_default_and_nullability_in_one_statement(observed, dialect, token):
    con = SimpleNamespace(dialect=dialect, fetch_first=lambda *args: None)
    proposal = reviewer.review_server_model_changes(con, (Messages,), 'default', 'identity')
    assert proposal.operations[0]['kind'] == 'alter_columns'
    assert len(proposal.operations) == len(proposal.statements) == 1
    sql = proposal.statements[0]
    assert sql.startswith(f'ALTER TABLE {token}server_coherent_messages{token} ')
    if dialect == 'mysql':
        assert sql.count('MODIFY COLUMN') == 3
        assert '`title` VARCHAR(250) NULL' in sql
        assert '`visits` BIGINT NOT NULL DEFAULT 7' in sql
        assert '`note` VARCHAR(80) NULL' in sql
    else:
        assert 'ALTER COLUMN "title" TYPE VARCHAR(250)' in sql
        assert 'ALTER COLUMN "visits" SET DEFAULT 7' in sql
        assert 'ALTER COLUMN "note" DROP NOT NULL' in sql
    assert proposal.review_sha256 == reviewer.review_server_model_changes(
        con, (Messages,), 'default', 'identity').review_sha256
    observed['type'] = 'VARCHAR(120)'
    assert proposal.review_sha256 != reviewer.review_server_model_changes(
        con, (Messages,), 'default', 'identity').review_sha256


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_add_new_column_and_modify_existing_together(observed, monkeypatch, dialect):
    class NewMessages(Model):
        __table__ = 'server_coherent_messages'
        id = column(int, primary_key=True)
        title = column(str, length=250, not_null=False)
        visits = column(int, default=7)
        note = column(str, length=80, not_null=False)
        score = column(int, default=5)

    observed['kinds'] = [('type_mismatch', 'title'), ('missing_column', 'score')]
    con = SimpleNamespace(dialect=dialect, fetch_first=lambda *args: None)
    proposal = reviewer.review_server_model_changes(con, (NewMessages,), 'default', 'identity')
    assert proposal.operations[0]['additions'] == ('score',)
    assert proposal.statements[0].count('ADD COLUMN') == 1
    assert 'DEFAULT 5 NOT NULL' in proposal.statements[0]
    assert len(proposal.statements) == 1


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_nullability_tightening_checks_actual_rows(observed, dialect):
    class RequiredMessages(Model):
        __table__ = 'server_coherent_messages'
        id = column(int, primary_key=True)
        title = column(str, length=250, not_null=False)
        visits = column(int, default=7)
        note = column(str, length=80, not_null=True)

    observed['kinds'] = [('nullability_mismatch', 'note')]
    observed['note_nullable'] = True
    queries = []
    def read(sql):
        queries.append(sql)
        return observed['null_row']
    con = SimpleNamespace(dialect=dialect, fetch_first=read)
    observed['null_row'] = {'found': 1}
    with pytest.raises(ValueError, match='Existing NULL values prevent NOT NULL'):
        reviewer.review_server_model_changes(con, (RequiredMessages,), 'default', 'identity')
    assert len(queries) == 1 and 'IS NULL LIMIT 1' in queries[0]
    observed['null_row'] = None
    proposal = reviewer.review_server_model_changes(con, (RequiredMessages,), 'default', 'identity')
    assert proposal.operations[0]['kind'] == 'alter_columns'
    assert ('SET NOT NULL' if dialect == 'postgresql' else 'NOT NULL') in proposal.statements[0]


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_varchar_narrowing_requires_actual_value_fit(observed, dialect):
    observed['kinds'] = [('type_mismatch', 'title')]
    observed['type'] = 'VARCHAR(500)'
    max_seen = {'length': 250}
    queries = []
    def fetch(sql):
        queries.append(sql)
        return {'max_length': max_seen['length']}
    connection = SimpleNamespace(dialect=dialect, fetch_first=fetch)
    proposal = reviewer.review_server_model_changes(connection, (Messages,), 'default', 'identity')
    assert proposal.operations[0]['kind'] == 'alter_columns'
    assert 'VARCHAR(250)' in proposal.statements[0]
    assert 'MAX(CHAR_LENGTH' in queries[0]
    max_seen['length'] = 251
    with pytest.raises(ValueError, match='existing value length 251 exceeds'):
        reviewer.review_server_model_changes(connection, (Messages,), 'default', 'identity')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_existing_default_expression_not_silently_replaced(observed, dialect):
    observed['kinds'] = [('default_mismatch', 'visits')]
    observed['visits_default'] = 'CURRENT_TIMESTAMP'
    with pytest.raises(ValueError, match='not a verified literal'):
        reviewer.review_server_model_changes(SimpleNamespace(dialect=dialect),
                                             (Messages,), 'default', 'identity')

@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_unverified_indexes_prevent_schema_change(observed, dialect):
    observed['index'] = 'some_custom_index'
    with pytest.raises(ValueError, match='Indexed source table'):
        reviewer.review_server_model_changes(SimpleNamespace(dialect=dialect),
                                             (Messages,), 'default', 'identity')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_approval_is_rechecked_inside_lock_before_ddl(observed, monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init
    conn = SimpleNamespace(dialect=dialect, fetch_first=lambda *_: None)
    proposal = reviewer.review_server_model_changes(conn, (Messages,), 'default', 'identity')
    events = []

    class Connection:
        _transaction_active = False
        _migration_lock_active = False
        def __init__(self):
            self.dialect = dialect
        @contextmanager
        def transaction(self):
            events.append('begin')
            yield
        def fetch_first(self, *args):
            return {'acquired': True}
        def execute_write(self, *args):
            events.append('DDL')

    @contextmanager
    def mysql_lock(connection):
        events.append('lock')
        yield

    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(executor, 'read_history', lambda *_: ([{'id': 1}], {}))
    monkeypatch.setattr(executor, 'history_table', lambda *_: '__migrations__')
    monkeypatch.setattr(executor, 'migration_lock', mysql_lock)
    observed['type'] = 'VARCHAR(125)'
    monkeypatch.setattr(executor, 'review_server_model_changes', reviewer.review_server_model_changes)
    with pytest.raises(RuntimeError, match='proposal changed after review'):
        executor.execute_server_forward_migration(
            Connection(), (Messages,), tmp_path, 'default', identity='identity',
            parsed=object(), review=proposal, approval=proposal.review_sha256,
            title='Review combined change', backup_path=None, config_path='dummy')
    assert events.count('DDL') == 0

@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_verified_forward_execution_one_ddl_and_one_history(monkeypatch, tmp_path, observed, dialect):
    from aurora.database import cli_init
    con = SimpleNamespace(dialect=dialect, fetch_first=lambda *_: None)
    proposal = reviewer.review_server_model_changes(con, (Messages,), 'default', 'identity')
    events = []
    snapshot = tmp_path / 'migration.json'
    snapshot.write_text('{}', encoding='utf8')
    expected_row = {'id': 2, 'title': 'Combine safe edits',
                    'snapshot': snapshot.name, 'date': None, 'warnings': None}
    state = {'written': False}

    class Connection:
        _transaction_active = False
        _migration_lock_active = False
        def __init__(self):
            self.dialect = dialect
        @contextmanager
        def transaction(self):
            events.append('transaction')
            yield
        def fetch_first(self, *args):
            return {'acquired': True}
        def execute_write(self, sql, params=None):
            events.append('ddl' if sql.startswith('ALTER TABLE') else 'history')
            if params:
                expected_row.update(params)
                state['written'] = True

    @contextmanager
    def mysql_lock(connection):
        events.append('lock')
        yield

    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(executor, 'read_history', lambda *_: (
        ([{'id': 1}, dict(expected_row)] if state['written'] else [{'id': 1}]), {}))
    monkeypatch.setattr(executor, 'history_table', lambda *_: '__migrations__')
    monkeypatch.setattr(executor, 'migration_lock', mysql_lock)
    monkeypatch.setattr(executor, 'verify_server_declared_schema', lambda *_ , **kw: events.append('verified'))
    monkeypatch.setattr(executor, '_write_snapshot', lambda *_: snapshot)
    monkeypatch.setattr(executor, 'review_server_model_changes', reviewer.review_server_model_changes)
    result, backup = executor.execute_server_forward_migration(
        Connection(), (Messages,), tmp_path, 'default', identity='identity',
        parsed=object(), review=proposal, approval=proposal.review_sha256,
        title='Combine safe edits', backup_path=None, config_path='dummy')
    assert result == snapshot and backup is None
    assert events.count('ddl') == 1 and events.count('history') == 1
    assert events.index('ddl') < events.index('verified') < events.index('history')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_required_column_addition_checks_empty_table(observed, dialect):
    class RequiredMessages(Model):
        __table__ = 'server_coherent_messages'
        id = column(int, primary_key=True)
        title = column(str, length=250, not_null=False)
        visits = column(int, default=7)
        note = column(str, length=80, not_null=False)
        category = column(str, length=40, not_null=True)

    observed['kinds'] = [('missing_column', 'category')]
    rows = {'existing': None}
    queries = []

    def fetch(sql):
        queries.append(sql)
        return rows['existing']

    connection = SimpleNamespace(dialect=dialect, fetch_first=fetch)
    proposal = reviewer.review_server_model_changes(
        connection, (RequiredMessages,), 'default', 'identity'
    )

    assert proposal.operations[0]['kind'] == 'add_column'
    assert 'ADD COLUMN' in proposal.statements[0]
    assert 'NOT NULL' in proposal.statements[0]
    assert 'DEFAULT' not in proposal.statements[0]
    assert len(queries) == 1
    assert 'SELECT 1 AS present FROM' in queries[0]
    assert 'LIMIT 1' in queries[0]

    rows['existing'] = {'present': 1}

    with pytest.raises(ValueError, match='requires an empty existing table'):
        reviewer.review_server_model_changes(connection, (RequiredMessages,), 'default', 'identity')


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_required_addition_with_other_safe_columns_still_checks_rows(observed, dialect):
    class RequiredMessages(Model):
        __table__ = 'server_coherent_messages'
        id = column(int, primary_key=True)
        title = column(str, length=250, not_null=False)
        visits = column(int, default=7)
        note = column(str, length=80, not_null=False)
        category = column(str, length=40, not_null=True)

    observed['kinds'] = [('type_mismatch', 'title'), ('missing_column', 'category')]
    connection = SimpleNamespace(dialect=dialect, fetch_first=lambda sql: None)
    proposal = reviewer.review_server_model_changes(
        connection, (RequiredMessages,), 'default', 'identity'
    )

    assert proposal.operations[0]['kind'] == 'alter_columns'
    assert proposal.operations[0]['additions'] == ('category',)
    assert 'NOT NULL' in proposal.statements[0]


@pytest.mark.parametrize('dialect', ['mysql', 'postgresql'])
def test_required_addition_refuses_new_rows_during_locked_review(
        observed, monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init

    class RequiredMessages(Model):
        __table__ = 'server_coherent_messages'
        id = column(int, primary_key=True)
        title = column(str, length=250, not_null=False)
        visits = column(int, default=7)
        note = column(str, length=80, not_null=False)
        category = column(str, length=40, not_null=True)

    observed['kinds'] = [('missing_column', 'category')]
    occupied = {'value': False}
    events = []

    class Connection:
        _transaction_active = False
        _migration_lock_active = False

        def __init__(self):
            self.dialect = dialect

        def fetch_first(self, sql, parameters=None):
            if 'pg_try_advisory_xact_lock' in sql:
                return {'acquired': True}

            return {'present': 1} if occupied['value'] else None

        @contextmanager
        def transaction(self):
            yield

        def execute_write(self, sql, parameters=None):
            events.append(sql)

    @contextmanager
    def locked(connection):
        yield

    connection = Connection()
    proposal = reviewer.review_server_model_changes(
        connection, (RequiredMessages,), 'default', 'identity'
    )
    occupied['value'] = True

    monkeypatch.setattr(cli_init, '_mysql_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(cli_init, '_postgres_database_identity', lambda *_: 'identity')
    monkeypatch.setattr(executor, 'read_history', lambda *_: ([{'id': 1}], {}))
    monkeypatch.setattr(executor, 'history_table', lambda *_: '__migrations__')
    monkeypatch.setattr(executor, 'migration_lock', locked)

    with pytest.raises(ValueError, match='requires an empty existing table'):
        executor.execute_server_forward_migration(
            connection, (RequiredMessages,), tmp_path, 'default',
            identity='identity', parsed=object(), review=proposal,
            approval=proposal.review_sha256, title='Add required field',
            backup_path=None, config_path='dummy'
        )

    assert events == []
