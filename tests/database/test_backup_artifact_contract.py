"""Backup artifacts have one durable, independently inspectable source of truth."""

import json
import sqlite3

from aurora.cli import main
from aurora.database.backup_catalog import list_artifacts


def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect(tmp_path / "data.sqlite3") as database:
        database.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
        database.execute("INSERT INTO example VALUES (7)")
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps({"databases": {"data": {"engine": "sqlite", "extension": "sqlite3"}}}),
        encoding="utf-8",
    )
    (tmp_path / "aurora_config.json").write_text(
        json.dumps({"production": False, "default_database": "data"}), encoding="utf-8"
    )


def test_managed_sqlite_backups_unique_immutable_and_read_only_history(
    tmp_path, monkeypatch, capsys
):
    _project(tmp_path, monkeypatch)
    assert not (tmp_path / "__backups__").exists()
    assert main(["db", "history", "--backups"]) == 0
    assert not (tmp_path / "__backups__").exists()
    for _ in range(2):
        assert main(["db", "backup", "--title", "Safety copy"]) == 0
    reports = list_artifacts(tmp_path, "data")
    assert len(reports) == 2
    assert len({r["id"] for r in reports}) == 2
    assert all(r["status"] == "ARTIFACT_VERIFIED" for r in reports)
    assert main(["db", "history", "--backups"]) == 0
    assert "Recorded backups: 2" in capsys.readouterr().out
    folder = tmp_path / "__backups__" / "data"
    for entry in reports:
        with sqlite3.connect(folder / (entry["id"] + ".sqlite3")) as conn:
            assert conn.execute("SELECT id FROM example").fetchone() == (7,)
    artifact = folder / (reports[0]["id"] + ".sqlite3")
    artifact.write_bytes(b"tampered")
    assert list_artifacts(tmp_path, "data")[0]["status"] == "MISSING_OR_CHANGED"
    assert main(["db", "history", "--backups"]) == 3


def test_backup_public_flags_and_restore_failure(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    from pytest import raises

    with raises(SystemExit) as stopped:
        main(["db", "backup", "--to", "other.sqlite3"])
    assert stopped.value.code == 2
    with raises(SystemExit) as stopped:
        main(["db", "restore", "--backup", "20261117T120000Z-0123456789abcdef01234567"])
    assert stopped.value.code == 1
    assert not (tmp_path / "__backups__").exists()
    assert "disabled" in capsys.readouterr().err.lower()


def test_read_only_history_flags_orphan_and_missing_artifact(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    folder = tmp_path / "__backups__" / "data"
    backup = next(folder.glob("*.sqlite3"))
    backup.unlink()
    assert main(["db", "history", "--backups"]) == 3
    orphan = folder / "unrecorded.sqlite3"
    orphan.write_bytes(b"x")
    assert any(row["status"] == "UNRECORDED_ARTIFACT" for row in list_artifacts(tmp_path, "data"))


def test_backup_history_refuses_rebound_destination(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    manifest = tmp_path / "aurora_databases.json"
    manifest.write_text(
        json.dumps({"databases": {"data": {"engine": "sqlite", "extension": "db"}}}),
        encoding="utf-8",
    )
    assert main(["db", "history", "--backups"]) == 3
    assert list((tmp_path / "__backups__" / "data").glob("*.sqlite3"))


def test_server_backup_dispatch_records_verified_artifacts_without_live_ddl(tmp_path, monkeypatch):
    import pytest

    from aurora.database import cli_backup

    monkeypatch.chdir(tmp_path)
    (tmp_path / "aurora_config.json").write_text(
        json.dumps({"production": False, "default_database": "warehouse"}), encoding="utf-8"
    )

    def fake_mysql(_, __, destination, ___, expected_url=None):
        from pathlib import Path

        output = Path(destination)
        output.write_bytes(b"-- Complete hypothetical SQL dump --")
        return output

    def fake_postgres(_, __, destination, expected_url=None):
        from pathlib import Path

        output = Path(destination)
        output.write_bytes(b"PGDMP" + b"X" * 96)
        return output

    monkeypatch.setattr(cli_backup, "backup_mysql_database", fake_mysql)
    monkeypatch.setattr(cli_backup, "backup_postgres_database", fake_postgres)
    # This dispatch test stubs BOTH the external dump and the server catalog;
    # it is not a live database acceptance test.
    from contextlib import contextmanager

    from aurora.database import backup_catalog
    from aurora.database.configuration import DatabaseConfiguration

    @contextmanager
    def fake_connect(self, alias):
        yield object()

    monkeypatch.setattr(DatabaseConfiguration, "connect", fake_connect)
    monkeypatch.setattr(backup_catalog, "_catalog_format", lambda connection: None)
    monkeypatch.setattr(
        backup_catalog,
        "inspect_server_catalog",
        lambda connection, root, alias, identity, reports: reports,
    )
    monkeypatch.setattr(
        backup_catalog, "reconcile_server_catalog", lambda connection, root, alias, identity: None
    )

    # Existing artifacts bind the configured name to one physical destination.
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps({"databases": {"warehouse": {"engine": "mysql", "username": "tester"}}}),
        encoding="utf-8",
    )
    assert main(["db", "backup", "--title", "mysql"]) == 0
    assert list_artifacts(tmp_path, "warehouse", None)[-1]["status"] == "ARTIFACT_VERIFIED"

    (tmp_path / "aurora_databases.json").write_text(
        json.dumps({"databases": {"warehouse": {"engine": "postgresql", "username": "tester"}}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as blocked:
        main(["db", "backup", "--title", "postgresql"])
    assert blocked.value.code == 1
    assert main(["db", "history", "--backups"]) == 3
    assert len(list_artifacts(tmp_path, "warehouse")) == 1


def test_invalid_title_does_not_create_artifacts(tmp_path, monkeypatch):
    import pytest

    _project(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        main(["db", "backup", "--title", "line\nbreak"])
    assert stopped.value.code == 1
    assert not (tmp_path / "__backups__").exists()


def test_explicit_backup_database_remains_isolated(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "second.sqlite3") as connection:
        connection.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps(
            {
                "databases": {
                    "data": {"engine": "sqlite", "extension": "sqlite3"},
                    "second": {"engine": "sqlite", "extension": "sqlite3"},
                }
            }
        ),
        encoding="utf-8",
    )
    assert main(["db", "backup", "--database", "second"]) == 0
    assert not (tmp_path / "__backups__" / "data").exists()
    assert len(list_artifacts(tmp_path, "second")) == 1
    assert main(["db", "history", "--backups", "--database", "data"]) == 0
    assert main(["db", "history", "--backups", "--database", "second"]) == 0


def test_timestamp_backup_names_and_collision_suffix_are_stable(tmp_path, monkeypatch):
    from aurora.database import backup_catalog as artifacts

    _project(tmp_path, monkeypatch)
    from datetime import datetime, timezone

    class FixedClock:
        @staticmethod
        def now(_):
            return datetime(2026, 11, 17, 20, 57, 13, tzinfo=timezone.utc)

    monkeypatch.setattr(artifacts, "datetime", FixedClock)
    first_id, first_file = artifacts.prepare_artifact(tmp_path, "data", "sqlite")
    assert __import__("re").fullmatch(r"\d{8}-\d{6}", first_id)
    first_file.write_bytes(b"reserved artifact")
    second_id, second_file = artifacts.prepare_artifact(tmp_path, "data", "sqlite")
    assert second_id == first_id + "-1"
    assert second_file != first_file
    assert first_file.read_bytes() == b"reserved artifact"


def test_new_artifact_filename_and_independent_manifest_match(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup", "--title", "Before update"]) == 0
    folder = tmp_path / "__backups__" / "data"
    manifest_path = next(folder.glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["artifact"] == manifest_path.stem + ".sqlite3"
    assert set(manifest) == {
        "database",
        "backend",
        "title",
        "artifact",
        "date",
        "bytes",
        "sha256",
        "destination_sha256",
    }
    assert (folder / manifest["artifact"]).is_file()
    assert len(manifest["sha256"]) == 64
    assert len(manifest["destination_sha256"]) == 64


def test_legacy_manifest_remains_readable_without_rewriting(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup", "--title", "Legacy readable"]) == 0
    folder = tmp_path / "__backups__" / "data"
    manifest_path = next(folder.glob("*.json"))
    item = json.loads(manifest_path.read_text())
    item["id"] = manifest_path.stem
    item["completed_at"] = item.pop("date")
    item["verification"] = "artifact_checked"
    manifest_path.write_text(json.dumps(item, sort_keys=True) + "\n")
    original = manifest_path.read_bytes()
    assert list_artifacts(tmp_path, "data")[0]["status"] == "ARTIFACT_VERIFIED"
    assert main(["db", "history", "--backups"]) == 0
    assert manifest_path.read_bytes() == original


def test_manifest_requires_date_and_integrity_evidence(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    folder = tmp_path / "__backups__" / "data"
    manifest_path = next(folder.glob("*.json"))
    item = json.loads(manifest_path.read_text())
    del item["date"]
    manifest_path.write_text(json.dumps(item) + "\n")
    assert list_artifacts(tmp_path, "data")[0]["status"] == "INVALID"
    assert main(["db", "history", "--backups"]) == 3
