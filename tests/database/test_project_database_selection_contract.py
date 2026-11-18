"""Project-level default selection and globally unique physical database names."""
import json

import pytest

from aurora.core.app_settings import load_application_settings
from aurora.database.cli_plan import _load_config, resolve_project_database
from aurora.database.configuration import DatabaseConfiguration


def project(tmp_path, databases, default=None):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({"databases": databases}))
    config = {"application": {"name": "Test"}}
    if default is not None:
        config["default_database"] = default
    (tmp_path / "aurora_config.json").write_text(json.dumps(config))
    return _load_config(tmp_path / "aurora_databases.json")[0]


def test_explicit_default_is_selected_without_creating_files(tmp_path):
    config = project(tmp_path, {"alpha": {"url": "sqlite:///alpha.sqlite3"},
                                "beta": {"url": "sqlite:///beta.sqlite3"}}, "beta")
    assert resolve_project_database(config, tmp_path) == "beta"
    assert resolve_project_database(config, tmp_path, "alpha") == "alpha"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aurora_config.json", "aurora_databases.json"]


def test_single_database_without_selector_and_legacy_default(tmp_path):
    config = project(tmp_path, {"main": {"url": "sqlite:///main.sqlite3"}})
    assert resolve_project_database(config, tmp_path) == "main"
    config = project(tmp_path, {"data": {"url": "sqlite:///data.sqlite3"},
                                "logs": {"url": "sqlite:///logs.sqlite3"}})
    with pytest.raises(ValueError, match="set default_database"):
        resolve_project_database(config, tmp_path)


def test_ambiguous_selection_fails_closed(tmp_path):
    config = project(tmp_path, {"alpha": {"url": "sqlite:///alpha.sqlite3"},
                                "beta": {"url": "sqlite:///beta.sqlite3"}})
    with pytest.raises(ValueError, match="set default_database"):
        resolve_project_database(config, tmp_path)


def test_unknown_default_and_bad_value_refuse(tmp_path):
    config = project(tmp_path, {"alpha": {"url": "sqlite:///alpha.sqlite3"}}, "missing")
    with pytest.raises(ValueError, match="Unknown database name"):
        resolve_project_database(config, tmp_path)
    path = tmp_path / "aurora_config.json"
    path.write_text('{"default_database":"../invalid"}')
    with pytest.raises(ValueError, match="default_database"):
        load_application_settings(path)


def test_cross_backend_physical_name_alias_is_refused():
    with pytest.raises(ValueError, match="configured name"):
        DatabaseConfiguration({"reports": {"url": "sqlite:///reports.sqlite3"},
                               "remote": {"url": "mysql+pymysql://user@host/reports"}})


def test_distinct_names_with_matching_connection_remain_supported():
    config = DatabaseConfiguration({"local": {"url": "sqlite:///local.sqlite3"},
                                    "reports": {"url": "postgresql+psycopg://user@host/reports"}})
    assert config.names() == ("local", "reports")


def test_empty_registry_is_not_a_valid_project_database_configuration():
    with pytest.raises(ValueError, match="at least one"):
        DatabaseConfiguration({})


def test_default_switch_only_changes_selection_and_never_moves_files(tmp_path):
    config = project(tmp_path, {"first": {"url": "sqlite:///first.sqlite3"},
                                "second": {"url": "sqlite:///second.sqlite3"}}, "first")
    historic = tmp_path / "migrations" / "first"
    historic.mkdir(parents=True)
    (historic / "00001.json").write_text("historical-evidence")
    assert resolve_project_database(config, tmp_path) == "first"
    settings_path = tmp_path / "aurora_config.json"
    settings = json.loads(settings_path.read_text())
    settings["default_database"] = "second"
    settings_path.write_text(json.dumps(settings))
    assert resolve_project_database(config, tmp_path) == "second"
    assert (historic / "00001.json").read_text() == "historical-evidence"
    assert not (tmp_path / "migrations" / "second").exists()
