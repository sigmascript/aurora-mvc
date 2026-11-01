"""Read-only journal CLI integration and fail-closed behavior."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


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
