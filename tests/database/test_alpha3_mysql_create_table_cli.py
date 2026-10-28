"""Safety checks for the legacy MySQL CREATE TABLE operation."""

from __future__ import annotations

import pytest

from aurora.cli import main
from aurora.database.cli_mysql_create_table import run_mysql_create_table


_ARGS = {
    "config_path": "missing.json",
    "alias": "test",
    "schema": "test",
    "table": "items",
    "model": "myapp.models:Item",
}


def test_legacy_command_is_not_public(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["db", "mysql-create-table", "--help"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_missing_confirmation(monkeypatch):
    monkeypatch.setenv("AURORA_MYSQL_MIGRATIONS", "1")

    with pytest.raises(PermissionError, match="--confirm APPLY"):
        run_mysql_create_table(
            **_ARGS,
            apply=True,
            approved_review="a" * 64,
            approved_sql="b" * 64,
        )


def test_environment_gate(monkeypatch):
    monkeypatch.delenv("AURORA_MYSQL_MIGRATIONS", raising=False)

    with pytest.raises(PermissionError, match="AURORA_MYSQL_MIGRATIONS"):
        run_mysql_create_table(
            **_ARGS,
            apply=True,
            confirmation="APPLY",
            approved_review="a" * 64,
            approved_sql="b" * 64,
        )


def test_production_gate(monkeypatch):
    monkeypatch.setenv("AURORA_ENV", "production")
    monkeypatch.setenv("AURORA_MYSQL_MIGRATIONS", "1")

    with pytest.raises(PermissionError, match="production"):
        run_mysql_create_table(
            **_ARGS,
            apply=True,
            confirmation="APPLY",
            approved_review="a" * 64,
            approved_sql="b" * 64,
        )


def test_review_rejects_approval_arguments():
    with pytest.raises(ValueError, match="require --apply"):
        run_mysql_create_table(
            **_ARGS,
            approved_sql="a" * 64,
        )


def test_retired_command_rejects_apply(capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "db",
                "mysql-create-table",
                "--database",
                "test",
                "--schema",
                "test",
                "--table",
                "items",
                "--model",
                "myapp.models:Item",
                "--apply",
                "--confirm",
                "APPLY",
                "--approve-review",
                "a" * 64,
                "--approve-sql",
                "b" * 64,
            ]
        )

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
