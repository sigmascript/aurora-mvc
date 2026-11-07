"""A.1 ordinary SQLite column editing preserves records and linear history."""

import json
import sqlite3
from pathlib import Path

import pytest

from aurora.cli import main
from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.core import Connection
from aurora.database.migration_timeline import inspect_timeline


def _project(tmp_path, *, existing_null=False):
    path = tmp_path / "products.sqlite3"
    initial = type("Product", (Model,), {
        "__module__": "product_schema", "__table__": "products",
        "id": column(int, primary_key=True),
        "name": column(str, length=40, nullable=True, default="draft"),
    })

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (initial,))

    with sqlite3.connect(path) as raw:
        raw.execute("INSERT INTO products(id,name) VALUES (1,?)",
                    (None if existing_null else "Original",))

    (tmp_path / "product_schema.py").write_text(
        "from aurora.database import Model, column\n"
        "class Product(Model):\n"
        "    __table__ = 'products'\n"
        "    id = column(int, primary_key=True)\n"
        "    name = column(str, length=120, nullable=False, default='active')\n",
        encoding="utf-8",
    )
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["product_schema:Product"]},
    }), encoding="utf-8")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///products.sqlite3"}}}), encoding="utf-8")
    return path, config


def test_reviewed_sqlite_column_edits_preserve_existing_data(tmp_path, capsys,
                                                              monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("AURORA_ENV", raising=False)
    path, config = _project(tmp_path)
    preview = ["db", "migrate", "--config", str(config), "--dry-run", "--format", "json"]
    assert main(preview) == 0
    report = json.loads(capsys.readouterr().out)
    assert {change["kind"] for change in report["operations"]} == {
        "type_mismatch", "nullability_mismatch", "default_mismatch",
    }
    backup = tmp_path / "before.sqlite3"
    assert main(["db", "migrate", "--config", str(config), "--apply",
                 "--confirm", "APPLY", "--approve", report["review_fingerprint"],
                 "--backup", str(backup), "--title", "Update product name"]) == 0
    capsys.readouterr()

    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT name FROM products WHERE id=1").fetchone() == ("Original",)
        raw.execute("INSERT INTO products(id) VALUES (2)")
        assert raw.execute("SELECT name FROM products WHERE id=2").fetchone() == ("active",)
        declaration = next(row for row in raw.execute('PRAGMA table_info("products")')
                           if row[1] == "name")
        assert declaration[2].upper() == "VARCHAR(120)"
        assert declaration[3] == 1
        assert declaration[4] == "'active'"
        timeline = inspect_timeline(raw, "default")
        assert timeline[-1]["title"] == "Update product name"
        assert timeline[-1]["physical_history_consistent"] is True

    assert backup.is_file()

    from aurora.database.cli_history import run_history
    from types import SimpleNamespace
    args = SimpleNamespace(config=str(config), database="default",
                           models_config="aurora_models.json", verify_models=True,
                           format="json")
    assert run_history(args) == 0
    assert json.loads(capsys.readouterr().out)["timeline"][-1]["verified_current"] is True


def test_null_values_refuse_required_column_edit_without_backup(tmp_path, capsys,
                                                                monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    path, config = _project(tmp_path, existing_null=True)
    before = path.read_bytes()

    assert main(["db", "migrate", "--config", str(config), "--dry-run"]) == 0
    assert "advisory only" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_sqlite_column_edit_rejects_narrower_varchar_type(tmp_path):
    from aurora.database.sqlite_column_edit import sqlite_column_edit_proposal

    path = tmp_path / "narrow.sqlite3"
    original = type("Product", (Model,), {
        "__module__": "width_models", "__table__": "products",
        "id": column(int, primary_key=True),
        "name": column(str, length=200),
    })
    narrower = type("Product", (Model,), {
        "__module__": "width_models", "__table__": "products",
        "id": column(int, primary_key=True),
        "name": column(str, length=50),
    })

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (original,))

    with Connection(sqlite3.connect(path, isolation_level=None), "sqlite") as connection:
        with pytest.raises(ValueError, match="narrowing"):
            sqlite_column_edit_proposal(connection, (narrower,), "default", path)
