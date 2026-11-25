"""One configured name owns SQLite physical file and forward evidence."""

import json
import sqlite3

import pytest

from aurora.cli import main
from aurora.database.snapshots import sqlite_history_name, sqlite_snapshot_directory


def _project(root):
    (root / "models").mkdir()
    (root / "models" / "__init__.py").write_text("")
    (root / "models" / "Account.py").write_text(
        "from aurora.database import Model, column\n"
        "class Account(Model):\n    id = column(int, primary_key=True)\n"
    )
    (root / "aurora_config.json").write_text(
        json.dumps({"production": False, "default_database": "accounts"})
    )
    (root / "aurora_databases.json").write_text(
        json.dumps({"databases": {"accounts": {"engine": "sqlite", "extension": "sqlite3"}}})
    )
    (root / "aurora_models.json").write_text(
        json.dumps({"databases": {"accounts": ["models.Account:Account"]}})
    )


def test_fresh_snapshots_and_physical_file_share_one_configured_name(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    db_file = tmp_path / "accounts.sqlite3"
    assert sqlite_history_name(tmp_path, db_file) == "accounts"
    assert sqlite_snapshot_directory(tmp_path, db_file) == tmp_path / "__migrations__" / "accounts"
    assert main(["db", "init", "--confirm", "yes"]) == 0
    assert main(["db", "check"]) == 0
    with sqlite3.connect(db_file) as db:
        assert db.execute('SELECT count(*) FROM "__migrations__"').fetchone() == (1,)
        snapshot_name = db.execute('SELECT snapshot FROM "__migrations__"').fetchone()[0]
    assert (
        json.loads((tmp_path / "__migrations__" / "accounts" / snapshot_name).read_text())[
            "database"
        ]
        == "accounts"
    )
    assert main(["db", "init"]) == 0


def test_legacy_named_snapshot_namespace_is_not_relocated(tmp_path):
    _project(tmp_path)
    old = tmp_path / "migrations" / "accounts"
    old.mkdir(parents=True)
    marker = old / "20261117-120000.json"
    marker.write_text("existing snapshot evidence")
    assert sqlite_snapshot_directory(tmp_path, tmp_path / "accounts.sqlite3") == old
    assert marker.read_text() == "existing snapshot evidence"
    assert not (tmp_path / "__migrations__").exists()


def test_conflicting_old_and_new_snapshot_directories_refuse(tmp_path):
    _project(tmp_path)
    (tmp_path / "migrations" / "accounts").mkdir(parents=True)
    (tmp_path / "__migrations__" / "accounts").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="manual history reconciliation"):
        sqlite_snapshot_directory(tmp_path, tmp_path / "accounts.sqlite3")
