"""Backup manifests stay authoritative; SQLite catalog is a guarded derived index."""

import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.backup_artifacts import list_artifacts
from aurora.database.sqlite_backup_catalog import reconcile_sqlite_catalog


def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO example VALUES (7)")
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": {
        "data": {"engine": "sqlite", "extension": "sqlite3"}
    }}), encoding="utf-8")
    (tmp_path / "aurora_config.json").write_text(json.dumps({
        "production": False, "default_database": "data"
    }), encoding="utf-8")


def test_catalog_created_after_backup_and_read_only_history(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "history", "--backups"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='__backups__'").fetchone() is None
    assert main(["db", "backup", "--title", "Acceptance"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        rows = db.execute('SELECT id,title,artifact,date,warnings FROM "__backups__"').fetchall()
        assert len(rows) == 1 and rows[0][1] == "Acceptance"
        assert rows[0][0] == 1 and rows[0][2].endswith(".sqlite3") and rows[0][3]
        assert db.execute('SELECT id FROM example').fetchone() == (7,)
    assert main(["db", "history", "--backups"]) == 0
    assert len(list_artifacts(tmp_path, "data")) == 1


def test_older_manifest_adopted_only_on_next_explicit_backup(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute('DROP TABLE "__backups__"')  # Simulates pre-catalog release.
    assert main(["db", "history", "--backups"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='__backups__'").fetchone() is None
    assert main(["db", "backup"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (2,)


def test_tampered_catalog_refused_without_mutating_other_rows(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute('UPDATE "__backups__" SET title=?', ("tampered",))
    assert main(["db", "history", "--backups"]) == 3
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        before = db.execute('SELECT * FROM "__backups__"').fetchall()
    from aurora.database.backup_artifacts import destination_fingerprint
    identity = destination_fingerprint('sqlite:///data.sqlite3', tmp_path)
    with pytest.raises(ValueError, match="conflicts"):
        reconcile_sqlite_catalog(tmp_path / "data.sqlite3", tmp_path, "data", identity)
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT * FROM "__backups__"').fetchall() == before


def test_unknown_existing_catalog_table_is_never_overwritten(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute('CREATE TABLE "__backups__" (different TEXT)')
        db.execute('INSERT INTO "__backups__" VALUES ("important")')
    with pytest.raises(SystemExit) as halted:
        main(["db", "backup"])
    assert halted.value.code == 1
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT different FROM "__backups__"').fetchone() == ("important",)


def test_invalid_manifest_not_adopted_into_catalog(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    (next((tmp_path / "__backups__" / "data").glob("*.sqlite3"))).write_bytes(b"corrupt")
    with pytest.raises(SystemExit) as halted:
        main(["db", "backup"])
    assert halted.value.code == 1
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (1,)


def test_catalog_does_not_break_existing_forward_history(tmp_path, monkeypatch):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "__init__.py").write_text("")
    (model_dir / "Item.py").write_text(
        "from aurora.database import Model, column\n"
        "class Item(Model):\n    id = column(int, primary_key=True)\n")
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": {
        "data": {"engine": "sqlite", "extension": "sqlite3"}
    }}))
    (tmp_path / "aurora_config.json").write_text(json.dumps({
        "production": False, "default_database": "data"
    }))
    (tmp_path / "aurora_models.json").write_text(json.dumps({"databases": {
        "data": ["models.Item:Item"]
    }}))
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init", "--confirm", "yes"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute('INSERT INTO item(id) VALUES(12)')
    snapshot = next((tmp_path / "__migrations__" / "data").glob("*.json"))
    unchanged = snapshot.read_bytes()
    assert main(["db", "backup"]) == 0
    assert main(["db", "check"]) == 0
    assert main(["db", "history"]) == 0
    assert main(["db", "init"]) == 0
    assert snapshot.read_bytes() == unchanged
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT id FROM item').fetchall() == [(12,)]
        assert db.execute('SELECT count(*) FROM "__migrations__"').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (1,)


def test_verified_manifest_missing_from_catalog_is_reconciled_on_explicit_backup(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute('DELETE FROM "__backups__"')  # Index is stale; manifests remain valid.
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (0,)
    # Read-only listing reports verified artifacts but never repairs its index.
    assert main(["db", "history", "--backups"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (0,)
    assert main(["db", "backup"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (2,)
        assert db.execute('SELECT id FROM example').fetchone() == (7,)


def test_five_column_catalog_matches_approved_history(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup", "--title", "Before schema change"]) == 0
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info("__backups__")')]
        assert columns == ["id", "title", "artifact", "date", "warnings"]
        ident, title, artifact, created, warnings = db.execute(
            'SELECT id,title,artifact,date,warnings FROM "__backups__"').fetchone()
        assert ident == 1 and title == "Before schema change"
        assert artifact.endswith(".sqlite3") and created and warnings is None


def test_old_catalog_is_readable_but_not_implicitly_rewritten(tmp_path, monkeypatch):
    import hashlib
    from aurora.database.backup_artifacts import destination_fingerprint
    from aurora.database.sqlite_backup_catalog import _catalog_format
    _project(tmp_path, monkeypatch)
    assert main(["db", "backup"]) == 0
    folder = tmp_path / "__backups__" / "data"
    manifest_path = next(folder.glob("*.json"))
    item = json.loads(manifest_path.read_text())
    # Recreate the former manifest format to exercise a genuine legacy catalog.
    item["id"] = manifest_path.stem
    item["completed_at"] = item.pop("date")
    item["verification"] = "artifact_checked"
    manifest_path.write_text(json.dumps(item, sort_keys=True) + "\n")
    raw = manifest_path.read_bytes()
    identity = destination_fingerprint("sqlite:///data.sqlite3", tmp_path)
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        db.execute('DROP TABLE "__backups__"')
        db.execute('CREATE TABLE "__backups__" ('
                   '"id" TEXT PRIMARY KEY, "title" TEXT, "completed_at" TEXT NOT NULL, '
                   '"backend" TEXT NOT NULL, "artifact" TEXT NOT NULL, '
                   '"artifact_sha256" TEXT NOT NULL, "destination_sha256" TEXT NOT NULL, '
                   '"manifest_sha256" TEXT NOT NULL)')
        db.execute('INSERT INTO "__backups__" VALUES (?,?,?,?,?,?,?,?)', (
            item["id"], item["title"], item["completed_at"], item["backend"],
            item["artifact"], item["sha256"], identity, hashlib.sha256(raw).hexdigest()))
        assert _catalog_format(db) == "legacy"
    assert main(["db", "history", "--backups"]) == 0
    with pytest.raises(SystemExit) as stopped:
        main(["db", "backup"])
    assert stopped.value.code == 1
    with sqlite3.connect(tmp_path / "data.sqlite3") as db:
        assert _catalog_format(db) == "legacy"
        assert db.execute('SELECT count(*) FROM "__backups__"').fetchone() == (1,)
    assert len(list(folder.glob("*.sqlite3"))) == 1
    assert len(list(folder.glob("*.json"))) == 1
