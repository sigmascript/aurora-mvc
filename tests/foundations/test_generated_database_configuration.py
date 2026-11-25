"""Generated database selection follows the approved project configuration."""

import json

import pytest

from aurora._cli_scaffolding import create_project
from aurora.cli import main
from aurora.database.cli_check import _load_config, resolve_project_database


@pytest.mark.parametrize("layout", ["mvc", "modular"])
def test_generated_project_selects_named_database_and_registers_models(
    tmp_path, monkeypatch, layout
):
    root = create_project(
        "sample", directory=tmp_path, mvc=layout == "mvc", modular=layout == "modular"
    )
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


@pytest.mark.parametrize(
    "engine, entry",
    [
        ("sqlite", {"engine": "sqlite", "extension": "db"}),
        (
            "mysql",
            {
                "engine": "mysql",
                "host": "localhost",
                "username": "example",
                "password": "",
                "port": 3306,
            },
        ),
        (
            "postgresql",
            {
                "engine": "postgresql",
                "host": "localhost",
                "username": "example",
                "password": "",
                "port": 5432,
            },
        ),
    ],
)
def test_explicit_model_database_selection(tmp_path, monkeypatch, engine, entry):
    root = create_project("sample", directory=tmp_path, mvc=True)
    databases = json.loads((root / "aurora_databases.json").read_text())
    databases["databases"]["accounting"] = entry
    (root / "aurora_databases.json").write_text(json.dumps(databases))
    manifest_path = root / "aurora_models.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["databases"]["accounting"] = []
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.chdir(root)
    assert main(["model", "add", "Invoice", "--database", "accounting"]) == 0
    assert json.loads(manifest_path.read_text())["databases"] == {
        "accounting": ["models.Invoice:Invoice"]
    }
    assert (root / "models" / "Invoice.py").exists()
    assert not (root / "accounting.db").exists()
    assert not (root / "__migrations__").exists()


def test_invalid_explicit_database_has_no_source_or_manifest_effect(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    monkeypatch.chdir(root)
    before = (root / "aurora_models.json").read_bytes()
    with pytest.raises(SystemExit) as denied:
        main(["model", "add", "Invoice", "--database", "missing"])
    assert denied.value.code == 1
    assert (root / "aurora_models.json").read_bytes() == before
    assert not (root / "models" / "Invoice.py").exists()


def test_explicit_database_selection_modular(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, modular=True)
    database_path = root / "aurora_databases.json"
    config = json.loads(database_path.read_text())
    config["databases"]["accounting"] = {"engine": "sqlite", "extension": "sqlite3"}
    database_path.write_text(json.dumps(config))
    manifest_path = root / "aurora_models.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["databases"]["accounting"] = []
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.chdir(root)
    assert main(["model", "add", "Invoice", "--app", "home", "--database", "accounting"]) == 0
    assert (
        "apps.home.models.Invoice:Invoice"
        in json.loads(manifest_path.read_text())["databases"]["accounting"]
    )


def test_reject_registered_shared_model_reference_before_source_creation(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    manifest_path = root / "aurora_models.json"
    manifest = {"databases": {"data": ["models.Invoice:Invoice"]}}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as denied:
        main(["model", "add", "Invoice", "--database", "data"])

    assert denied.value.code == 1
    assert manifest_path.read_bytes() == before
    assert not (root / "models" / "Invoice.py").exists()


def test_reject_cross_database_existing_model_reference_without_writing(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    databases_path = root / "aurora_databases.json"
    databases = json.loads(databases_path.read_text())
    databases["databases"]["accounting"] = {"engine": "sqlite", "extension": "sqlite3"}
    databases_path.write_text(json.dumps(databases))
    manifest_path = root / "aurora_models.json"
    manifest_path.write_text(json.dumps({"databases": {"accounting": ["models.Invoice:Invoice"]}}))
    before = manifest_path.read_bytes()
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as denied:
        main(["model", "add", "Invoice", "--database", "data"])

    assert denied.value.code == 1
    assert manifest_path.read_bytes() == before
    assert not (root / "models" / "Invoice.py").exists()


def test_generated_json_uses_four_spaces_per_level(tmp_path):
    root = create_project("sample", directory=tmp_path, mvc=True)
    for name in ("aurora_config.json", "aurora_databases.json", "aurora_models.json"):
        source = (root / name).read_text(encoding="utf-8")
        assert source.endswith("\n")
        assert json.loads(source)
        for line in source.splitlines():
            spaces = len(line) - len(line.lstrip(" "))
            assert spaces % 4 == 0, (name, line)


def test_registered_manifest_uses_four_spaces_per_level(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    monkeypatch.chdir(root)
    assert main(["model", "add", "Invoice"]) == 0
    source = (root / "aurora_models.json").read_text(encoding="utf-8")
    assert '\n    "databases": {\n        "data": [\n            "models.Invoice:Invoice"' in source
