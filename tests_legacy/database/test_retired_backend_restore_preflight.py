"""Fail-closed, non-destructive SQLite restore preflight contracts."""

from contextlib import closing
import json
import sqlite3

import pytest

from aurora.database.cli_restore_preflight import check_sqlite_restore


def _setup(tmp_path):
    target = tmp_path / "target.sqlite3"
    backup = tmp_path / "backup.sqlite3"

    for path in (target, backup):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE data (value TEXT)")
            conn.execute("INSERT INTO data VALUES ('preserve')")
            conn.commit()

    config = tmp_path / "databases.json"
    config.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///target.sqlite3"}}}))
    return config, target, backup


def test_preflight_is_non_destructive_and_never_authorizes(tmp_path):
    config, target, backup = _setup(tmp_path)
    original = (target.read_bytes(), backup.read_bytes())
    result = check_sqlite_restore(config, "default", backup)
    assert result["status"] == "PREFLIGHT_ONLY"

    assert result["restore_authorized"] is False

    assert (target.read_bytes(), backup.read_bytes()) == original


def test_corrupt_backup_rejected_without_target_changes(tmp_path):
    config, target, backup = _setup(tmp_path)
    before = target.read_bytes()
    backup.write_bytes(b"not a database")

    with pytest.raises(sqlite3.DatabaseError):
        check_sqlite_restore(config, "default", backup)

    assert target.read_bytes() == before


def test_same_target_rejected(tmp_path):
    config, target, _ = _setup(tmp_path)

    with pytest.raises(ValueError, match="differ"):
        check_sqlite_restore(config, "default", target)


def test_unknown_alias_rejected(tmp_path):
    config, _, backup = _setup(tmp_path)

    with pytest.raises(ValueError, match="Unknown database alias"):
        check_sqlite_restore(config, "missing", backup)


def test_wal_sidecar_rejected(tmp_path):
    config, target, backup = _setup(tmp_path)
    (tmp_path / "target.sqlite3-wal").write_bytes(b"sidecar")

    with pytest.raises(ValueError, match="sidecar"):
        check_sqlite_restore(config, "default", backup)


def test_symlink_backup_rejected(tmp_path):
    config, _, backup = _setup(tmp_path)
    link = tmp_path / "linked.sqlite3"

    try:
        link.symlink_to(backup)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")

    with pytest.raises(ValueError, match="symbolic"):
        check_sqlite_restore(config, "default", link)


def test_backup_wal_sidecar_rejected_before_read(tmp_path):
    config, target, backup = _setup(tmp_path)
    original = target.read_bytes()
    (tmp_path / "backup.sqlite3-wal").write_bytes(b"uncheckpointed")

    with pytest.raises(ValueError, match="backup.*sidecar"):
        check_sqlite_restore(config, "default", backup)

    assert target.read_bytes() == original


def test_backup_journal_sidecar_rejected_before_read(tmp_path):
    config, target, backup = _setup(tmp_path)
    (tmp_path / "backup.sqlite3-journal").write_bytes(b"uncommitted")

    with pytest.raises(ValueError, match="backup.*sidecar"):
        check_sqlite_restore(config, "default", backup)
