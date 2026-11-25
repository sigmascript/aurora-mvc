"""Safe, actionable F06 configuration failures via public CLI."""

import json
from types import SimpleNamespace

import pytest

from aurora.cli import _database_failure, main
from aurora.database.configuration import DatabaseConfiguration


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"engine": "mysql", "user": "old"}, "'username'"),
        ({"engine": "postgresql"}, "'username'"),
        (
            {"engine": "mysql", "username": "app", "pwssord_invalid": "secret-value"},
            "unknown fields",
        ),
        (
            {"engine": "mysql", "username": "app", "password": "a", "password_env": "X"},
            "either 'password'",
        ),
        ({"engine": "mysql", "username": "app", "port": 0}, "'port'"),
        ({"engine": "sqlite", "extension": "sqlite3", "username": "bad"}, "SQLite accepts only"),
        ({"engine": "mysql", "username": "app", "host": "/secret"}, "'host'"),
        ({"url": "sqlite:///data.sqlite3", "engine": "sqlite"}, "either 'url'/'url_env'"),
        ({"url": "unrecognized://secret@bad/demo"}, "supported SQLite"),
        ({"engine": "sqlite", "database": "data.sqlite3"}, "Remove the obsolete database field"),
        ({"engine": "postgresql", "database": "demo", "username": "postgres"}, "remove database"),
    ],
)
def test_rejected_config_explains_specific_correction_without_echoing_secrets(entry, expected):
    with pytest.raises(ValueError) as caught:
        DatabaseConfiguration({"data": entry})
    message = _database_failure(
        caught.value, SimpleNamespace(config="aurora_databases.json"), "check"
    )
    assert expected.lower() in message.lower()
    assert "ValueError" not in message
    assert "No database changes were made" in message
    assert "unrecognized://secret" not in message
    assert "secret-value" not in message


def test_actual_cli_user_key_reports_username_without_creating_database(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aurora_databases.json").write_text(
        json.dumps(
            {
                "databases": {
                    "data": {"engine": "mysql", "user": "testuser", "password": "secret-value"}
                }
            }
        )
    )
    with pytest.raises(SystemExit) as caught:
        main(["db", "check"])
    assert caught.value.code == 1
    output = capsys.readouterr().err
    assert "'username'" in output
    assert "secret-value" not in output
    assert not (tmp_path / "data").exists()


def test_actual_cli_bad_json_explains_config_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aurora_databases.json").write_text("{invalid")
    with pytest.raises(SystemExit) as caught:
        main(["db", "check"])
    assert caught.value.code == 1
    assert "valid JSON" in capsys.readouterr().err


def test_unknown_external_exception_remains_redacted():
    exc = ValueError("sensitive password=secret and full host")
    message = _database_failure(exc, SimpleNamespace(config="aurora_databases.json"), "check")
    assert "secret" not in message
    assert "refused (ValueError)" in message


def test_known_production_refusal_is_actionable_but_unknown_permission_text_is_redacted():
    args = SimpleNamespace(config="aurora_databases.json", database="crm")
    message = _database_failure(
        PermissionError("Model migration is disabled in production"), args, "migration"
    )
    assert "Model migration is disabled in production" in message
    secret = "Project production policy contains private-password"
    message = _database_failure(PermissionError(secret), args, "migration")
    assert "private-password" not in message


@pytest.mark.parametrize("module,code", [("pymysql.err", 1049), ("psycopg.errors", "3D000")])
def test_server_missing_database_guidance_retains_alias_without_driver_text(module, code):
    error_type = type("MissingDatabase", (Exception,), {"__module__": module})
    error = error_type(code, "private-driver-password")
    if module.startswith("psycopg"):
        error.sqlstate = code
    args = SimpleNamespace(config="aurora_databases.json", database="crm")
    message = _database_failure(error, args, "check")
    assert "aurora db init --database crm" in message
    assert "aurora db check --database crm" in message
    assert "private-driver-password" not in message
