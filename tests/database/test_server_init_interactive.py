"""Ordinary server initialization uses reviewed terminal consent, not hashes."""

from argparse import Namespace
from types import SimpleNamespace
import sys

import pytest

from aurora.database import cli_init
from tests.database.test_mysql_cli import _setup as mysql_setup
from tests.database.test_postgres_cli_execution import _setup as postgres_setup


@pytest.mark.parametrize("dialect,setup", [
    ("mysql", mysql_setup), ("postgresql", postgres_setup),
])
def test_interactive_server_init_reviews_then_passes_title(monkeypatch, capsys, dialect, setup):
    connection = setup(monkeypatch)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    calls = []

    if dialect == "mysql":
        from aurora.database import server_forward_init
        monkeypatch.setattr(server_forward_init, "execute_server_forward_init", lambda conn, models, root, alias, **kwargs:
                            calls.append((conn, kwargs)) or __import__("pathlib").Path("20261112-000000.json"),)
    else:
        from aurora.database import server_forward_init
        monkeypatch.setattr(server_forward_init, "execute_server_forward_init", lambda conn, models, root, alias, **kwargs:
                            calls.append((conn, kwargs)) or __import__("pathlib").Path("20261112-000000.json"),)

    args = Namespace(config="unused", database="default", model=[], models_config=None,
                     approve=None, confirm=None, title="Create application schema")
    assert cli_init.run_init_apply(args) == 0
    assert calls[0][0] is connection
    assert calls[0][1]["title"] == "Create application schema"
    assert len(calls[0][1]["approval"]) == 64
    assert "Initialize this database?" not in capsys.readouterr().out


@pytest.mark.parametrize("setup", [mysql_setup, postgres_setup])
def test_interactive_server_init_refusal_makes_no_ddl_call(monkeypatch, capsys, setup):
    setup(monkeypatch)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    args = Namespace(config="unused", database="default", model=[], models_config=None,
                     approve=None, confirm=None, title=None)

    assert cli_init.run_init_apply(args) == 0
    assert "cancelled" in capsys.readouterr().out.lower()
