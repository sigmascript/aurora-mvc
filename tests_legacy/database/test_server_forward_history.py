"""Read-only forward history evidence: never manufacture missing snapshots."""

import json

import pytest

from aurora.database import server_forward_history as history


class Observed:
    dialect = "mysql"

    def __init__(self, rows):
        self.rows = rows

    def fetch_all(self, query):
        if "information_schema.TABLES" in query:
            return [{"name": "migrations", "kind": "BASE TABLE"},
                    {"name": "note", "kind": "BASE TABLE"}]
        if "information_schema.COLUMNS" in query:
            return [{"name": item} for item in history._COLUMNS]
        if "SELECT id,title" in query:
            return self.rows
        raise AssertionError("Unexpected query: " + query)


def test_directory_refuses_unsafe_alias_and_symlink(tmp_path):
    assert history._directory(tmp_path, "accounting") == tmp_path / "migrations/accounting"
    for alias in ("../danger", ".", "bad/name", ""):
        with pytest.raises(ValueError):
            history._directory(tmp_path, alias)
    (tmp_path / "migrations").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError):
        history._directory(tmp_path, "accounting")


def _history_table(monkeypatch):
    from aurora.database.inspection import ColumnInfo, TableInfo
    columns = tuple(ColumnInfo(name, "bigint" if name == "id" else "text",
                               name == "warnings", name == "id")
                    for name in history._COLUMNS)
    monkeypatch.setattr(history, "inspect_table", lambda connection, name:
                        TableInfo(name, True, columns))


def test_server_history_rejects_missing_snapshot(tmp_path, monkeypatch):
    _history_table(monkeypatch)
    row = {"id": 1, "title": "Initial", "snapshot": "20261112-123000.json",
           "date": "2026-11-12T12:30:00+00:00", "warnings": None}
    with pytest.raises(RuntimeError, match="missing"):
        history.read_history(Observed([row]), tmp_path, "accounting")


def test_server_history_rejects_gaps_and_forged_warnings(tmp_path, monkeypatch):
    _history_table(monkeypatch)
    folder = tmp_path / "migrations/accounting"
    folder.mkdir(parents=True)
    (folder / "20261112-123000.json").write_text(json.dumps({
        "database": "accounting", "engine": "mysql", "title": "Initial",
        "models": [], "physical_schema": []}), encoding="utf-8")
    row = {"id": 2, "title": "Initial", "snapshot": "20261112-123000.json",
           "date": "2026-11-12T12:30:00+00:00", "warnings": None}
    with pytest.raises(RuntimeError, match="incomplete"):
        history.read_history(Observed([row]), tmp_path, "accounting")
    row["id"] = 1
    row["warnings"] = '["yes", 4]'
    with pytest.raises(RuntimeError, match="warnings"):
        history.read_history(Observed([row]), tmp_path, "accounting")


def test_server_history_refuses_mixed_legacy():
    class Legacy(Observed):
        def fetch_all(self, query):
            if "information_schema.TABLES" in query:
                return [{"name": "migrations", "kind": "BASE TABLE"},
                        {"name": "aurorasql_schema_history", "kind": "BASE TABLE"}]
            return super().fetch_all(query)

    with pytest.raises(RuntimeError, match="mixed"):
        history.read_history(Legacy([]), ".", "accounting")


def test_server_snapshot_verified_against_physical_catalog(tmp_path, monkeypatch):
    _history_table(monkeypatch)
    folder = tmp_path / "migrations/accounting"
    folder.mkdir(parents=True)
    document = {"database": "accounting", "engine": "mysql", "title": "Initial",
                "models": [{"name": "note"}], "physical_schema": [{"table": "note"}]}
    (folder / "20261112-123000.json").write_text(json.dumps(document), encoding="utf-8")
    row = {"id": 1, "title": "Initial", "snapshot": "20261112-123000.json",
           "date": "2026-11-12T12:30:00+00:00", "warnings": None}
    monkeypatch.setattr(history, "_physical", lambda connection: [{"table": "note"}])
    records, latest = history.read_history(Observed([row]), tmp_path, "accounting")
    assert len(records) == 1 and latest == document
    monkeypatch.setattr(history, "_physical", lambda connection: [{"table": "unexpected"}])
    with pytest.raises(RuntimeError, match="physical schema differs"):
        history.read_history(Observed([row]), tmp_path, "accounting")


def test_server_history_refuses_reused_snapshot_filename(tmp_path, monkeypatch):
    _history_table(monkeypatch)
    folder = tmp_path / "migrations/accounting"
    folder.mkdir(parents=True)
    name = "20261112-123000.json"
    (folder / name).write_text(json.dumps({
        "database": "accounting", "engine": "mysql", "title": "Initial",
        "models": [], "physical_schema": [],
    }), encoding="utf-8")
    rows = [
        {"id": number, "title": "Initial", "snapshot": name,
         "date": "2026-11-12T12:30:00+00:00", "warnings": None}
        for number in (1, 2)
    ]

    with pytest.raises(RuntimeError, match="reuses a snapshot"):
        history.read_history(Observed(rows), tmp_path, "accounting")


@pytest.mark.parametrize("document", [
    [],
    None,
    {"database": "accounting", "engine": "mysql", "title": "Initial",
     "models": [None], "physical_schema": []},
    {"database": "accounting", "engine": "mysql", "title": "Initial",
     "models": [{"name": "note"}], "physical_schema": []},
    {"database": "accounting", "engine": "mysql", "title": "Initial",
     "models": [], "physical_schema": [None]},
])
def test_server_history_refuses_malformed_snapshot_structure(tmp_path, monkeypatch, document):
    """Malformed JSON values must refuse migration, not escape as attribute errors."""
    _history_table(monkeypatch)
    folder = tmp_path / "migrations/accounting"
    folder.mkdir(parents=True)
    (folder / "20261112-123000.json").write_text(
        json.dumps(document), encoding="utf-8")
    row = {"id": 1, "title": "Initial", "snapshot": "20261112-123000.json",
           "date": "2026-11-12T12:30:00+00:00", "warnings": None}

    with pytest.raises(RuntimeError, match="snapshot invalid"):
        history.read_history(Observed([row]), tmp_path, "accounting")


@pytest.mark.parametrize("column,nullable,primary_key", [
    ("id", True, True),
    ("title", True, False),
    ("snapshot", True, False),
    ("date", True, False),
    ("warnings", False, False),
    ("warnings", True, True),
])
def test_server_history_refuses_invalid_column_constraints(
        monkeypatch, column, nullable, primary_key):
    """Correct column names cannot excuse nullable evidence or extra keys."""
    from aurora.database.inspection import ColumnInfo, TableInfo

    columns = tuple(
        ColumnInfo(name, "bigint" if name == "id" else "text",
                   nullable if name == column else name == "warnings",
                   primary_key if name == column else name == "id")
        for name in history._COLUMNS
    )
    monkeypatch.setattr(history, "inspect_table", lambda connection, name:
                        TableInfo(name, True, columns))

    with pytest.raises(RuntimeError, match="column constraints"):
        history._verify_table(Observed([]))


@pytest.mark.parametrize("duplicate", ["models", "physical_schema"])
def test_server_history_rejects_duplicate_table_identity(tmp_path, monkeypatch, duplicate):
    _history_table(monkeypatch)
    folder = tmp_path / "migrations/accounting"
    folder.mkdir(parents=True)
    document = {
        "database": "accounting",
        "engine": "mysql",
        "title": "Initial",
        "models": [{"name": "note"}],
        "physical_schema": [{"table": "note"}],
    }
    document[duplicate].append(document[duplicate][0].copy())
    (folder / "20261112-123000.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    record = {
        "id": 1,
        "title": "Initial",
        "snapshot": "20261112-123000.json",
        "date": "2026-11-12T12:30:00+00:00",
        "warnings": None,
    }
    monkeypatch.setattr(history, "_physical", lambda connection: [{"table": "note"}])

    with pytest.raises(RuntimeError, match="snapshot invalid"):
        history.read_history(Observed([record]), tmp_path, "accounting")
