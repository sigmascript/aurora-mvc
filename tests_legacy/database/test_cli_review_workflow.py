"""Safe CLI review export and migration journal approval gates."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from aurora.database import Model, column, schema_snapshot
from aurora.database.cli_review_workflow import run_review_export
from aurora.database.review_artifacts import create_review, load_review


class Original(Model):
    __table__ = "review_inventory"
    id = column(int, primary_key=True)


class Updated(Model):
    __table__ = "review_inventory"
    id = column(int, primary_key=True)
    label = column(str, nullable=True)


def setup(tmp_path, *, journal=True, unresolved=False):
    db = tmp_path / "app.sqlite3"
    connection = sqlite3.connect(db)

    try:
        if journal:
            connection.execute("CREATE TABLE aurorasql_schema_history (operation_id VARCHAR(36) PRIMARY KEY, table_name VARCHAR(128) NOT NULL, dialect VARCHAR(16) NOT NULL, fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)")

            if unresolved:
                connection.execute("INSERT INTO aurorasql_schema_history VALUES ('op1','review_inventory','sqlite','x','uncertain')")

        connection.commit()
    finally:
        connection.close()

    config = tmp_path / "db.json"
    config.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///app.sqlite3"}}}), encoding="utf-8")
    before = schema_snapshot(Original, dialect="sqlite")
    after = schema_snapshot(Updated, dialect="sqlite")
    a, b = tmp_path / "before.json", tmp_path / "after.json"
    a.write_text(json.dumps(before), encoding="utf-8")
    b.write_text(json.dumps(after), encoding="utf-8")
    return db, SimpleNamespace(config=str(config), database="default", review_before=str(a),
                               review_after=str(b), review_id="inventory_001",
                               review_out=str(tmp_path / "review.json"), review_ack=None), before, after


def test_export_is_deterministic_read_only_and_non_executable(tmp_path, capsys):
    db, args, before, after = setup(tmp_path)
    original = db.read_bytes()
    expected = create_review(before, after, migration_id=args.review_id)
    args.review_ack = expected["review_sha256"]
    assert run_review_export(args) == 0

    assert load_review(args.review_out) == expected

    assert db.read_bytes() == original

    assert json.loads(capsys.readouterr().out)["execution_allowed"] is False

    with pytest.raises(FileExistsError):
        run_review_export(args)


@pytest.mark.parametrize("journal,unresolved", [(False, False), (True, True)])
def test_missing_or_unresolved_journal_fails_closed(tmp_path, journal, unresolved):
    db, args, _, _ = setup(tmp_path, journal=journal, unresolved=unresolved)
    original = db.read_bytes()

    with pytest.raises((RuntimeError, ValueError)):
        run_review_export(args)

    assert not (tmp_path / "review.json").exists()

    assert db.read_bytes() == original


def test_wrong_ack_and_wrong_dialect_refused(tmp_path):
    db, args, _, _ = setup(tmp_path)
    original = db.read_bytes()
    args.review_ack = "0" * 64

    with pytest.raises(PermissionError):
        run_review_export(args)

    args.review_ack = None
    after = json.loads((tmp_path / "after.json").read_text(encoding="utf-8"))
    after["dialect"] = "mysql"
    (tmp_path / "after.json").write_text(json.dumps(after), encoding="utf-8")

    with pytest.raises(ValueError):
        run_review_export(args)

    assert db.read_bytes() == original

    assert not (tmp_path / "review.json").exists()


def test_partial_arguments_refused_without_writes(tmp_path):
    db, args, _, _ = setup(tmp_path)
    args.review_after = None

    with pytest.raises(ValueError):
        run_review_export(args)

    assert not (tmp_path / "review.json").exists()
