"""Read-only history consistency reporting for create-only experiments."""

from aurora.aurorasql import (Model, column, connect, initialize_history,
                              prepare_create, execute_create_recorded, review_history)


class ReviewItem(Model):
    id = column(int, primary_key=True)
    title = column(str, length=40)


def _record(db, identifier, name, dialect, status):
    db.execute_write(
        'INSERT INTO aurorasql_schema_history '
        '(operation_id, table_name, dialect, fingerprint, status) '
        'VALUES (:id, :name, :dialect, :fingerprint, :status)',
        {'id': identifier, 'name': name, 'dialect': dialect,
         'fingerprint': 'f', 'status': status},
    )


def test_clean_applied_history_has_no_findings():
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        approval = prepare_create(db, ReviewItem)
        execute_create_recorded(db, ReviewItem, approval, confirm=True)
        assert review_history(db) == ()


def test_uncertain_history_is_read_only_even_when_table_exists():
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        _record(db, 'one', 'review_item', 'sqlite', 'uncertain')
        before = db.fetch_all('SELECT * FROM aurorasql_schema_history')
        findings = review_history(db)
        assert {f.kind for f in findings} == {'unresolved'}
        assert db.fetch_all('SELECT * FROM aurorasql_schema_history') == before
        assert not db.fetch_first("SELECT name FROM sqlite_master WHERE name='review_item'")


def test_conflicts_and_missing_applied_tables_reported():
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        _record(db, 'one', 'review_item', 'mysql', 'applied')
        _record(db, 'two', 'review_item', 'sqlite', 'started')
        kinds = {f.kind for f in review_history(db)}
        assert {'dialect_mismatch', 'missing_applied_table', 'unresolved',
                'multiple_operations'} <= kinds


def test_unknown_status_is_not_silently_accepted():
    with connect('sqlite:///:memory:') as db:
        initialize_history(db, confirm=True)
        _record(db, 'one', 'review_item', 'sqlite', 'mystery')
        assert 'invalid_status' in {f.kind for f in review_history(db)}
