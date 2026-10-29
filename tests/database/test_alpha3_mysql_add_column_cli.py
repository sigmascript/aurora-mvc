"""Safety checks for the legacy MySQL ADD COLUMN operation."""

from __future__ import annotations

import pytest

from aurora.cli import main
from aurora.database.cli_mysql_add_column import run_mysql_add_column


def test_legacy_command_is_not_public(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["db", "mysql-add-column", "--help"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_missing_confirmation_refused_before_config(monkeypatch):
    monkeypatch.setenv("AURORA_MYSQL_MIGRATIONS", "1")

    with pytest.raises(PermissionError, match="--confirm APPLY"):
        run_mysql_add_column(
            config_path="missing.json",
            alias="test",
            schema="test",
            table="items",
            model="myapp.models:Item",
            column="note",
            apply=True,
            approved_review="a" * 64,
            approved_sql="b" * 64,
        )


def test_environment_gate_refused_before_config(monkeypatch):
    monkeypatch.delenv("AURORA_MYSQL_MIGRATIONS", raising=False)

    with pytest.raises(PermissionError, match="AURORA_MYSQL_MIGRATIONS"):
        run_mysql_add_column(
            config_path="missing.json",
            alias="test",
            schema="test",
            table="items",
            model="myapp.models:Item",
            column="note",
            apply=True,
            approved_review="a" * 64,
            approved_sql="b" * 64,
            confirmation="APPLY",
        )


def test_production_gate_refused_even_when_enabled(monkeypatch):
    monkeypatch.setenv("AURORA_ENV", "production")
    monkeypatch.setenv("AURORA_MYSQL_MIGRATIONS", "1")

    with pytest.raises(PermissionError, match="production"):
        run_mysql_add_column(
            config_path="missing.json",
            alias="test",
            schema="test",
            table="items",
            model="myapp.models:Item",
            column="note",
            apply=True,
            approved_review="a" * 64,
            approved_sql="b" * 64,
            confirmation="APPLY",
        )


def test_review_rejects_approval_options():
    with pytest.raises(ValueError, match="require --apply"):
        run_mysql_add_column(
            config_path="missing.json",
            alias="test",
            schema="test",
            table="items",
            model="myapp.models:Item",
            column="note",
            approved_review="a" * 64,
        )


def test_retired_command_rejects_apply(capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "db",
                "mysql-add-column",
                "--database",
                "test",
                "--schema",
                "test",
                "--table",
                "items",
                "--model",
                "myapp.models:Item",
                "--column",
                "note",
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
