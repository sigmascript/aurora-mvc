"""Project-scoped production safeguards do not accept environment overrides."""

import json

import pytest

from aurora._cli_scaffolding import create_project
from aurora.cli import main
from aurora.core.project_policy import project_is_production


def _configure(root, *, production, strict=True):
    path = root / "aurora_config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(production=production, strict=strict)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_policy_is_isolated_by_project(tmp_path, monkeypatch):
    first = create_project("first", directory=tmp_path, mvc=True)
    second = create_project("second", directory=tmp_path, mvc=True)
    _configure(first, production=True)
    _configure(second, production=False)

    monkeypatch.setenv("AURORA_ENV", "development")
    assert project_is_production(first) is True
    assert project_is_production(second) is False
    monkeypatch.setenv("AURORA_ENV", "production")
    assert project_is_production(second) is False
    assert project_is_production(first) is True


def test_missing_and_invalid_configuration_fail_closed(tmp_path):
    with pytest.raises(PermissionError, match="missing"):
        project_is_production(tmp_path)
    path = tmp_path / "aurora_config.json"
    for text in ('{"production": "false"}', '{"production": false, "production": true}', '{broken'):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(PermissionError, match="invalid"):
            project_is_production(tmp_path)


def test_omitted_production_flag_stays_protected(tmp_path):
    (tmp_path / "aurora_config.json").write_text('{"strict": false}', encoding="utf-8")
    assert project_is_production(tmp_path) is True


def test_cli_component_changes_refuse_production_even_when_not_strict(tmp_path, monkeypatch, capsys):
    root = create_project("sample", directory=tmp_path, modular=True)
    _configure(root, production=True, strict=False)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as refusal:
        main(["app", "add", "newapp"])
    assert refusal.value.code == 1
    assert "production" in capsys.readouterr().err
    assert not (root / "apps/newapp").exists()


def test_cli_missing_policy_fails_without_creating_component(tmp_path, monkeypatch, capsys):
    root = create_project("sample", directory=tmp_path, modular=True)
    (root / "aurora_config.json").unlink()
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as refusal:
        main(["app", "add", "newapp"])
    assert refusal.value.code == 1
    assert "aurora_config.json" in capsys.readouterr().err
    assert not (root / "apps/newapp").exists()


def test_legacy_environment_variable_no_longer_blocks_development(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, modular=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("AURORA_ENV", "production")
    assert main(["app", "add", "newapp"]) == 0
    assert (root / "apps/newapp").exists()


def test_production_blocks_database_initialization_without_creating_file(
    tmp_path, monkeypatch, capsys
):
    root = create_project("sample", directory=tmp_path, mvc=True)
    _configure(root, production=True, strict=False)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as refusal:
        main(["db", "init", "--confirm", "yes"])
    assert refusal.value.code == 1
    assert "production" in capsys.readouterr().err
    assert not (root / "data.sqlite3").exists()
    assert not (root / "__migrations__").exists()


def test_existing_config_without_production_refuses_modification(
    tmp_path, monkeypatch, capsys
):
    root = create_project("sample", directory=tmp_path, modular=True)
    config = root / "aurora_config.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    del document["production"]
    document["strict"] = False
    config.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as refusal:
        main(["app", "add", "other"])
    assert refusal.value.code == 1
    assert "production" in capsys.readouterr().err
    assert not (root / "apps/other").exists()
