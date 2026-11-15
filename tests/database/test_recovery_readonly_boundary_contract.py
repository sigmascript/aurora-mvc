"""Recovery inspection remains read-only; executable restore paths are retired."""

import importlib.util
import json
import subprocess

import pytest

from aurora.database import cli_mysql_restore_preflight as mysql_preflight


def _config_and_dump(tmp_path):
    config = tmp_path / "databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "mysql+pymysql://reviewer@localhost:3306/review_db"
    }}}), encoding="utf-8")
    dump = tmp_path / "backup.sql"
    dump.write_bytes(b"-- MySQL dump 10.13\nCREATE TABLE t (id int);\n-- Dump completed on 2026\n")
    return config, dump


@pytest.mark.parametrize("occupied", [False, True])
def test_mysql_preflight_only_issues_read_only_sql(tmp_path, monkeypatch, occupied):
    config, dump = _config_and_dump(tmp_path)
    original = dump.read_bytes()
    queries = []

    def fake_run(command, *, label):
        assert command[0] == "mysql"
        sql = command[-1]
        assert command[-2].startswith("--execute=") is False or sql.startswith("--execute=")
        assert sql.startswith("--execute=SELECT ")
        assert not any(word in sql.upper() for word in ("CREATE ", "DROP ", "INSERT ", "ALTER ", "UPDATE "))
        queries.append(sql)
        if "CURRENT_USER()" in sql:
            answer = "review_db\treviewer@localhost\n"
        elif "TABLES" in sql:
            answer = "2\n"
        else:
            answer = "1\n" if occupied else "0\n"
        return subprocess.CompletedProcess(command, 0, answer, "")

    monkeypatch.setattr(mysql_preflight, "_run", fake_run)
    report = mysql_preflight.check_mysql_restore(str(config), "default", str(dump), "profile")
    assert report.staging_available is not occupied
    assert report.table_count == 2
    assert len(queries) == 3
    assert dump.read_bytes() == original
    text = report.format_text()
    assert "READ ONLY" in text
    assert "staging imports, is disabled" in text
    assert "does not authorize" in text


def test_preflight_rejects_target_as_staging_without_connecting(tmp_path, monkeypatch):
    config, dump = _config_and_dump(tmp_path)
    monkeypatch.setattr(mysql_preflight, "_run", lambda *_, **__: pytest.fail("connected"))
    with pytest.raises(ValueError, match="staging"):
        mysql_preflight.check_mysql_restore(str(config), "default", str(dump), "profile", "review_db")


def test_preflight_rejects_unverified_backup_without_connecting(tmp_path, monkeypatch):
    config, dump = _config_and_dump(tmp_path)
    dump.write_bytes(b"CREATE DATABASE review_db;")
    monkeypatch.setattr(mysql_preflight, "_run", lambda *_, **__: pytest.fail("connected"))
    with pytest.raises(ValueError, match="completed"):
        mysql_preflight.check_mysql_restore(str(config), "default", str(dump), "profile")


def test_removed_staging_import_module_is_not_importable():
    assert importlib.util.find_spec("aurora.database.cli_mysql_staging_restore") is None


def test_public_restore_refuses_without_database_config(tmp_path, monkeypatch):
    from aurora.cli import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        main(["db", "restore", "--from", str(tmp_path / "missing.sql")])
    assert stopped.value.code == 1
    assert list(tmp_path.iterdir()) == []


def test_sqlite_backup_preserves_existing_destination(tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from aurora.database.cli_backup import run_backup

    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO items (id) VALUES (7)")
    config = tmp_path / "databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///source.sqlite3"
    }}}), encoding="utf-8")
    output = tmp_path / "existing.sqlite3"
    output.write_bytes(b"existing-backup-must-not-change")
    args = SimpleNamespace(to=str(output), config=str(config), database="default")
    with pytest.raises((FileExistsError, ValueError)):
        run_backup(args)
    assert output.read_bytes() == b"existing-backup-must-not-change"
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT id FROM items").fetchone() == (7,)


def test_sqlite_backup_rejects_invalid_source_without_leaving_output(tmp_path):
    from types import SimpleNamespace
    from aurora.database.cli_backup import run_backup

    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"not a sqlite database")
    config = tmp_path / "databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///source.sqlite3"
    }}}), encoding="utf-8")
    output = tmp_path / "backup.sqlite3"
    args = SimpleNamespace(to=str(output), config=str(config), database="default")
    with pytest.raises(Exception):
        run_backup(args)
    assert source.read_bytes() == b"not a sqlite database"
    assert not output.exists()
