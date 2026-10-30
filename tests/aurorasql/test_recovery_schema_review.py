"""Conservative read-only verification of observed recovery schema."""

from aurora.aurorasql import (Model, column, connect, create_table,
                              initialize_history, list_history, prepare_create,
                              review_recovery_schema)


class ReviewProbe(Model):
    id = column(int, primary_key=True)
    title = column(str, length=60, unique=True)


def setup(db):
    initialize_history(db, confirm=True)
    approval = prepare_create(db, ReviewProbe)
    db.execute_write(
        'INSERT INTO "aurorasql_schema_history" '
        '(operation_id, table_name, dialect, fingerprint, status) '
        'VALUES (:id, :name, :dialect, :fingerprint, :status)',
        {"id": "review-probe", "name": "review_probe", "dialect": "sqlite",
         "fingerprint": approval.fingerprint, "status": "uncertain"},
    )


def test_matching_columns_do_not_mutate_history():
    with connect("sqlite:///:memory:") as db:
        setup(db)
        create_table(db, ReviewProbe)
        assert review_recovery_schema(db, "review-probe", ReviewProbe) == ()
        assert list_history(db)[0].status == "uncertain"


def test_missing_table_is_reported_without_changes():
    with connect("sqlite:///:memory:") as db:
        setup(db)
        findings = review_recovery_schema(db, "review-probe", ReviewProbe)
        assert [finding.kind for finding in findings] == ["table_missing"]
        assert list_history(db)[0].status == "uncertain"


def test_type_and_unique_mismatches_are_reported():
    with connect("sqlite:///:memory:") as db:
        setup(db)
        db.execute_write('CREATE TABLE "review_probe" (id INTEGER PRIMARY KEY, title INTEGER)')
        findings = review_recovery_schema(db, "review-probe", ReviewProbe)
        assert {finding.kind for finding in findings} >= {"type_mismatch", "unique_missing"}
        assert list_history(db)[0].status == "uncertain"
