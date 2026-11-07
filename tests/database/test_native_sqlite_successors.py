"""Reviewed additive migrations extend native Initial in one guarded sequence."""

import sqlite3
import json
from argparse import Namespace

import pytest

from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.cli_history import run_history
from aurora.database.core import Connection
from aurora.database.migration_timeline import inspect_timeline
from aurora.database.review_artifacts import create_review
from aurora.database.snapshots import schema_snapshot
from aurora.database.sqlite_additive_migration import execute_sqlite_additive_migration


class InitialNote(Model):
    __module__ = "tests.database.test_native_sqlite_successors"
    __table__ = "native_notes"
    id = column(int, primary_key=True)
    title = column(str)


class ExpandedNote(Model):
    __module__ = "tests.database.test_native_sqlite_successors"
    __table__ = "native_notes"
    id = column(int, primary_key=True)
    title = column(str)
    status = column(str, nullable=True, default="draft")


class FurtherExpandedNote(Model):
    __table__ = "native_notes"
    id = column(int, primary_key=True)
    title = column(str)
    status = column(str, nullable=True, default="draft")
    description = column(str, nullable=True)


def _state(path):
    sqlite3.connect(path).close()

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (InitialNote,))

    review = create_review(
        schema_snapshot(InitialNote, dialect="sqlite"),
        schema_snapshot(ExpandedNote, dialect="sqlite"),
        migration_id="AddNoteStatus",
    )
    return review


def _apply(raw, review):
    return execute_sqlite_additive_migration(
        raw, review, (ExpandedNote,),
        reviewed_sha256=review["review_sha256"], confirm=True,
    )


def test_initial_then_reviewed_successor_has_one_linear_timeline(tmp_path):
    path = tmp_path / "native.sqlite3"
    review = _state(path)

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("INSERT INTO native_notes(title) VALUES ('saved')")
        _apply(raw, review)
        timeline = inspect_timeline(raw, "default")
        assert [entry["number"] for entry in timeline] == [1, 2]
        assert [entry["name"] for entry in timeline] == ["Initial", "AddNoteStatus"]
        assert all(entry["status"] == "applied" for entry in timeline)
        assert timeline[-1]["physical_history_consistent"] is True
        assert raw.execute("SELECT status FROM native_notes").fetchone()[0] == "draft"

        with pytest.raises(RuntimeError):
            _apply(raw, review)

        assert len(inspect_timeline(raw, "default")) == 2


def test_migration_titles_are_descriptive_not_unique_identifiers(tmp_path):
    from aurora.database.migration_titles import migration_title

    path = tmp_path / "titles.sqlite3"
    sqlite3.connect(path).close()

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (InitialNote,), title="Set up the CMS")

    review = create_review(
        schema_snapshot(InitialNote, dialect="sqlite"),
        schema_snapshot(ExpandedNote, dialect="sqlite"),
        migration_id="AddNoteStatus",
    )

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        execute_sqlite_additive_migration(
            raw, review, (ExpandedNote,), reviewed_sha256=review["review_sha256"],
            confirm=True, title="Set up the CMS",
        )
        timeline = inspect_timeline(raw, "default")
        assert [entry["number"] for entry in timeline] == [1, 2]
        assert [entry["title"] for entry in timeline] == ["Set up the CMS", "Set up the CMS"]
        assert [entry["name"] for entry in timeline] == ["Initial", "AddNoteStatus"]
        assert timeline[-1]["physical_history_consistent"] is True

    assert migration_title("", "Untitled") == "Untitled"
    assert migration_title("  Add user email  ", "Untitled") == "Add user email"

    with pytest.raises(ValueError):
        migration_title("\nunsafe", "Untitled")


def test_catalog_drift_prevents_native_successor(tmp_path):
    path = tmp_path / "drift.sqlite3"
    review = _state(path)

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("ALTER TABLE native_notes ADD COLUMN unreviewed TEXT")

        with pytest.raises(RuntimeError, match="catalog|history"):
            _apply(raw, review)

        assert len(inspect_timeline(raw, "default")) == 1
        assert inspect_timeline(raw, "default")[-1]["physical_history_consistent"] is False


def test_public_history_verifies_latest_model_after_successor(tmp_path, capsys):
    path = tmp_path / "public.sqlite3"
    review = _state(path)

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        _apply(raw, review)

    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///public.sqlite3"}}
    }), encoding="utf-8")
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["tests.database.test_native_sqlite_successors:ExpandedNote"]}
    }), encoding="utf-8")
    args = Namespace(config=str(tmp_path / "aurora_databases.json"), database="default",
                     models_config="aurora_models.json", model=[], verify_models=True,
                     format="json")

    assert run_history(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in report["timeline"]] == ["Initial", "AddNoteStatus"]
    assert report["timeline"][-1]["verified_current"] is True


def test_failed_postcondition_rolls_back_schema_and_successor_intents(tmp_path, monkeypatch):
    from aurora.database import sqlite_additive_migration

    path = tmp_path / "rollback.sqlite3"
    review = _state(path)

    def fail_verification(*args, **kwargs):
        raise RuntimeError("simulated postcondition failure")

    monkeypatch.setattr(sqlite_additive_migration, "_verify_result_catalog", fail_verification)

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")

        with pytest.raises(RuntimeError, match="postcondition"):
            _apply(raw, review)

        assert [row[1] for row in raw.execute('PRAGMA table_info("native_notes")')] == [
            "id", "title",
        ]
        assert raw.execute('SELECT count(*) FROM aurorasql_migration_intents_v2').fetchone()[0] == 0
        assert len(inspect_timeline(raw, "default")) == 1
        assert inspect_timeline(raw, "default")[-1]["physical_history_consistent"] is True


def test_unresolved_legacy_journal_blocks_successor(tmp_path):
    path = tmp_path / "legacy_uncertain.sqlite3"
    review = _state(path)

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("UPDATE aurorasql_schema_history SET status='uncertain'")

        with pytest.raises(RuntimeError, match="Native history"):
            _apply(raw, review)

        assert raw.execute('SELECT count(*) FROM aurorasql_migration_intents_v2').fetchone()[0] == 0


def test_third_migration_requires_intact_fingerprint_chain(tmp_path):
    path = tmp_path / "third.sqlite3"
    first_review = _state(path)
    second_review = create_review(
        schema_snapshot(ExpandedNote, dialect="sqlite"),
        schema_snapshot(FurtherExpandedNote, dialect="sqlite"),
        migration_id="AddNoteDescription",
    )

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        _apply(raw, first_review)
        execute_sqlite_additive_migration(
            raw, second_review, (FurtherExpandedNote,),
            reviewed_sha256=second_review["review_sha256"], confirm=True,
        )
        timeline = inspect_timeline(raw, "default")
        assert [entry["number"] for entry in timeline] == [1, 2, 3]
        assert timeline[-1]["physical_history_consistent"] is True

        raw.execute(
            "UPDATE aurorasql_migration_intents_v2 SET before_sha256=? "
            "WHERE migration_id='AddNoteDescription'", ("f" * 64,)
        )
        assert inspect_timeline(raw, "default")[-1]["physical_history_consistent"] is False


def test_missing_native_successor_journal_invalidates_initial(tmp_path):
    path = tmp_path / "missing_journal.sqlite3"
    _state(path)

    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute('DROP TABLE aurorasql_migration_intents_v2')
        report = inspect_timeline(raw, "default")
        assert report[-1]["physical_history_consistent"] is False
