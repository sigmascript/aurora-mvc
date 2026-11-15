"""Read-only SQLite forward-history integrity safeguards."""

import json
import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.sqlite_forward_history import initialize, read_history


class Entry(Model):
    __table__ = "entry"
    id = column(int, primary_key=True)
    label = column(str)


def _initial(tmp_path):
    db_file = tmp_path / "app.sqlite3"
    with sqlite3.connect(db_file, isolation_level=None) as raw:
        snapshot = initialize(raw, tmp_path, db_file, (Entry,), "Initial")
    return db_file, snapshot


@pytest.mark.parametrize("invalid", [
    [], None, {"models": [None]}, {"physical_schema": [None]},
    {"models": [{"name": "entry"}, {"name": "entry"}]},
])
def test_malformed_snapshot_is_refused_without_database_writes(tmp_path, invalid):
    db_file, path = _initial(tmp_path)
    if isinstance(invalid, dict):
        document = json.loads(path.read_text(encoding="utf-8"))
        document.update(invalid)
    else:
        document = invalid
    path.write_text(json.dumps(document), encoding="utf-8")
    with sqlite3.connect(db_file, isolation_level=None) as raw:
        before = raw.total_changes
        with pytest.raises(RuntimeError, match="snapshot is invalid"):
            read_history(raw, tmp_path, db_file)
        assert raw.total_changes == before
        assert raw.execute("SELECT count(*) FROM migrations").fetchone() == (1,)


def test_history_cannot_reuse_snapshot_filename(tmp_path):
    db_file, path = _initial(tmp_path)
    with sqlite3.connect(db_file, isolation_level=None) as raw:
        raw.execute('INSERT INTO migrations (title,snapshot,date,warnings) '
                    'SELECT title,snapshot,date,warnings FROM migrations')
        before = raw.total_changes
        with pytest.raises(RuntimeError, match="reuses a snapshot"):
            read_history(raw, tmp_path, db_file)
        assert raw.total_changes == before
        assert raw.execute("SELECT count(*) FROM migrations").fetchone() == (2,)
        assert path.is_file()
