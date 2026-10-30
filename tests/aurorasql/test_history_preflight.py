"""Fail-closed recorded creation checks; no concurrency claims."""

import pytest

from aurora.aurorasql import (
    Model, column, connect, initialize_history, prepare_create,
    execute_create_recorded, list_history,
)


class Target(Model):
    id = column(int, primary_key=True)


class Other(Model):
    id = column(int, primary_key=True)


def _insert(db, operation_id, table, dialect, status):
    db.execute_write(
        'INSERT INTO aurorasql_schema_history '
        '(operation_id, table_name, dialect, fingerprint, status) '
        'VALUES (:id, :table, :dialect, :fingerprint, :status)',
        {'id': operation_id, 'table': table, 'dialect': dialect,
         'fingerprint': 'placeholder', 'status': status},
    )


@pytest.mark.parametrize('status', ['started', 'uncertain', 'invalid'])
def test_inconsistent_unrelated_history_blocks_create(status):
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        _insert(db, 'one', 'other', 'sqlite', status)
        approval = prepare_create(db, Target)
        with pytest.raises(RuntimeError, match='history'):
            execute_create_recorded(db, Target, approval, confirm=True)
        assert len(list_history(db)) == 1
        assert not db.fetch_first("SELECT name FROM sqlite_master WHERE name='target'")


def test_missing_previously_applied_table_cannot_be_recreated():
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        _insert(db, 'one', 'target', 'sqlite', 'applied')
        approval = prepare_create(db, Target)
        with pytest.raises(RuntimeError, match='history'):
            execute_create_recorded(db, Target, approval, confirm=True)
        assert len(list_history(db)) == 1


def test_clean_history_still_allows_recorded_create():
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        approval = prepare_create(db, Target)
        operation_id = execute_create_recorded(db, Target, approval, confirm=True)
        assert len(operation_id) == 36
        assert list_history(db)[0].status == 'applied'
