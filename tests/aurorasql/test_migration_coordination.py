"""Focused lock ownership and fail-closed behavior checks."""

import pytest

from aurora.aurorasql import (
    Model, column, connect, execute_create_recorded, initialize_history,
    inspect_table, list_history, prepare_create,
)
from aurora.aurorasql.coordination import migration_lock


class Coordinated(Model):
    id = column(int, primary_key=True)


def test_sqlite_competing_connection_refused_without_journal_mutation(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'coordination.db').replace('\\', '/')
    with connect(url) as first, connect(url) as second:
        initialize_history(first, confirm=True)
        approval = prepare_create(first, Coordinated)
        second.fetch_all('PRAGMA busy_timeout = 20')
        with migration_lock(first):
            with pytest.raises(Exception, match='locked'):
                execute_create_recorded(second, Coordinated, approval, confirm=True)
            assert not list_history(first)
            assert not inspect_table(first, 'coordinated').exists
        execute_create_recorded(second, Coordinated, approval, confirm=True)
        assert list_history(first)[0].status == 'applied'


def test_sqlite_nested_coordination_rejected_without_leaking_lock():
    with connect('sqlite:///:memory:') as db:
        with migration_lock(db):
            with pytest.raises(RuntimeError, match='nested'):
                with migration_lock(db):
                    pass
        with migration_lock(db):
            pass


def test_sqlite_failed_ddl_keeps_uncertain_history(monkeypatch, tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'uncertain.db').replace('\\', '/')
    with connect(url) as db:
        initialize_history(db, confirm=True)
        approval = prepare_create(db, Coordinated)
        def fail(*args, **kwargs):
            raise OSError('simulated lost response')
        monkeypatch.setattr('aurora.aurorasql.history.execute_create', fail)
        with pytest.raises(OSError, match='simulated'):
            execute_create_recorded(db, Coordinated, approval, confirm=True)
    with connect(url) as db:
        assert list_history(db)[0].status == 'uncertain'
        assert not inspect_table(db, 'coordinated').exists


@pytest.mark.parametrize('dialect,acquire,released', [
    ('mysql', {'acquired': 0}, {'released': 1}),
    ('postgresql', {'acquired': False}, {'released': True}),
])
def test_backend_lock_refusal_fails_closed(dialect, acquire, released):
    class FakeConnection:
        def __init__(self):
            self.dialect = dialect
            self._transaction_active = False
            self._migration_lock_active = False
            self.calls = []
        def _ensure_open(self):
            pass
        def fetch_first(self, sql, params):
            self.calls.append(sql)
            return acquire
    db = FakeConnection()
    with pytest.raises(RuntimeError, match='unavailable'):
        with migration_lock(db):
            pytest.fail('Must not enter critical section')
    assert len(db.calls) == 1
    assert not db._migration_lock_active


@pytest.mark.parametrize('dialect,acquire,released', [
    ('mysql', {'acquired': 1}, {'released': 1}),
    ('postgresql', {'acquired': True}, {'released': True}),
])
def test_backend_lock_released_after_failure(dialect, acquire, released):
    class FakeConnection:
        def __init__(self):
            self.dialect = dialect
            self._transaction_active = False
            self._migration_lock_active = False
            self.calls = []
        def _ensure_open(self):
            pass
        def fetch_first(self, sql, params):
            self.calls.append(sql)
            return acquire if len(self.calls) == 1 else released
    db = FakeConnection()
    with pytest.raises(ValueError, match='operation failed'):
        with migration_lock(db):
            raise ValueError('operation failed')
    assert len(db.calls) == 2
    assert not db._migration_lock_active
