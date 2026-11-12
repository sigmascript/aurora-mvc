"""Conventional shared/app model discovery with alias-specific assignments."""

import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from aurora.database.model_discovery import discover_model_refs
from aurora.database.cli_plan import _select_models
from aurora._cli_scaffolding import create_project
from aurora.cli import main


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
def test_project_model_discovery_keeps_existing_layouts(tmp_path, layout):
    project = create_project("sample", directory=tmp_path,
                          mvc=layout != "minimal", modular=layout == "modular")
    shared = project / "models"
    shared.mkdir(exist_ok=True)
    (shared / "Customer.py").write_text(
        "from aurora.database import Model, column\n"
        "class Customer(Model):\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    expected = ["models.Customer:Customer"]

    if layout == "modular":
        local = project / "apps" / "blog" / "models"
        local.mkdir()
        (local / "Article.py").write_text(
            "from aurora.database import Model, column\n"
            "class Article(Model):\n"
            "    id = column(int, primary_key=True)\n", encoding="utf-8")
        expected.insert(0, "apps.blog.models.Article:Article")

    refs = discover_model_refs(project, project / "aurora_models.json")
    assert sorted(refs) == sorted(expected)


def test_manual_shared_and_app_models_use_explicit_database_assignments(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "Customer.py").write_text(
        "from aurora.database import Model, column\n"
        "class Customer(Model):\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    (tmp_path / "models" / "Invoice.py").write_text(
        "from aurora.database import Model, column\n"
        "class Invoice(Model):\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {
        "accounting": ["models.Invoice:Invoice"]}}), encoding="utf-8")

    assert discover_model_refs(tmp_path, manifest, "default") == ("models.Customer:Customer",)
    assert discover_model_refs(tmp_path, manifest, "accounting") == ("models.Invoice:Invoice",)


def test_simple_cli_initialization_discovers_shared_models_without_model_flag(
        tmp_path, monkeypatch, capsys):
    project = create_project("sample", directory=tmp_path, mvc=True)
    (project / "models" / "Customer.py").write_text(
        "from aurora.database import Model, column\n"
        "class Customer(Model):\n"
        "    __table__ = 'customers'\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    sqlite3.connect(project / "data.sqlite3").close()
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    for name in list(sys.modules):
        if name == "models" or name.startswith("models."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(("y", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.delenv("AURORA_ENV", raising=False)

    assert main(["db", "init"]) == 0
    capsys.readouterr()
    assert main(["db", "plan"]) == 0
    capsys.readouterr()

    with sqlite3.connect(project / "data.sqlite3") as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='customers'"
        ).fetchone() == ("customers",)


def test_named_database_init_selects_assigned_models_only(tmp_path, monkeypatch, capsys):
    project = create_project("billing", directory=tmp_path, mvc=True)
    (project / "models" / "Customer.py").write_text(
        "from aurora.database import Model, column\n"
        "class Customer(Model):\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    (project / "models" / "Invoice.py").write_text(
        "from aurora.database import Model, column\n"
        "class Invoice(Model):\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    (project / "aurora_models.json").write_text(json.dumps({"databases": {
        "accounting": ["models.Invoice:Invoice"]}}), encoding="utf-8")
    (project / "aurora_databases.json").write_text(json.dumps({"databases": {
        "default": {"url": "sqlite:///data.sqlite3"},
        "accounting": {"url": "sqlite:///accounting.sqlite3"},
    }}), encoding="utf-8")
    sqlite3.connect(project / "accounting.sqlite3").close()
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    for name in list(sys.modules):
        if name == "models" or name.startswith("models."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(("y", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.delenv("AURORA_ENV", raising=False)

    assert main(["db", "init", "--database", "accounting"]) == 0
    capsys.readouterr()

    with sqlite3.connect(project / "accounting.sqlite3") as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "invoice" in names
        assert "customer" not in names


def test_new_project_init_creates_sqlite_file_only_after_approval(tmp_path, monkeypatch,
                                                                  capsys):
    project = create_project("fresh", directory=tmp_path, mvc=True)
    (project / "models" / "Customer.py").write_text(
        "from aurora.database import Model, column\n"
        "class Customer(Model):\n"
        "    id = column(int, primary_key=True)\n", encoding="utf-8")
    db_file = project / "data.sqlite3"
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))

    for name in list(sys.modules):
        if name == "models" or name.startswith("models."):
            monkeypatch.delitem(sys.modules, name)

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["db", "init"]) == 0
    assert not db_file.exists()
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["db", "init", "--title", "Create fresh CMS"]) == 0
    assert db_file.is_file()
    capsys.readouterr()

    with sqlite3.connect(db_file) as connection:
        title, snapshot = connection.execute(
            "SELECT title, snapshot FROM migrations WHERE id=1"
        ).fetchone()
        assert title == "Create fresh CMS"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='aurorasql_migration_timeline'"
        ).fetchone() is None
    snapshot_path = project / "migrations" / "data" / snapshot
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["title"] == title
