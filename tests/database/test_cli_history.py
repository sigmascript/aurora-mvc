"""Read-only journal CLI integration and fail-closed behavior."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def _run(tmp_path, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "aurora", *args], cwd=tmp_path,
                          env=env, text=True, capture_output=True, timeout=15)


def _config(tmp_path, url):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": url}}}))


def test_missing_file_is_not_created(tmp_path):
    _config(tmp_path, "sqlite:///missing.sqlite3")
    result = _run(tmp_path, "db", "history")
    assert result.returncode != 0

    assert not (tmp_path / "missing.sqlite3").exists()


def test_missing_journal_is_not_initialized(tmp_path):
    db = tmp_path / "app.sqlite3"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    _config(tmp_path, "sqlite:///app.sqlite3")
    result = _run(tmp_path, "db", "history")
    assert result.returncode != 0

    assert db.read_bytes() == before


def test_history_read_only_and_unresolved_findings(tmp_path):
    db = tmp_path / "app.sqlite3"

    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE aurorasql_schema_history (operation_id VARCHAR(36) PRIMARY KEY, table_name VARCHAR(128) NOT NULL, dialect VARCHAR(16) NOT NULL, fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)")
        connection.execute("INSERT INTO aurorasql_schema_history VALUES ('op1','widgets','sqlite','abc','uncertain')")

    before = db.read_bytes()
    _config(tmp_path, "sqlite:///app.sqlite3")
    result = _run(tmp_path, "db", "history", "--format", "json")
    assert result.returncode == 3, result.stderr

    report = json.loads(result.stdout)
    assert report["entries"][0]["status"] == "uncertain"

    assert any(f["kind"] == "unresolved" for f in report["findings"])

    assert db.read_bytes() == before


def test_empty_journal_reports_success_without_mutation(tmp_path):
    db = tmp_path / "app.sqlite3"

    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE aurorasql_schema_history (operation_id VARCHAR(36) PRIMARY KEY, table_name VARCHAR(128) NOT NULL, dialect VARCHAR(16) NOT NULL, fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)")

    before = db.read_bytes()
    _config(tmp_path, "sqlite:///app.sqlite3")
    result = _run(tmp_path, "db", "history", "--format", "json")
    assert result.returncode == 0, result.stderr

    assert json.loads(result.stdout)["entries"] == []

    assert db.read_bytes() == before


def test_legacy_history_file_rejected_on_new_backend(tmp_path):
    result = _run(tmp_path, "db", "history", "--history-file", "x")
    assert result.returncode != 0

    assert "legacy" in result.stderr


def test_history_report_reads_journal_once(tmp_path, monkeypatch):
    """A CLI report must review the same snapshot it displays."""
    from aurora.database import cli_history
    from aurora.database.history import HistoryEntry, HistoryFinding

    entry = HistoryEntry("op1", "widgets", "sqlite", "fingerprint", "uncertain")
    calls = []

    def fake_list(connection):
        calls.append("read")
        return (entry,)

    def fake_review(connection, *, entries=None):
        calls.append("review")
        assert entries == (entry,)

        return (HistoryFinding("op1", "widgets", "unresolved", "Review required"),)

    monkeypatch.setattr(cli_history, "list_history", fake_list)
    monkeypatch.setattr(cli_history, "review_history", fake_review)
    report = cli_history._read_history(object(), "default")
    assert calls == ["read", "review"]

    assert report["entries"][0]["operation_id"] == "op1"

    assert report["findings"][0]["kind"] == "unresolved"


def test_history_review_accepts_supplied_entries_without_journal_read(monkeypatch):
    from aurora.database import history

    monkeypatch.setattr(history, "list_history", lambda connection: (_ for _ in ()).throw(
        AssertionError("unexpected second journal query")))
    assert history.review_history(object(), entries=()) == ()


def test_review_history_only_exempts_one_verified_successor_table(monkeypatch):
    from aurora.database import history

    monkeypatch.setattr(history, "inspect_table", lambda *_: SimpleNamespace(exists=True))
    connection = SimpleNamespace(dialect="postgresql")
    entries = (
        history.HistoryEntry("initial", "notes", "postgresql", "a" * 64, "applied"),
        history.HistoryEntry("successor", "notes", "postgresql", "b" * 64, "applied"),
        history.HistoryEntry("third", "other", "postgresql", "c" * 64, "applied"),
    )

    assert len([finding for finding in history.review_history(connection, entries=entries)
                if finding.kind == "multiple_operations"]) == 2
    assert history.review_history(connection, entries=entries,
                                  verified_successor_table="notes") == ()

    unresolved = entries[:1] + (
        history.HistoryEntry("successor", "notes", "postgresql", "b" * 64,
                             "uncertain"),
    )
    assert any(finding.kind == "unresolved" for finding in history.review_history(
        connection, entries=unresolved, verified_successor_table="notes"))

    too_many = entries[:2] + (
        history.HistoryEntry("third", "notes", "postgresql", "c" * 64, "applied"),
    )
    assert len([finding for finding in history.review_history(
        connection, entries=too_many, verified_successor_table="notes")
                if finding.kind == "multiple_operations"]) == 3

    duplicate_id = (
        entries[0],
        history.HistoryEntry("initial", "notes", "postgresql", "b" * 64,
                             "applied"),
    )
    assert any(finding.kind == "duplicate_operation_id" for finding in
               history.review_history(connection, entries=duplicate_id,
                                      verified_successor_table="notes"))


def test_server_history_suppresses_duplicate_only_after_current_verification(monkeypatch):
    from aurora.database import cli_history, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    entries = (
        HistoryEntry("initial", "notes", "postgresql", "a" * 64, "applied"),
        HistoryEntry("successor", "notes", "postgresql", "b" * 64, "applied"),
    )
    monkeypatch.setattr(cli_history, "list_history", lambda _: entries)
    monkeypatch.setattr(cli_history, "inspect_native_history", lambda *_: None)
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                             "a" * 64, {"models.Note:Note": "notes"}, "b" * 64,
                             "applied"),
        ServerMigrationEntry(2, "Migration2", "Widen", "accounting",
                             "c" * 64, {"models.Note:Note": "notes"}, "d" * 64,
                             "applied", operation={"kind": "widen_column",
                                                   "table": "notes", "column": "label",
                                                   "from": "varchar(80)", "to": "varchar(160)"}),
    ))
    from aurora.database import history
    monkeypatch.setattr(history, "inspect_table", lambda *_: SimpleNamespace(exists=True))
    monkeypatch.setattr(cli_history, "review_history", history.review_history)
    connection = SimpleNamespace(dialect="postgresql")

    monkeypatch.setattr(server_migration_timeline, "verify_server_successor_current",
                        lambda *_: True)
    report = cli_history._read_history(connection, "accounting", models=[object()])
    assert report["findings"] == []
    assert report["timeline"][-1]["verified_current"]

    monkeypatch.setattr(server_migration_timeline, "verify_server_successor_current",
                        lambda *_: False)
    report = cli_history._read_history(connection, "accounting", models=[object()])
    assert len([finding for finding in report["findings"]
                if finding["kind"] == "multiple_operations"]) == 2
    assert any(finding["kind"] == "server_history_not_verified"
               for finding in report["findings"])


def test_named_server_history_verification_passes_discovered_models(monkeypatch, tmp_path,
                                                                    capsys):
    from contextlib import contextmanager

    from aurora.database import cli_history

    selected_models = [object()]
    connection = SimpleNamespace(dialect="postgresql")

    class Configuration:
        def resolve(self, alias):
            assert alias == "accounting"
            return "postgresql://localhost/accounting"

        @contextmanager
        def connect(self, alias):
            assert alias == "accounting"
            yield connection

    monkeypatch.setattr(cli_history, "_load_config", lambda _: (Configuration(), tmp_path))
    monkeypatch.setattr(cli_history, "_select_models",
                        lambda args, base_dir: (["app.models:Invoice"], selected_models))

    def read_history(selected_connection, alias, *, models=None):
        assert selected_connection is connection
        assert alias == "accounting"
        assert models is selected_models
        return {"entries": [], "findings": [], "timeline": []}

    monkeypatch.setattr(cli_history, "_read_history", read_history)
    args = SimpleNamespace(config="ignored", database="accounting", verify_models=True,
                           format="json")
    assert cli_history.run_history(args) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == []
