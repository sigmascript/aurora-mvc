"""Generated database selection follows the approved project configuration."""

import json

import pytest

from aurora._cli_scaffolding import create_project
from aurora.cli import main
from aurora.database.cli_plan import _load_config, resolve_project_database


@pytest.mark.parametrize("layout", ["mvc", "modular"])
def test_generated_project_selects_named_database_and_registers_models(tmp_path, monkeypatch, layout):
    root = create_project("sample", directory=tmp_path,
                          mvc=layout == "mvc", modular=layout == "modular")
    settings = json.loads((root / "aurora_config.json").read_text())
    databases = json.loads((root / "aurora_databases.json").read_text())
    assert settings["default_database"] == "data"
    assert databases == {"databases": {"data": {"engine": "sqlite", "extension": "sqlite3"}}}
    config, _ = _load_config(root / "aurora_databases.json")
    assert resolve_project_database(config, root) == "data"
    monkeypatch.chdir(root)
    assert main(["model", "add", "User"]) == 0
    manifest = json.loads((root / "aurora_models.json").read_text())
    assert manifest == {"databases": {"data": ["models.User:User"]}}
    assert not (root / "data.sqlite3").exists()
    assert not (root / "__migrations__").exists()


def test_minimal_project_has_no_database_selection(tmp_path):
    root = create_project("sample", directory=tmp_path)
    settings = json.loads((root / "aurora_config.json").read_text())
    assert "default_database" not in settings
    assert not (root / "aurora_databases.json").exists()


def test_existing_named_legacy_project_is_never_rewritten(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    existing = {"databases": {"default": {"engine": "sqlite", "database": "data.sqlite3"}}}
    (root / "aurora_databases.json").write_text(json.dumps(existing))
    settings_path = root / "aurora_config.json"
    settings = json.loads(settings_path.read_text())
    settings.pop("default_database")
    settings_path.write_text(json.dumps(settings))
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as denied:
        main(["model", "add", "User"])
    assert denied.value.code == 1
    assert json.loads((root / "aurora_databases.json").read_text()) == existing
